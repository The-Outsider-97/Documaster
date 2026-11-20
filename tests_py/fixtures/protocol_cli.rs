// Test harness: reads engine output lines on stdin and prints how the shell's parser (src-tauri/src/protocol.rs) classifies each one.
// Built by tests_py/test_shell_protocol.py with plain rustc; no Tauri needed.
#[allow(dead_code)]
#[path = "../../src-tauri/src/protocol.rs"]
mod protocol;

use std::io::{BufRead, Write};

fn main() {
    let stdin = std::io::stdin();
    let mut out = std::io::stdout().lock();
    for line in stdin.lock().lines().map_while(Result::ok) {
        let shown = match protocol::parse_line(&line) {
            protocol::Line::Ready { url } => format!("ready\t{}\t{}", url.replace('\n', "\\n"), protocol::validate_url(&url).is_ok()),
            protocol::Line::Error(text) => format!("error\t{text}"),
            protocol::Line::Other => "other".to_string(),
        };
        writeln!(out, "{shown}").unwrap();
    }
}
