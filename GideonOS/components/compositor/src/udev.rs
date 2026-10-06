//! DRM/KMS backend: libseat session, udev hotplug, libinput, and one
//! `DrmCompositor` per connected connector.
//!
//! Rendering uses the CPU (pixman) into DRM dumb buffers. This works on every
//! KMS driver (including virtio-gpu and simpledrm) and does not depend on Mesa
//! having a hardware driver for the GPU; GPU composition is a later step.

use std::{
    collections::HashMap,
    path::Path,
    time::Duration,
};

use smithay::{
    backend::{
        allocator::{
            dumb::DumbAllocator,
            gbm::GbmDevice,
            Format, Fourcc, Modifier,
        },
        drm::{
            compositor::{DrmCompositor, FrameFlags},
            DrmDevice, DrmDeviceFd, DrmEvent, DrmNode,
        },
        libinput::{LibinputInputBackend, LibinputSessionInterface},
        renderer::pixman::PixmanRenderer,
        session::{libseat::LibSeatSession, Event as SessionEvent, Session},
        udev::{UdevBackend, UdevEvent},
    },
    output::{Mode as OutputMode, Output, PhysicalProperties, Scale, Subpixel},
    reexports::{
        calloop::{
            timer::{TimeoutAction, Timer},
            EventLoop, RegistrationToken,
        },
        drm::control::{connector, crtc, Device as ControlDevice, ModeTypeFlags},
        input::Libinput,
        pixman::Image,
        rustix::fs::OFlags,
        wayland_server::backend::GlobalId,
    },
    utils::{DeviceFd, Physical, Size, Transform},
};

use crate::{render, state::Gideon, App, Backend};

type GideonDrmCompositor = DrmCompositor<DumbAllocator, DrmDeviceFd, (), DrmDeviceFd>;

struct Surface {
    output: Output,
    global: GlobalId,
    connector: connector::Handle,
    modes: Vec<smithay::reexports::drm::control::Mode>,
    compositor: GideonDrmCompositor,
    /// A frame is queued; wait for its vblank before rendering again.
    pending: bool,
    dirty: bool,
    throttle: Option<RegistrationToken>,
}

struct Device {
    drm: DrmDevice,
    fd: DrmDeviceFd,
    token: RegistrationToken,
    surfaces: HashMap<crtc::Handle, Surface>,
}

pub struct Udev {
    session: LibSeatSession,
    libinput: Libinput,
    renderer: PixmanRenderer,
    devices: HashMap<DrmNode, Device>,
    initial: Vec<(u64, std::path::PathBuf)>,
}

fn udev(app: &mut App) -> &mut Udev {
    match &mut app.backend {
        Backend::Udev(u) => u,
        #[allow(unreachable_patterns)]
        _ => unreachable!("udev callback with another backend"),
    }
}

impl Udev {
    pub fn init(event_loop: &mut EventLoop<'static, App>, state: &mut Gideon) -> Result<Udev, String> {
        let (session, notifier) = LibSeatSession::new().map_err(|e| format!("no seat session (logind/seatd): {e}"))?;
        let seat = session.seat();
        tracing::info!(%seat, "session opened");

        let udev_backend = UdevBackend::new(&seat).map_err(|e| format!("udev: {e}"))?;
        let initial = udev_backend.device_list().map(|(id, path)| (id, path.to_path_buf())).collect();

        let mut libinput = Libinput::new_with_udev::<LibinputSessionInterface<LibSeatSession>>(session.clone().into());
        libinput.udev_assign_seat(&seat).map_err(|_| "libinput: cannot assign seat".to_string())?;
        let handle = event_loop.handle();
        handle
            .insert_source(LibinputInputBackend::new(libinput.clone()), |event, _, app| {
                app.state.process_input(event);
                if let Some(vt) = app.state.vt_switch.take() {
                    if let Err(err) = udev(app).session.change_vt(vt) {
                        tracing::warn!(%err, vt, "VT switch failed");
                    }
                }
            })
            .map_err(|e| e.to_string())?;

        handle
            .insert_source(notifier, |event, _, app| match event {
                SessionEvent::PauseSession => {
                    tracing::info!("session paused (VT switched away)");
                    let u = udev(app);
                    u.libinput.suspend();
                    for device in u.devices.values_mut() {
                        device.drm.pause();
                    }
                }
                SessionEvent::ActivateSession => {
                    tracing::info!("session resumed");
                    let u = udev(app);
                    if u.libinput.resume().is_err() {
                        tracing::warn!("libinput resume failed");
                    }
                    for device in u.devices.values_mut() {
                        if let Err(err) = device.drm.activate(false) {
                            tracing::warn!(%err, "DRM activate failed");
                        }
                        for surface in device.surfaces.values_mut() {
                            surface.compositor.reset_buffers();
                            surface.pending = false;
                            surface.dirty = true;
                        }
                    }
                    app.state.redraw = true;
                }
            })
            .map_err(|e| e.to_string())?;

        handle
            .insert_source(udev_backend, |event, _, app| match event {
                UdevEvent::Added { device_id, path } => device_added(app, device_id, &path),
                UdevEvent::Changed { device_id } => {
                    if let Ok(node) = DrmNode::from_dev_id(device_id) {
                        scan_connectors(app, node);
                    }
                }
                UdevEvent::Removed { device_id } => {
                    if let Ok(node) = DrmNode::from_dev_id(device_id) {
                        device_removed(app, node);
                    }
                }
            })
            .map_err(|e| e.to_string())?;

        let _ = state;
        Ok(Udev { session, libinput, renderer: PixmanRenderer::new().map_err(|e| format!("pixman: {e:?}"))?, devices: HashMap::new(), initial })
    }

    pub fn modes(&self, output: &Output) -> Vec<(i32, i32, i32)> {
        self.devices
            .values()
            .flat_map(|d| d.surfaces.values())
            .find(|s| &s.output == output)
            .map(|s| s.modes.iter().map(|m| (m.size().0 as i32, m.size().1 as i32, OutputMode::from(*m).refresh)).collect())
            .unwrap_or_default()
    }

    pub fn set_mode(&mut self, state: &mut Gideon, output: &Output, w: u16, h: u16) -> Result<(), String> {
        let surface = self
            .devices
            .values_mut()
            .flat_map(|d| d.surfaces.values_mut())
            .find(|s| &s.output == output)
            .ok_or("output not driven by this backend")?;
        let mode = pick_mode(&surface.modes, Some((w, h))).ok_or("mode not supported by the display")?;
        if mode.size() != (w, h) {
            return Err(format!("{w}x{h} is not offered by the display"));
        }
        surface.compositor.use_mode(mode).map_err(|e| format!("{e}"))?;
        output.change_current_state(Some(OutputMode::from(mode)), None, None, None);
        surface.dirty = true;
        state.arrange_outputs();
        tracing::info!(output = %output.name(), w, h, "mode changed");
        Ok(())
    }

    pub fn screenshot(&mut self, state: &mut Gideon, output: &Output) -> Result<(Size<i32, Physical>, Vec<u8>), String> {
        let size = output.current_mode().ok_or("output has no mode")?.size;
        let scale = output.current_scale().fractional_scale();
        let (elements, _) = render::output_elements(state, &mut self.renderer, output);
        let clear = render::clear_color(state);
        let rgba = render::capture::<_, Image<'static, 'static>>(&mut self.renderer, &elements, size, scale, clear)?;
        Ok((size, rgba))
    }

    pub fn after_dispatch(&mut self, state: &mut Gideon) {
        if let Some(vt) = state.vt_switch.take() {
            if let Err(err) = self.session.change_vt(vt) {
                tracing::warn!(%err, vt, "VT switch failed");
            }
        }
        if !self.session.is_active() {
            return;
        }
        if state.redraw {
            state.redraw = false;
            for surface in self.devices.values_mut().flat_map(|d| d.surfaces.values_mut()) {
                surface.dirty = true;
            }
        }
        let Udev { devices, renderer, .. } = self;
        for surface in devices.values_mut().flat_map(|d| d.surfaces.values_mut()) {
            if surface.dirty && !surface.pending {
                render_surface(state, renderer, surface);
            }
        }
    }
}

/// Mode for an output: the requested size (highest refresh), else the
/// connector's preferred mode, else the first.
fn pick_mode(modes: &[smithay::reexports::drm::control::Mode], want: Option<(u16, u16)>) -> Option<smithay::reexports::drm::control::Mode> {
    if let Some(want) = want {
        if let Some(m) = modes.iter().filter(|m| m.size() == want).max_by_key(|m| m.vrefresh()) {
            return Some(*m);
        }
    }
    modes
        .iter()
        .find(|m| m.mode_type().contains(ModeTypeFlags::PREFERRED))
        .or_else(|| modes.first())
        .copied()
}

fn render_surface(state: &mut Gideon, renderer: &mut PixmanRenderer, surface: &mut Surface) {
    surface.dirty = false;
    let (elements, animating) = render::output_elements(state, renderer, &surface.output);
    let clear = render::clear_color(state);
    let queued = match surface.compositor.render_frame(renderer, &elements, clear, FrameFlags::DEFAULT) {
        Ok(result) if !result.is_empty => match surface.compositor.queue_frame(()) {
            Ok(()) => true,
            Err(err) => {
                tracing::warn!(%err, "queue_frame failed");
                false
            }
        },
        Ok(_) => false,
        Err(err) => {
            tracing::warn!(?err, "render_frame failed");
            false
        }
    };
    if animating {
        state.redraw = true;
    }
    if queued {
        surface.pending = true;
        send_frames(state, &surface.output);
    } else if surface.throttle.is_none() {
        // Nothing changed on screen: answer frame callbacks at roughly the
        // refresh rate instead of immediately, so idle clients do not spin.
        let output = surface.output.clone();
        let refresh = output.current_mode().map(|m| m.refresh).unwrap_or(60_000).max(1000);
        let delay = Duration::from_micros(1_000_000_000 / refresh as u64);
        let token = state
            .loop_handle
            .insert_source(Timer::from_duration(delay), move |_, _, app| {
                for s in udev(app).devices.values_mut().flat_map(|d| d.surfaces.values_mut()) {
                    if s.output == output {
                        s.throttle = None;
                    }
                }
                send_frames(&mut app.state, &output);
                TimeoutAction::Drop
            })
            .ok();
        surface.throttle = token;
    }
}

fn send_frames(state: &mut Gideon, output: &Output) {
    let time = state.start_time.elapsed();
    for window in state.space.elements() {
        if state.space.outputs_for_element(window).contains(output) {
            window.send_frame(output, time, Some(Duration::ZERO), |_, _| Some(output.clone()));
        }
    }
    if let smithay::input::pointer::CursorImageStatus::Surface(surface) = &state.cursor_status {
        smithay::desktop::utils::send_frames_surface_tree(surface, output, time, Some(Duration::ZERO), |_, _| Some(output.clone()));
    }
}

pub fn add_initial_devices(app: &mut App) {
    let initial = std::mem::take(&mut udev(app).initial);
    for (id, path) in initial {
        device_added(app, id, &path);
    }
    if udev(app).devices.is_empty() {
        tracing::error!("no usable DRM device found");
    }
}

fn device_added(app: &mut App, device_id: u64, path: &Path) {
    let Ok(node) = DrmNode::from_dev_id(device_id) else { return };
    if udev(app).devices.contains_key(&node) {
        return;
    }
    let fd = match udev(app).session.open(path, OFlags::RDWR | OFlags::CLOEXEC | OFlags::NOCTTY | OFlags::NONBLOCK) {
        Ok(fd) => DrmDeviceFd::new(DeviceFd::from(fd)),
        Err(err) => {
            tracing::warn!(%err, path = %path.display(), "cannot open DRM device");
            return;
        }
    };
    let (drm, notifier) = match DrmDevice::new(fd.clone(), true) {
        Ok(v) => v,
        Err(err) => {
            tracing::warn!(%err, path = %path.display(), "not a usable KMS device");
            return;
        }
    };
    let token = match app.state.loop_handle.insert_source(notifier, move |event, _, app| match event {
        DrmEvent::VBlank(crtc) => {
            let u = udev(app);
            if let Some(surface) = u.devices.get_mut(&node).and_then(|d| d.surfaces.get_mut(&crtc)) {
                if let Err(err) = surface.compositor.frame_submitted() {
                    tracing::warn!(%err, "frame_submitted failed");
                }
                surface.pending = false;
            }
        }
        DrmEvent::Error(err) => tracing::warn!(%err, "DRM error"),
    }) {
        Ok(t) => t,
        Err(err) => {
            tracing::warn!(%err, "cannot watch DRM device");
            return;
        }
    };
    tracing::info!(%node, path = %path.display(), "DRM device added");
    udev(app).devices.insert(node, Device { drm, fd, token, surfaces: HashMap::new() });
    scan_connectors(app, node);
}

fn device_removed(app: &mut App, node: DrmNode) {
    let Some(device) = udev(app).devices.remove(&node) else { return };
    for surface in device.surfaces.into_values() {
        app.state.space.unmap_output(&surface.output);
        app.state.display_handle.remove_global::<Gideon>(surface.global);
    }
    app.state.loop_handle.remove(device.token);
    app.state.arrange_outputs();
    tracing::info!(%node, "DRM device removed");
}

/// Compare connector state with our outputs: set up new ones, drop gone ones.
fn scan_connectors(app: &mut App, node: DrmNode) {
    let had_outputs = app.state.space.outputs().next().is_some();
    let config_mode = app.state.config.mode;
    let config_scale = app.state.config.scale;
    let Some(device) = udev(app).devices.get_mut(&node) else { return };
    let Ok(res) = device.drm.resource_handles() else { return };

    let mut connected = Vec::new();
    for handle in res.connectors() {
        if let Ok(info) = device.drm.get_connector(*handle, true) {
            if info.state() == connector::State::Connected && !info.modes().is_empty() {
                connected.push(info);
            }
        }
    }

    // Removed connectors.
    let gone: Vec<crtc::Handle> = device
        .surfaces
        .iter()
        .filter(|(_, s)| !connected.iter().any(|c| c.handle() == s.connector))
        .map(|(crtc, _)| *crtc)
        .collect();
    let mut removed = Vec::new();
    for crtc in gone {
        if let Some(surface) = device.surfaces.remove(&crtc) {
            tracing::info!(output = %surface.output.name(), "output disconnected");
            removed.push((surface.output, surface.global));
        }
    }

    // New connectors.
    let mut added = Vec::new();
    for info in connected {
        if device.surfaces.values().any(|s| s.connector == info.handle()) {
            continue;
        }
        let used: Vec<crtc::Handle> = device.surfaces.keys().copied().collect();
        let crtc = info
            .encoders()
            .iter()
            .filter_map(|e| device.drm.get_encoder(*e).ok())
            .flat_map(|enc| res.filter_crtcs(enc.possible_crtcs()))
            .find(|c| !used.contains(c));
        let Some(crtc) = crtc else {
            tracing::warn!(connector = ?info.handle(), "no free CRTC");
            continue;
        };
        let modes = info.modes().to_vec();
        let Some(mode) = pick_mode(&modes, config_mode) else { continue };
        let name = format!("{}-{}", info.interface().as_str(), info.interface_id());
        let drm_surface = match device.drm.create_surface(crtc, mode, &[info.handle()]) {
            Ok(s) => s,
            Err(err) => {
                tracing::warn!(%err, %name, "cannot create DRM surface");
                continue;
            }
        };
        let (mm_w, mm_h) = info.size().unwrap_or((0, 0));
        let output = Output::new(
            name.clone(),
            PhysicalProperties {
                size: (mm_w as i32, mm_h as i32).into(),
                subpixel: Subpixel::Unknown,
                make: "Unknown".into(),
                model: name.clone(),
            },
        );
        let preferred = pick_mode(&modes, None).map(OutputMode::from);
        if let Some(p) = preferred {
            output.set_preferred(p);
        }
        output.change_current_state(Some(OutputMode::from(mode)), Some(Transform::Normal), Some(Scale::Fractional(config_scale)), None);

        let formats = [
            Format { code: Fourcc::Xrgb8888, modifier: Modifier::Linear },
            Format { code: Fourcc::Argb8888, modifier: Modifier::Linear },
        ];
        let compositor = match DrmCompositor::new(
            &output,
            drm_surface,
            None,
            DumbAllocator::new(device.fd.clone()),
            device.fd.clone(),
            [Fourcc::Xrgb8888, Fourcc::Argb8888],
            formats,
            device.drm.cursor_size(),
            None::<GbmDevice<DrmDeviceFd>>,
        ) {
            Ok(c) => c,
            Err(err) => {
                tracing::warn!(?err, %name, "cannot create DRM compositor");
                continue;
            }
        };
        let (w, h) = mode.size();
        tracing::info!(output = %name, w, h, refresh = mode.vrefresh(), scale = config_scale, "output connected");
        added.push((crtc, output, info.handle(), modes, compositor));
    }

    for (output, global) in removed {
        app.state.space.unmap_output(&output);
        app.state.display_handle.remove_global::<Gideon>(global);
    }
    for (crtc, output, connector, modes, compositor) in added {
        let global = output.create_global::<Gideon>(&app.state.display_handle);
        app.state.space.map_output(&output, (i32::MAX / 4, 0));
        let surface = Surface { output, global, connector, modes, compositor, pending: false, dirty: true, throttle: None };
        if let Some(device) = udev(app).devices.get_mut(&node) {
            device.surfaces.insert(crtc, surface);
        }
    }
    app.state.arrange_outputs();
    // Start the pointer in the middle of the first output.
    if !had_outputs {
        if let (Some(pointer), Some(g)) = (
            app.state.seat.get_pointer(),
            app.state.space.outputs().next().and_then(|o| app.state.space.output_geometry(o)),
        ) {
            let centre = g.loc.to_f64() + smithay::utils::Point::<f64, smithay::utils::Logical>::from((g.size.w as f64 / 2.0, g.size.h as f64 / 2.0));
            pointer.set_location(centre);
        }
    }
    app.state.redraw = true;
}
