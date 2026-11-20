//! SLAI Documaster as a Tauri 2 desktop app.
//!
//! Documaster's engine is Python (the `slai` package, with SLAI's agents and models next to it) and its screen is the web client in
//! `dist/`. This crate does two things:
//!
//! * start the engine as a child process, wait for its `DOCUMASTER_READY {...}` line, then point the window at the engine's loopback
//!   address (`desktop.rs`);
//! * when the app closes, close the engine's standard input, which makes it exit (and end its own helper programs).
//!
//! The installed app is self-contained: `resources/engine/` holds a Python runtime, the packages, SLAI's source, the model Documaster's
//! language agent loads, and the web client (see `tools/build_bundle.py` and `docs/DESKTOP_APP.md`). Nothing is looked up on the
//! computer and nothing is downloaded at run time.
//!
//! The page the window shows (the engine's own) never calls Tauri, so no Tauri permissions are granted to it (see `capabilities/`).

mod desktop;
mod protocol;

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            desktop::start_engine(app.handle().clone());
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building SLAI Documaster")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                desktop::stop_engine(app);
            }
        });
}
