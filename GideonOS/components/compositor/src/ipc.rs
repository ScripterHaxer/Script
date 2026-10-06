//! Compositor IPC: one JSON request per connection on a Unix socket.
//!
//! Socket: `$GIDEON_COMPOSITOR_SOCKET`, default `$XDG_RUNTIME_DIR/gideon-compositor.sock`
//! (the runtime dir is private to the user). Request: `{"cmd": "...", "args": [...]}`.
//! Reply: `{"ok": true, "result": ...}` or `{"ok": false, "error": "..."}`.
//! Used by tests now and by the desktop shell (M5).

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::{UnixListener, UnixStream},
    path::PathBuf,
    time::Duration,
};

use serde_json::{json, Value};
use smithay::{
    reexports::calloop::{generic::Generic, Interest, Mode, PostAction},
    utils::{Point, Size},
};

use crate::{wm::Layout, App};

pub fn socket_path() -> PathBuf {
    if let Ok(p) = std::env::var("GIDEON_COMPOSITOR_SOCKET") {
        return PathBuf::from(p);
    }
    let dir = std::env::var("XDG_RUNTIME_DIR").unwrap_or_else(|_| "/tmp".into());
    PathBuf::from(dir).join("gideon-compositor.sock")
}

pub fn listen(app_loop: &smithay::reexports::calloop::LoopHandle<'static, App>) -> std::io::Result<PathBuf> {
    let path = socket_path();
    if path.exists() {
        // A live compositor answers; a stale socket does not.
        if UnixStream::connect(&path).is_ok() {
            return Err(std::io::Error::new(std::io::ErrorKind::AddrInUse, "another compositor owns the IPC socket"));
        }
        std::fs::remove_file(&path)?;
    }
    let listener = UnixListener::bind(&path)?;
    listener.set_nonblocking(true)?;
    app_loop
        .insert_source(Generic::new(listener, Interest::READ, Mode::Level), |_, listener, app| {
            while let Ok((stream, _)) = listener.accept() {
                serve(app, stream);
            }
            Ok(PostAction::Continue)
        })
        .map_err(|e| std::io::Error::other(e.to_string()))?;
    Ok(path)
}

fn serve(app: &mut App, stream: UnixStream) {
    let _ = stream.set_nonblocking(false);
    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
    let mut line = String::new();
    if BufReader::new(&stream).read_line(&mut line).is_err() {
        return;
    }
    let reply = match serde_json::from_str::<Value>(&line) {
        Ok(req) => match handle(app, &req) {
            Ok(result) => json!({"ok": true, "result": result}),
            Err(error) => json!({"ok": false, "error": error}),
        },
        Err(e) => json!({"ok": false, "error": format!("bad request: {e}")}),
    };
    let mut stream = stream;
    let _ = writeln!(stream, "{reply}");
}

fn arg_str(args: &[Value], i: usize) -> Result<String, String> {
    match args.get(i) {
        Some(Value::String(s)) => Ok(s.clone()),
        Some(v) => Ok(v.to_string()),
        None => Err(format!("missing argument {}", i + 1)),
    }
}

fn arg_i(args: &[Value], i: usize) -> Result<i64, String> {
    let s = arg_str(args, i)?;
    s.trim_matches('"').parse::<i64>().map_err(|_| format!("argument {} must be a number", i + 1))
}

fn window_id(app: &App, args: &[Value]) -> Result<u64, String> {
    let id = arg_i(args, 0)? as u64;
    app.state.wm.get(id).map(|_| id).ok_or_else(|| format!("no window {id}"))
}

pub fn handle(app: &mut App, req: &Value) -> Result<Value, String> {
    let cmd = req.get("cmd").and_then(Value::as_str).ok_or("missing cmd")?.to_string();
    let args: Vec<Value> = req.get("args").and_then(Value::as_array).cloned().unwrap_or_default();
    let st = &mut app.state;
    st.redraw = true;
    match cmd.as_str() {
        "version" => Ok(json!({"name": "gideon-compositor", "version": env!("CARGO_PKG_VERSION"), "backend": app.backend.name()})),
        "outputs" => Ok(Value::Array(
            st.space
                .outputs()
                .map(|o| {
                    let g = st.space.output_geometry(o).unwrap_or_default();
                    let mode = o.current_mode();
                    json!({
                        "name": o.name(),
                        "x": g.loc.x, "y": g.loc.y, "width": g.size.w, "height": g.size.h,
                        "mode": mode.map(|m| json!({"width": m.size.w, "height": m.size.h, "refresh": m.refresh})),
                        "scale": o.current_scale().fractional_scale(),
                        "modes": app.backend.modes(o).into_iter().map(|(w, h, r)| format!("{w}x{h}@{}", r / 1000)).collect::<Vec<_>>(),
                    })
                })
                .collect(),
        )),
        "windows" => Ok(Value::Array(
            st.wm
                .windows
                .iter()
                .filter(|m| m.placed)
                .map(|m| {
                    let (title, app_id) = crate::wm::title_of(&m.window);
                    let loc = st.space.element_location(&m.window).unwrap_or(m.loc);
                    let size = m.window.geometry().size;
                    json!({
                        "id": m.id, "title": title, "app_id": app_id,
                        "workspace": m.workspace + 1,
                        "x": loc.x, "y": loc.y, "width": size.w, "height": size.h,
                        "focused": st.wm.focused == Some(m.id),
                        "minimized": m.minimized,
                        "maximized": m.layout == Layout::Maximized,
                        "fullscreen": m.layout == Layout::Fullscreen,
                        "tiled": match m.layout { Layout::TiledLeft => "left", Layout::TiledRight => "right", _ => "" },
                        "stacking": m.z,
                    })
                })
                .collect(),
        )),
        "workspaces" => Ok(json!({"active": st.wm.active + 1, "count": crate::wm::WORKSPACES})),
        "pointer" => {
            let p = st.seat.get_pointer().map(|p| p.current_location()).unwrap_or_default();
            Ok(json!({"x": p.x, "y": p.y}))
        }
        "workspace" => {
            let n = arg_i(&args, 0)?;
            if !(1..=crate::wm::WORKSPACES as i64).contains(&n) {
                return Err("workspace must be 1..9".into());
            }
            st.switch_workspace(n as usize - 1);
            Ok(json!(n))
        }
        "focus" => {
            let id = window_id(app, &args)?;
            app.state.focus(id);
            Ok(json!(id))
        }
        "close" | "maximize" | "minimize" | "restore" | "fullscreen" | "tile-left" | "tile-right" | "floating" => {
            let id = window_id(app, &args)?;
            let st = &mut app.state;
            match cmd.as_str() {
                "close" => st.close(id),
                "maximize" => st.toggle_maximized(id),
                "minimize" => st.minimize(id),
                "restore" => st.focus(id),
                "fullscreen" => st.toggle_fullscreen(id),
                "tile-left" => st.tile(id, true),
                "tile-right" => st.tile(id, false),
                _ => st.set_layout(id, Layout::Floating),
            }
            Ok(json!(id))
        }
        "move" => {
            let id = window_id(app, &args)?;
            let loc = Point::from((arg_i(&args, 1)? as i32, arg_i(&args, 2)? as i32));
            app.state.move_window(id, loc);
            Ok(json!(id))
        }
        "resize" => {
            let id = window_id(app, &args)?;
            let size = Size::from((arg_i(&args, 1)? as i32, arg_i(&args, 2)? as i32));
            app.state.resize_window(id, size);
            Ok(json!(id))
        }
        "to-workspace" => {
            let id = window_id(app, &args)?;
            let n = arg_i(&args, 1)?;
            if !(1..=crate::wm::WORKSPACES as i64).contains(&n) {
                return Err("workspace must be 1..9".into());
            }
            app.state.move_to_workspace(id, n as usize - 1);
            Ok(json!(id))
        }
        "action" => {
            // Keyboard-shortcut actions by name, e.g. "terminal", "cycle-windows".
            let name = arg_str(&args, 0)?;
            let action = match name.as_str() {
                "terminal" => crate::input::Action::Terminal,
                "cycle-windows" => crate::input::Action::CycleWindows,
                "screenshot" => crate::input::Action::Screenshot,
                other => return Err(format!("unknown action {other}")),
            };
            app.state.run_action(action);
            Ok(json!(name))
        }
        "spawn" => {
            let command = args.iter().map(|a| a.as_str().map(str::to_string).unwrap_or_else(|| a.to_string())).collect::<Vec<_>>().join(" ");
            if command.trim().is_empty() {
                return Err("spawn needs a command".into());
            }
            crate::spawn(&command, &app.state.socket_name);
            Ok(json!(command))
        }
        "set-mode" | "set-scale" => {
            let name = arg_str(&args, 0)?;
            let output = st.space.outputs().find(|o| o.name() == name).cloned().ok_or(format!("no output {name}"))?;
            if cmd == "set-mode" {
                let (w, h) = crate::config::parse_mode(&arg_str(&args, 1)?).ok_or("mode must be WIDTHxHEIGHT")?;
                app.backend.set_mode(&mut app.state, &output, w, h)?;
            } else {
                let scale: f64 = arg_str(&args, 1)?.parse().map_err(|_| "scale must be a number")?;
                if !(0.5..=4.0).contains(&scale) {
                    return Err("scale must be between 0.5 and 4".into());
                }
                app.state.set_output_scale(&output, scale);
            }
            Ok(json!(name))
        }
        "screenshot" => {
            let path = PathBuf::from(arg_str(&args, 0)?);
            if !path.is_absolute() {
                return Err("screenshot path must be absolute".into());
            }
            let output = match args.get(1) {
                Some(_) => {
                    let name = arg_str(&args, 1)?;
                    st.space.outputs().find(|o| o.name() == name).cloned().ok_or(format!("no output {name}"))?
                }
                None => st.space.outputs().next().cloned().ok_or("no outputs")?,
            };
            let (size, rgba) = app.backend.screenshot(&mut app.state, &output)?;
            crate::render::write_png(&path, size, &rgba)?;
            Ok(json!({"path": path, "width": size.w, "height": size.h}))
        }
        "reload-config" => {
            app.state.reload_config();
            Ok(json!(true))
        }
        "quit" => {
            app.state.loop_signal.stop();
            Ok(json!(true))
        }
        other => Err(format!("unknown command {other}")),
    }
}

/// `gideon-compositor msg CMD [ARGS...]`: send one request, print the reply.
pub fn client(args: &[String]) -> i32 {
    let Some(cmd) = args.first() else {
        eprintln!("usage: gideon-compositor msg COMMAND [ARGS...]");
        return 2;
    };
    let req = json!({"cmd": cmd, "args": args[1..]});
    let path = socket_path();
    let mut stream = match UnixStream::connect(&path) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("gideon-compositor: cannot connect to {}: {e}", path.display());
            return 1;
        }
    };
    let _ = stream.set_read_timeout(Some(Duration::from_secs(30)));
    if writeln!(stream, "{req}").is_err() {
        return 1;
    }
    let mut line = String::new();
    if BufReader::new(&stream).read_line(&mut line).is_err() {
        eprintln!("gideon-compositor: no reply");
        return 1;
    }
    let reply: Value = serde_json::from_str(&line).unwrap_or(json!({"ok": false, "error": "bad reply"}));
    if reply["ok"] == json!(true) {
        println!("{}", serde_json::to_string_pretty(&reply["result"]).unwrap_or_default());
        0
    } else {
        eprintln!("gideon-compositor: {}", reply["error"].as_str().unwrap_or("error"));
        1
    }
}
