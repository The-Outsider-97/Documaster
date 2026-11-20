//! Desktop engine launcher: start Documaster's engine, wait until it is ready, show it, and stop it again.
//!
//! This is the part of the shell that needs Tauri and the operating system, so it is not covered by the std-only tests of
//! `protocol.rs`. It was written against the same Tauri 2 calls SciCal's launcher uses and has NOT been compiled or run by the person
//! who wrote it (see docs/DESKTOP_APP.md, "What was not built or run").

use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::sync::Mutex;
use std::time::Duration;

use tauri::{AppHandle, Manager};

use crate::protocol::{self, Line};

/// The port Documaster prefers, so the web view keeps the same address (and the same browser storage) from one run to the next.
/// If something else holds it the engine takes any free port (`--port-fallback`).
const PREFERRED_PORT: u16 = 47870;
/// Loading torch and SLAI's agents is done *after* the READY line, in the background; the server itself is ready in a few seconds. The
/// first start after an install can be slow while the virus scanner reads about a gigabyte of new files, hence the generous limit.
const START_TIMEOUT: Duration = Duration::from_secs(150);
const STOP_GRACE: Duration = Duration::from_secs(4);

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

struct Engine {
    child: Child,
    _stdin: Option<ChildStdin>, // kept open: closing it (when this process ends, however it ends) tells the engine to stop
}

static ENGINE: Mutex<Option<Engine>> = Mutex::new(None);

/// Where the engine writes what it prints to its error stream, and where the shell notes why it stopped:
/// `%LOCALAPPDATA%\SLAI\Documaster\engine.log` (the same folder as the engine's own working data).
fn data_dir() -> PathBuf {
    if cfg!(windows) {
        let base = std::env::var_os("LOCALAPPDATA")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("USERPROFILE").map(|p| PathBuf::from(p).join("AppData").join("Local")))
            .unwrap_or_else(std::env::temp_dir);
        base.join("SLAI").join("Documaster")
    } else {
        let base = std::env::var_os("XDG_DATA_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".local").join("share")))
            .unwrap_or_else(std::env::temp_dir);
        base.join("slai").join("documaster")
    }
}

/// Open a fresh engine log; the previous one is kept once as `engine.log.1`. A log that cannot be opened is not fatal.
fn open_log() -> (PathBuf, Option<File>) {
    let dir = data_dir();
    let path = dir.join("engine.log");
    let _ = fs::create_dir_all(&dir);
    let _ = fs::rename(&path, dir.join("engine.log.1"));
    let file = OpenOptions::new().create(true).write(true).truncate(true).open(&path).ok();
    (path, file)
}

/// The command that starts the engine, and a short description of which engine it is for the error messages.
///
/// A release build runs the bundled Python on the bundled app: `resources/engine/python/python.exe engine/app/run_documaster.py`,
/// made by `tools/build_bundle.py`. A debug build (`tauri dev`) runs the checkout's own Python instead (`python -m slai`), as does
/// a release build when `DOCUMASTER_DEV_PYTHON` is set.
fn command(app: &AppHandle) -> Result<Command, String> {
    if cfg!(debug_assertions) || std::env::var_os("DOCUMASTER_DEV_PYTHON").is_some() {
        let python = std::env::var("DOCUMASTER_DEV_PYTHON").unwrap_or_else(|_| if cfg!(windows) { "python".into() } else { "python3".into() });
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..");
        let mut c = Command::new(python);
        c.current_dir(root).args(protocol::engine_args(None, PREFERRED_PORT));
        return Ok(c);
    }
    let resources = app.path().resource_dir().map_err(|e| format!("The app's resource folder was not found: {e}"))?;
    let dir = PathBuf::from(protocol::strip_verbatim(&resources.join("engine").to_string_lossy()));
    let python = dir.join("python").join(if cfg!(windows) { "python.exe" } else { "bin/python3" });
    let script = dir.join("app").join("run_documaster.py");
    for (what, path) in [("the bundle record", dir.join("bundle.json")), ("the Python runtime", python.clone()), ("Documaster", script.clone())] {
        if !path.is_file() {
            return Err(format!("This installation is incomplete: {what} is missing ({}). Reinstall SLAI Documaster.", path.display()));
        }
    }
    let script_arg = script.to_string_lossy().into_owned();
    let mut c = Command::new(python);
    c.current_dir(&dir)
        .args(protocol::engine_args(Some(script_arg.as_str()), PREFERRED_PORT))
        .env_remove("PYTHONPATH")
        .env_remove("PYTHONHOME")
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1");
    Ok(c)
}

pub fn start_engine(app: AppHandle) {
    std::thread::spawn(move || {
        if let Err(message) = launch(&app) {
            show_error(&app, &message, "");
        }
    });
}

fn launch(app: &AppHandle) -> Result<(), String> {
    let (log_path, log) = open_log();
    let mut cmd = command(app)?;
    cmd.stdin(Stdio::piped()).stdout(Stdio::piped());
    // What the engine writes to its error stream (Python tracebacks, SLAI's own logging) goes to the log, never to a console.
    cmd.stderr(match log.as_ref().and_then(|f| f.try_clone().ok()) {
        Some(file) => Stdio::from(file),
        None => Stdio::null(),
    });
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = cmd.spawn().map_err(|e| format!("Could not start the Documaster engine: {e}"))?;
    let stdout = child.stdout.take().ok_or("The engine has no output")?;
    let stdin = child.stdin.take();
    *ENGINE.lock().unwrap() = Some(Engine { child, _stdin: stdin });

    // Read the engine's lines. The READY line (it carries the access token) is never written to the log; the rest is, so the log shows
    // the banner and the agents' loading messages. Reading continues after READY so the engine never blocks on a full pipe, and the end
    // of the stream is how the shell learns that the engine has stopped.
    let (tx, rx) = mpsc::channel::<Result<String, String>>();
    let reader_log = Mutex::new(log);
    let reader_path = log_path.clone();
    let reader_app = app.clone();
    std::thread::spawn(move || {
        let mut answered = false;
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            match protocol::parse_line(&line) {
                Line::Ready { url } => {
                    if !answered {
                        answered = true;
                        let _ = tx.send(protocol::validate_url(&url).map(|_| url));
                    }
                }
                Line::Error(message) => {
                    if !answered {
                        answered = true;
                        let _ = tx.send(Err(if message.is_empty() { "Documaster could not start.".to_string() } else { message }));
                    }
                }
                Line::Other => {
                    if let Some(file) = reader_log.lock().unwrap().as_mut() {
                        let _ = writeln!(file, "{line}");
                    }
                }
            }
        }
        // End of the stream: the engine has stopped (or closed its output).
        let code = wait_for_exit_code();
        if !answered {
            let tail = tail_of(&reader_path);
            let _ = tx.send(Err(format!(
                "The Documaster engine stopped before it was ready{}. Its log is {}.{}",
                code.map(|c| format!(" (exit code {c})")).unwrap_or_default(),
                reader_path.display(),
                if tail.is_empty() { String::new() } else { format!("\n\n{tail}") }
            )));
        } else {
            show_stopped(&reader_app, code);
        }
    });

    let url = match rx.recv_timeout(START_TIMEOUT) {
        Ok(result) => result?,
        Err(_) => return Err(format!("The Documaster engine did not start in time. Its log is {}.", log_path.display())),
    };
    let window = app.get_webview_window("main").ok_or("The main window is missing")?;
    window
        .eval(&format!("window.location.replace({})", protocol::js_string(&url)))
        .map_err(|e| format!("Could not open the Documaster window: {e}"))?;
    Ok(())
}

/// The engine's exit code, if it has ended (polled briefly: the end of its output can come a moment before the process is reaped).
fn wait_for_exit_code() -> Option<i32> {
    for _ in 0..20 {
        {
            let mut guard = ENGINE.lock().unwrap();
            if let Some(engine) = guard.as_mut() {
                if let Ok(Some(status)) = engine.child.try_wait() {
                    return status.code();
                }
            } else {
                return None; // the app is closing and stop_engine already took it
            }
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    None
}

fn tail_of(log: &Path) -> String {
    fs::read_to_string(log).map(|t| protocol::tail_lines(&t, 14)).unwrap_or_default()
}

fn show_error(app: &AppHandle, message: &str, detail: &str) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval(&format!(
            "window.documasterFailed && window.documasterFailed({}, {})",
            protocol::js_string(message),
            protocol::js_string(detail)
        ));
    }
}

/// The engine ended while the window was showing it: the page is now talking to nothing. Cover it with a message instead of leaving a
/// window of connection errors. (A normal quit leaves `ENGINE` empty, so nothing is shown then.)
fn show_stopped(app: &AppHandle, code: Option<i32>) {
    if ENGINE.lock().unwrap().is_none() {
        return;
    }
    let message = format!(
        "The Documaster engine stopped{}. Close this window and open Documaster again. Its log is {}.",
        code.map(|c| format!(" (exit code {c})")).unwrap_or_default(),
        data_dir().join("engine.log").display()
    );
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval(&format!(
            "(function(m){{var d=document.createElement('div');d.setAttribute('role','alert');\
             d.style.cssText='position:fixed;inset:0;z-index:2147483647;display:grid;place-content:center;padding:2rem;text-align:center;\
             background:#1b2028;color:#e6ebf1;font:16px system-ui,sans-serif';d.textContent=m;document.body.appendChild(d);}})({})",
            protocol::js_string(&message)
        ));
    }
}

pub fn stop_engine(_app: &AppHandle) {
    let Some(mut engine) = ENGINE.lock().unwrap().take() else { return };
    drop(engine._stdin.take()); // the engine sees the end of its input and shuts down on its own
    let deadline = std::time::Instant::now() + STOP_GRACE;
    while std::time::Instant::now() < deadline {
        if matches!(engine.child.try_wait(), Ok(Some(_))) {
            return;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    kill_tree(&mut engine.child);
}

/// Stop the engine and everything it started (LibreOffice, Tesseract). `Child::kill` ends only the engine itself.
fn kill_tree(child: &mut Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .creation_flags(CREATE_NO_WINDOW)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }
    let _ = child.kill();
    let _ = child.wait();
}
