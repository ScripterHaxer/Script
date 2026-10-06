//! GideonOS configuration as seen by the compositor.
//!
//! Reads the same layered files as `gideon-config`:
//! `/usr/share/gideon/defaults/<domain>.conf` then `/etc/gideon/<domain>.conf`
//! (a flat TOML subset: `name = "string" | true | false | number`).
//! `GIDEON_CONFIG_ROOT` prefixes both paths (used by tests on a dev host).

use std::{collections::HashMap, fs, path::PathBuf};

#[derive(Debug, Clone)]
pub struct Config {
    /// Desktop background, RGB 0..1.
    pub background: [f32; 3],
    /// "preferred" or WIDTHxHEIGHT, applied to every output.
    pub mode: Option<(u16, u16)>,
    pub scale: f64,
    pub keyboard_layout: String,
    /// Command started by Super+Enter.
    pub terminal: String,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            background: rgb("0f1216").unwrap(),
            mode: None,
            scale: 1.0,
            keyboard_layout: "us".into(),
            terminal: "foot".into(),
        }
    }
}

/// Filesystem root the configuration layers live under ("/" unless overridden).
fn root() -> PathBuf {
    match std::env::var("GIDEON_CONFIG_ROOT") {
        Ok(r) if !r.is_empty() => PathBuf::from(r),
        _ => PathBuf::from("/"),
    }
}

fn layer_paths(name: &str) -> [PathBuf; 2] {
    ["usr/share/gideon/defaults", "etc/gideon"].map(|dir| root().join(dir).join(format!("{name}.conf")))
}

/// Parse a flat TOML-subset file into key -> unquoted value.
pub fn parse(text: &str) -> HashMap<String, String> {
    let mut out = HashMap::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((k, v)) = line.split_once('=') else { continue };
        let mut v = v.trim().to_string();
        if v.len() >= 2 && v.starts_with('"') && v.ends_with('"') {
            v = v[1..v.len() - 1].replace("\\\"", "\"").replace("\\\\", "\\");
        }
        out.insert(k.trim().to_string(), v);
    }
    out
}

/// Effective values of one domain (vendor defaults overlaid by machine config).
pub fn domain(name: &str) -> HashMap<String, String> {
    let mut values = HashMap::new();
    for path in layer_paths(name) {
        if let Ok(text) = fs::read_to_string(&path) {
            values.extend(parse(&text));
        }
    }
    values
}

pub fn rgb(hex: &str) -> Option<[f32; 3]> {
    let hex = hex.trim_start_matches('#');
    if hex.len() != 6 || !hex.chars().all(|c| c.is_ascii_hexdigit()) {
        return None;
    }
    let c = |i: usize| u8::from_str_radix(&hex[i..i + 2], 16).ok().map(|v| v as f32 / 255.0);
    Some([c(0)?, c(2)?, c(4)?])
}

pub fn parse_mode(s: &str) -> Option<(u16, u16)> {
    let (w, h) = s.split_once('x')?;
    Some((w.trim().parse().ok()?, h.trim().parse().ok()?))
}

impl Config {
    pub fn load() -> Config {
        let mut cfg = Config::default();
        let display = domain("display");
        if let Some(bg) = display.get("background").and_then(|v| rgb(v)) {
            cfg.background = bg;
        }
        cfg.mode = display.get("mode").and_then(|v| parse_mode(v));
        if let Some(scale) = display.get("scale").and_then(|v| v.parse::<f64>().ok()) {
            if (0.5..=4.0).contains(&scale) {
                cfg.scale = scale;
            }
        }
        if let Some(layout) = domain("input").get("keyboard_layout") {
            if !layout.is_empty() {
                cfg.keyboard_layout = layout.clone();
            }
        }
        if let Some(term) = domain("session").get("terminal") {
            if !term.is_empty() {
                cfg.terminal = term.clone();
            }
        }
        cfg
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_flat_toml_subset() {
        let v = parse("# c\nmode = \"1024x768\"\nscale = 2\n bad line\nq = \"a \\\"b\\\"\"\n");
        assert_eq!(v["mode"], "1024x768");
        assert_eq!(v["scale"], "2");
        assert_eq!(v["q"], "a \"b\"");
        assert!(!v.contains_key("bad line"));
    }

    #[test]
    fn layers_are_absolute_system_paths() {
        // Regression: an unset GIDEON_CONFIG_ROOT once produced relative paths,
        // so the compositor silently ignored /etc/gideon on the real system.
        std::env::remove_var("GIDEON_CONFIG_ROOT");
        let [vendor, machine] = layer_paths("display");
        assert_eq!(vendor, PathBuf::from("/usr/share/gideon/defaults/display.conf"));
        assert_eq!(machine, PathBuf::from("/etc/gideon/display.conf"));
    }

    #[test]
    fn colours_and_modes() {
        assert_eq!(rgb("0f1216").map(|c| (c[0] * 255.0).round() as u8), Some(15));
        assert!(rgb("zz1216").is_none());
        assert_eq!(parse_mode("1024x768"), Some((1024, 768)));
        assert_eq!(parse_mode("preferred"), None);
    }
}
