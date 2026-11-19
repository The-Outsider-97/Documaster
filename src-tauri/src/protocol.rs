//! The conversation between this shell and the Documaster engine, and the small pure helpers around it.
//!
//! Nothing in this file uses Tauri or any crate: it compiles with plain `rustc`, so it can be tested without the Tauri toolchain
//! (`rustc --test --edition 2021 src/protocol.rs`, which `tests_py/test_shell_protocol.py` does). The engine's side of the protocol is
//! `slai/engine.py`; that test also feeds this parser the lines Python really writes.
//!
//! Protocol (engine -> shell, one line each, on the engine's standard output):
//!
//! * `DOCUMASTER_READY {"url":"http://127.0.0.1:PORT/?token=...", ...}`: listening. `url` carries the per-launch access token.
//! * `DOCUMASTER_ERROR text`: cannot start.
//!
//! Shell -> engine: closing the engine's standard input means "quit".

/// What one line of engine output means.
#[derive(Debug, PartialEq, Eq)]
pub enum Line {
    Ready { url: String },
    Error(String),
    Other,
}

const READY_PREFIX: &str = "DOCUMASTER_READY ";
const ERROR_PREFIX: &str = "DOCUMASTER_ERROR";

pub fn parse_line(line: &str) -> Line {
    let line = line.trim_end_matches(['\r', '\n']);
    if let Some(json) = line.strip_prefix(READY_PREFIX) {
        return match json_string_field(json, "url") {
            Some(url) => Line::Ready { url },
            None => Line::Other,
        };
    }
    if let Some(rest) = line.strip_prefix(ERROR_PREFIX) {
        if rest.is_empty() || rest.starts_with(' ') {
            return Line::Error(rest.trim().to_string());
        }
    }
    Line::Other
}

/// The address the window may be pointed at: plain `http` on this computer's loopback with an explicit port, no user info.
/// The engine only ever listens there; anything else on the READY line is not from the engine and is not opened.
pub fn validate_url(url: &str) -> Result<(), String> {
    let rest = url.strip_prefix("http://").ok_or("the engine's address is not plain http")?;
    let (authority, tail) = match rest.find(['/', '?', '#']) {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    let port = ["127.0.0.1:", "localhost:", "[::1]:"]
        .iter()
        .find_map(|prefix| authority.strip_prefix(prefix))
        .ok_or("the engine's address is not on this computer's loopback")?;
    if port.is_empty() || port.len() > 5 || !port.bytes().all(|b| b.is_ascii_digit()) || port.parse::<u32>().map_or(true, |p| p == 0 || p > 65535) {
        return Err("the engine's address has no valid port".into());
    }
    if tail.chars().any(|c| c.is_control() || c.is_whitespace()) {
        return Err("the engine's address contains control characters".into());
    }
    Ok(())
}

/// Arguments for starting the engine. `script` is the Python file to run (`None` when running `-m slai` in a checkout).
pub fn engine_args(script: Option<&str>, preferred_port: u16) -> Vec<String> {
    let mut args: Vec<String> = Vec::new();
    match script {
        Some(path) => args.push(path.to_string()),
        None => args.extend(["-m".to_string(), "slai".to_string()]),
    }
    args.extend(
        ["--no-browser", "--ready-line", "--exit-on-stdin-close", "--port-fallback", "--port"].map(String::from),
    );
    args.push(preferred_port.to_string());
    args
}

/// Tauri can hand out Windows paths in the `\\?\C:\...` form. Python and most programs accept it, a few do not; the plain form is
/// always accepted. UNC shares (`\\?\UNC\server\share`) keep the prefix because they have no plain drive form.
pub fn strip_verbatim(path: &str) -> String {
    match path.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC\\") && rest.as_bytes().get(1) == Some(&b':') => rest.to_string(),
        _ => path.to_string(),
    }
}

/// `text` as a JavaScript string literal that is safe to place in `window.eval(...)`: quotes, backslashes, control characters and the
/// two line separators that end a JavaScript line are escaped, and `<` is escaped so the literal can never close a `<script>`.
pub fn js_string(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '<' => out.push_str("\\u003c"),
            '\u{2028}' => out.push_str("\\u2028"),
            '\u{2029}' => out.push_str("\\u2029"),
            c if (c as u32) < 0x20 || c == '\u{7f}' => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
    out
}

/// The last `n` non-empty lines of `text`, for showing why the engine stopped.
pub fn tail_lines(text: &str, n: usize) -> String {
    let lines: Vec<&str> = text.lines().map(str::trim_end).filter(|l| !l.is_empty()).collect();
    lines[lines.len().saturating_sub(n)..].join("\n")
}

// ------------------------------------------------------------------------------------------------------------- a small JSON reader
/// The string value of `key` in a flat JSON object, or `None` if the text is not a JSON object or the key is missing or not a string.
/// Only what the READY line needs: strings with all JSON escapes (including surrogate pairs), and scalars/arrays/objects skipped.
pub fn json_string_field(json: &str, key: &str) -> Option<String> {
    let mut p = Parser { s: json.as_bytes(), i: 0 };
    p.ws();
    p.eat(b'{')?;
    p.ws();
    if p.peek() == Some(b'}') {
        return None;
    }
    loop {
        p.ws();
        let name = p.string()?;
        p.ws();
        p.eat(b':')?;
        p.ws();
        if p.peek() == Some(b'"') {
            let value = p.string()?;
            if name == key {
                return Some(value);
            }
        } else {
            p.skip_value()?;
        }
        p.ws();
        match p.next()? {
            b',' => continue,
            b'}' => return None,
            _ => return None,
        }
    }
}

struct Parser<'a> {
    s: &'a [u8],
    i: usize,
}

impl<'a> Parser<'a> {
    fn peek(&self) -> Option<u8> {
        self.s.get(self.i).copied()
    }
    fn next(&mut self) -> Option<u8> {
        let b = self.peek()?;
        self.i += 1;
        Some(b)
    }
    fn eat(&mut self, b: u8) -> Option<()> {
        (self.next()? == b).then_some(())
    }
    fn ws(&mut self) {
        while matches!(self.peek(), Some(b' ' | b'\t' | b'\r' | b'\n')) {
            self.i += 1;
        }
    }
    fn hex4(&mut self) -> Option<u32> {
        let mut v = 0u32;
        for _ in 0..4 {
            v = v * 16 + (self.next()? as char).to_digit(16)?;
        }
        Some(v)
    }
    fn string(&mut self) -> Option<String> {
        self.eat(b'"')?;
        let mut out: Vec<u8> = Vec::new();
        loop {
            match self.next()? {
                b'"' => return String::from_utf8(out).ok(),
                b'\\' => {
                    let ch = match self.next()? {
                        b'"' => '"',
                        b'\\' => '\\',
                        b'/' => '/',
                        b'b' => '\u{8}',
                        b'f' => '\u{c}',
                        b'n' => '\n',
                        b'r' => '\r',
                        b't' => '\t',
                        b'u' => {
                            let hi = self.hex4()?;
                            if (0xD800..0xDC00).contains(&hi) {
                                self.eat(b'\\')?;
                                self.eat(b'u')?;
                                let lo = self.hex4()?;
                                if !(0xDC00..0xE000).contains(&lo) {
                                    return None;
                                }
                                char::from_u32(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))?
                            } else {
                                char::from_u32(hi)?
                            }
                        }
                        _ => return None,
                    };
                    let mut buf = [0u8; 4];
                    out.extend_from_slice(ch.encode_utf8(&mut buf).as_bytes());
                }
                b if b < 0x20 => return None,
                b => out.push(b),
            }
        }
    }
    /// Skip one value that is not a string: number, `true`/`false`/`null`, or a (possibly nested) array/object.
    fn skip_value(&mut self) -> Option<()> {
        match self.peek()? {
            b'[' | b'{' => {
                let mut depth = 0usize;
                loop {
                    match self.peek()? {
                        b'"' => {
                            self.string()?;
                            continue;
                        }
                        b'[' | b'{' => depth += 1,
                        b']' | b'}' => depth -= 1,
                        _ => {}
                    }
                    self.i += 1;
                    if depth == 0 {
                        return Some(());
                    }
                }
            }
            _ => {
                let start = self.i;
                while matches!(self.peek(), Some(b) if !matches!(b, b',' | b'}' | b']' | b' ' | b'\t' | b'\r' | b'\n')) {
                    self.i += 1;
                }
                (self.i > start).then_some(())
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ready_line_gives_the_url() {
        let line = r#"DOCUMASTER_READY {"url":"http://127.0.0.1:47870/?token=abc_-D","port":47870,"version":"2.4.0","bundled":true}"#;
        assert_eq!(parse_line(line), Line::Ready { url: "http://127.0.0.1:47870/?token=abc_-D".into() });
        assert_eq!(parse_line(&format!("{line}\r\n")), Line::Ready { url: "http://127.0.0.1:47870/?token=abc_-D".into() });
    }

    #[test]
    fn url_may_come_after_other_fields_and_nested_values() {
        let line = r#"DOCUMASTER_READY {"a":[1,{"url":"decoy"},"x]"],"b":{"url":"decoy2"},"n":null,"t":true,"url":"http://127.0.0.1:1/"}"#;
        assert_eq!(parse_line(line), Line::Ready { url: "http://127.0.0.1:1/".into() });
    }

    #[test]
    fn json_escapes_including_surrogate_pairs() {
        let s = r#"{"url":"a\"b\\c\/d\n\u00e9\u4e2d\ud83d\ude00"}"#;
        assert_eq!(json_string_field(s, "url").unwrap(), "a\"b\\c/d\n\u{e9}\u{4e2d}\u{1F600}");
    }

    #[test]
    fn malformed_ready_lines_are_not_ready() {
        for line in [
            "DOCUMASTER_READY",
            "DOCUMASTER_READY ",
            "DOCUMASTER_READY not json",
            r#"DOCUMASTER_READY {"port":1}"#,
            r#"DOCUMASTER_READY {"url":5}"#,
            r#"DOCUMASTER_READY {"url":"unterminated"#,
            r#"DOCUMASTER_READY {"url":"bad \q escape"}"#,
            r#"DOCUMASTER_READY {"url":"lone \ud83d surrogate"}"#,
            r#"DOCUMASTER_READY ["url"]"#,
            r#"DOCUMASTER_READYX {"url":"http://127.0.0.1:1/"}"#,
            "  Open      : http://127.0.0.1:1/",
            "",
        ] {
            assert_eq!(parse_line(line), Line::Other, "{line:?}");
        }
    }

    #[test]
    fn error_line_gives_the_text() {
        assert_eq!(parse_line("DOCUMASTER_ERROR SLAI is missing\r\n"), Line::Error("SLAI is missing".into()));
        assert_eq!(parse_line("DOCUMASTER_ERROR"), Line::Error(String::new()));
        assert_eq!(parse_line("DOCUMASTER_ERRORS something"), Line::Other);
    }

    #[test]
    fn only_loopback_http_with_a_port_is_opened() {
        for ok in [
            "http://127.0.0.1:47870/?token=abc",
            "http://localhost:1/",
            "http://[::1]:65535/",
            "http://127.0.0.1:8080",
        ] {
            assert_eq!(validate_url(ok), Ok(()), "{ok}");
        }
        for bad in [
            "https://127.0.0.1:1/",
            "http://example.com:1/",
            "http://127.0.0.1/",
            "http://127.0.0.1:/",
            "http://127.0.0.1:0/",
            "http://127.0.0.1:65536/",
            "http://127.0.0.1:123456/",
            "http://127.0.0.1:80x/",
            "http://user@127.0.0.1:1/",
            "http://127.0.0.1:1@evil.example/",
            "http://127.0.0.1.evil.example:1/",
            "http://localhost.evil.example:1/",
            "http://127.0.0.1:1/\n",
            "http://127.0.0.1:1/ x",
            "file:///C:/Windows/win.ini",
            "javascript:alert(1)",
            "",
        ] {
            assert!(validate_url(bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn engine_arguments() {
        assert_eq!(
            engine_args(Some(r"C:\app\run_documaster.py"), 47870),
            [r"C:\app\run_documaster.py", "--no-browser", "--ready-line", "--exit-on-stdin-close", "--port-fallback", "--port", "47870"]
        );
        assert_eq!(&engine_args(None, 1)[..3], ["-m", "slai", "--no-browser"]);
        assert_eq!(engine_args(None, 1).last().unwrap(), "1");
    }

    #[test]
    fn verbatim_prefix_is_removed_only_when_a_plain_path_exists() {
        assert_eq!(strip_verbatim(r"\\?\C:\Users\me\AppData\Local\Programs\x"), r"C:\Users\me\AppData\Local\Programs\x");
        assert_eq!(strip_verbatim(r"C:\already\plain"), r"C:\already\plain");
        assert_eq!(strip_verbatim(r"\\?\UNC\server\share\x"), r"\\?\UNC\server\share\x");
        assert_eq!(strip_verbatim(r"\\?\Volume{1234}\x"), r"\\?\Volume{1234}\x");
        assert_eq!(strip_verbatim("/usr/lib"), "/usr/lib");
        assert_eq!(strip_verbatim(""), "");
    }

    #[test]
    fn js_literal_cannot_break_out() {
        assert_eq!(js_string(r#"a"b\c"#), r#""a\"b\\c""#);
        assert_eq!(js_string("line1\nline2\r\t"), r#""line1\nline2\r\t""#);
        assert_eq!(js_string("</script><b>"), r#""\u003c/script>\u003cb>""#);
        assert_eq!(js_string("\u{2028}\u{2029}\u{0}\u{1f}\u{7f}"), r#""\u2028\u2029\u0000\u001f\u007f""#);
        assert_eq!(js_string("caf\u{e9} \u{4e2d} \u{1F600}"), "\"caf\u{e9} \u{4e2d} \u{1F600}\"");
        let hostile = js_string("\"); fetch('http://evil'); (\"");
        assert!(hostile.starts_with('"') && hostile.ends_with('"'));
        assert!(!hostile[1..hostile.len() - 1].contains("\"; "), "an unescaped quote would end the literal early: {hostile}");
    }

    #[test]
    fn tail_keeps_the_last_non_empty_lines() {
        assert_eq!(tail_lines("a\n\nb  \nc\nd\n", 2), "c\nd");
        assert_eq!(tail_lines("only", 5), "only");
        assert_eq!(tail_lines("", 3), "");
    }
}
