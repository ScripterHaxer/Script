//! gideon-compositor — the GideonOS Wayland compositor.
//!
//!   gideon-compositor [--backend udev|winit] [--startup COMMAND]...
//!   gideon-compositor msg COMMAND [ARGS...]      (IPC client, see ipc.rs)
//!
//! The udev backend drives displays through DRM/KMS with a CPU (pixman)
//! renderer into dumb buffers, so it works on any GPU and in VMs. The winit
//! backend (feature "winit") runs nested in a window for development.

mod config;
mod grabs;
mod input;
mod ipc;
mod render;
mod state;
mod udev;
#[cfg(feature = "winit")]
mod winit;
mod wm;

use std::{ffi::OsStr, process::Command};

use smithay::{
    output::Output,
    reexports::{calloop::EventLoop, wayland_server::Display},
    utils::{Physical, Size},
};

use crate::state::Gideon;

pub struct App {
    pub state: Gideon,
    pub backend: Backend,
}

// Only one backend value exists per process, so the size difference is irrelevant.
#[allow(clippy::large_enum_variant)]
pub enum Backend {
    Udev(udev::Udev),
    #[cfg(feature = "winit")]
    Winit(winit::WinitBackend),
}

impl Backend {
    pub fn name(&self) -> &'static str {
        match self {
            Backend::Udev(_) => "udev",
            #[cfg(feature = "winit")]
            Backend::Winit(_) => "winit",
        }
    }

    /// Modes an output supports: (width, height, refresh mHz).
    pub fn modes(&self, output: &Output) -> Vec<(i32, i32, i32)> {
        match self {
            Backend::Udev(u) => u.modes(output),
            #[cfg(feature = "winit")]
            Backend::Winit(_) => output.current_mode().map(|m| vec![(m.size.w, m.size.h, m.refresh)]).unwrap_or_default(),
        }
    }

    pub fn set_mode(&mut self, state: &mut Gideon, output: &Output, w: u16, h: u16) -> Result<(), String> {
        match self {
            Backend::Udev(u) => u.set_mode(state, output, w, h),
            #[cfg(feature = "winit")]
            Backend::Winit(_) => Err("the nested backend follows its window size".into()),
        }
    }

    pub fn screenshot(&mut self, state: &mut Gideon, output: &Output) -> Result<(Size<i32, Physical>, Vec<u8>), String> {
        match self {
            Backend::Udev(u) => u.screenshot(state, output),
            #[cfg(feature = "winit")]
            Backend::Winit(w) => w.screenshot(state, output),
        }
    }

    /// Runs after every event-loop dispatch: VT switches, screenshots, rendering.
    fn after_dispatch(&mut self, state: &mut Gideon) {
        if state.screenshot_requested {
            state.screenshot_requested = false;
            self.save_screenshots(state);
        }
        match self {
            Backend::Udev(u) => u.after_dispatch(state),
            #[cfg(feature = "winit")]
            Backend::Winit(w) => w.after_dispatch(state),
        }
    }

    /// Print key: one PNG per output in ~/Pictures.
    fn save_screenshots(&mut self, state: &mut Gideon) {
        let dir = std::path::PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/tmp".into())).join("Pictures");
        let _ = std::fs::create_dir_all(&dir);
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or_default();
        let outputs: Vec<Output> = state.space.outputs().cloned().collect();
        for output in outputs {
            let path = dir.join(format!("Screenshot-{stamp}-{}.png", output.name()));
            match self.screenshot(state, &output).and_then(|(size, rgba)| render::write_png(&path, size, &rgba)) {
                Ok(()) => tracing::info!(path = %path.display(), "screenshot saved"),
                Err(err) => tracing::warn!(%err, "screenshot failed"),
            }
        }
    }
}

/// Start a program for the user (double-forked through sh so it is not our zombie).
pub fn spawn(command: &str, wayland_display: &OsStr) {
    tracing::info!(%command, "spawn");
    let result = Command::new("/bin/sh")
        .arg("-c")
        .arg(format!("{command} </dev/null &"))
        .env("WAYLAND_DISPLAY", wayland_display)
        .status();
    if let Err(err) = result {
        tracing::warn!(%err, %command, "spawn failed");
    }
}

fn usage() -> ! {
    eprintln!("usage: gideon-compositor [--backend udev|winit] [--startup COMMAND]...\n       gideon-compositor msg COMMAND [ARGS...]");
    std::process::exit(2);
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("msg") {
        std::process::exit(ipc::client(&args[1..]));
    }

    let mut backend_name = if cfg!(feature = "winit") && (std::env::var_os("WAYLAND_DISPLAY").is_some() || std::env::var_os("DISPLAY").is_some()) {
        "winit"
    } else {
        "udev"
    }
    .to_string();
    let mut startup = Vec::new();
    let mut it = args.into_iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--backend" => backend_name = it.next().unwrap_or_else(|| usage()),
            "--startup" => startup.push(it.next().unwrap_or_else(|| usage())),
            "-h" | "--help" => usage(),
            "--version" => {
                println!("gideon-compositor {}", env!("CARGO_PKG_VERSION"));
                return;
            }
            _ => usage(),
        }
    }

    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .with_ansi(false)
        .init();
    tracing::info!(version = env!("CARGO_PKG_VERSION"), backend = %backend_name, "starting gideon-compositor");

    let mut event_loop: EventLoop<'static, App> = EventLoop::try_new().expect("event loop");
    let display: Display<Gideon> = Display::new().expect("wayland display");
    let mut state = Gideon::new(display, event_loop.handle(), event_loop.get_signal());

    let backend = match backend_name.as_str() {
        "udev" => match udev::Udev::init(&mut event_loop, &mut state) {
            Ok(u) => Backend::Udev(u),
            Err(err) => {
                tracing::error!(%err, "cannot start the DRM/KMS backend");
                std::process::exit(1);
            }
        },
        #[cfg(feature = "winit")]
        "winit" => match winit::WinitBackend::init(&mut event_loop, &mut state) {
            Ok(w) => Backend::Winit(w),
            Err(err) => {
                tracing::error!(%err, "cannot start the nested backend");
                std::process::exit(1);
            }
        },
        other => {
            eprintln!("unknown backend {other}");
            usage();
        }
    };

    let mut app = App { state, backend };
    if matches!(app.backend, Backend::Udev(_)) {
        udev::add_initial_devices(&mut app);
    }

    // Environment for everything started from the session.
    std::env::set_var("WAYLAND_DISPLAY", &app.state.socket_name);
    std::env::set_var("XDG_SESSION_TYPE", "wayland");
    std::env::set_var("XDG_CURRENT_DESKTOP", "GideonOS");
    std::env::remove_var("DISPLAY");
    match ipc::listen(&event_loop.handle()) {
        Ok(path) => {
            std::env::set_var("GIDEON_COMPOSITOR_SOCKET", &path);
            tracing::info!(socket = %path.display(), "IPC listening");
        }
        Err(err) => tracing::warn!(%err, "IPC disabled"),
    }
    tracing::info!(wayland_display = ?app.state.socket_name, "ready");

    // Clean shutdown on SIGTERM/SIGINT (session logout, systemd): exit 0.
    match calloop::signals::Signals::new(&[calloop::signals::Signal::SIGTERM, calloop::signals::Signal::SIGINT]) {
        Ok(signals) => {
            let _ = event_loop.handle().insert_source(signals, |event, _, app| {
                tracing::info!(signal = ?event.signal(), "shutting down");
                app.state.loop_signal.stop();
            });
        }
        Err(err) => tracing::warn!(%err, "cannot handle termination signals"),
    }

    for command in &startup {
        spawn(command, &app.state.socket_name);
    }

    let result = event_loop.run(None, &mut app, |app| {
        app.backend.after_dispatch(&mut app.state);
        app.state.space.refresh();
        app.state.popups.cleanup();
        let _ = app.state.display_handle.flush_clients();
    });
    let _ = std::fs::remove_file(ipc::socket_path());
    if let Err(err) = result {
        tracing::error!(%err, "event loop failed");
        std::process::exit(1);
    }
    tracing::info!("exiting");
}
