//! Compositor state and Wayland protocol handlers.

use std::{ffi::OsString, sync::Arc, time::Instant};

use smithay::{
    backend::renderer::{element::memory::MemoryRenderBuffer, utils::on_commit_buffer_handler},
    delegate_compositor, delegate_data_device, delegate_fractional_scale, delegate_output,
    delegate_primary_selection, delegate_seat, delegate_shm, delegate_viewporter, delegate_xdg_decoration,
    delegate_xdg_shell,
    desktop::{
        find_popup_root_surface, get_popup_toplevel_coords, PopupKind, PopupManager, Space, Window,
        WindowSurfaceType,
    },
    input::{
        keyboard::XkbConfig,
        pointer::CursorImageStatus,
        Seat, SeatHandler, SeatState,
    },
    output::Output,
    reexports::{
        calloop::{generic::Generic, Interest, LoopHandle, LoopSignal, Mode, PostAction},
        wayland_protocols::xdg::{
            decoration::zv1::server::zxdg_toplevel_decoration_v1::Mode as DecorationMode,
            shell::server::xdg_toplevel,
        },
        wayland_server::{
            backend::{ClientData, ClientId, DisconnectReason},
            protocol::{wl_buffer, wl_output, wl_seat, wl_surface::WlSurface},
            Client, Display, DisplayHandle, Resource,
        },
    },
    utils::{Logical, Point, Rectangle, Serial},
    wayland::{
        buffer::BufferHandler,
        compositor::{
            get_parent, is_sync_subsurface, with_states, CompositorClientState, CompositorHandler,
            CompositorState,
        },
        fractional_scale::{with_fractional_scale, FractionalScaleHandler, FractionalScaleManagerState},
        output::{OutputHandler, OutputManagerState},
        selection::{
            data_device::{
                set_data_device_focus, ClientDndGrabHandler, DataDeviceHandler, DataDeviceState,
                ServerDndGrabHandler,
            },
            primary_selection::{set_primary_focus, PrimarySelectionHandler, PrimarySelectionState},
            SelectionHandler,
        },
        shell::xdg::{
            decoration::{XdgDecorationHandler, XdgDecorationState},
            PopupSurface, PositionerState, ToplevelSurface, XdgShellHandler, XdgShellState,
            XdgToplevelSurfaceData,
        },
        shm::{ShmHandler, ShmState},
        socket::ListeningSocketSource,
        viewporter::ViewporterState,
    },
};

use crate::{config::Config, grabs, render, App};

pub struct Gideon {
    pub display_handle: DisplayHandle,
    pub loop_handle: LoopHandle<'static, App>,
    pub loop_signal: LoopSignal,
    pub socket_name: OsString,
    pub start_time: Instant,
    pub config: Config,

    pub compositor_state: CompositorState,
    pub xdg_shell_state: XdgShellState,
    pub xdg_decoration_state: XdgDecorationState,
    pub shm_state: ShmState,
    pub output_manager_state: OutputManagerState,
    pub seat_state: SeatState<Gideon>,
    pub data_device_state: DataDeviceState,
    pub primary_selection_state: PrimarySelectionState,
    pub fractional_scale_state: FractionalScaleManagerState,
    pub viewporter_state: ViewporterState,
    pub popups: PopupManager,

    pub seat: Seat<Gideon>,
    pub space: Space<Window>,
    pub wm: crate::wm::WindowManager,

    pub cursor_status: CursorImageStatus,
    pub cursor_buffer: MemoryRenderBuffer,
    pub dnd_icon: Option<WlSurface>,
    /// Keys whose press was consumed by a shortcut; their release is swallowed too.
    pub suppressed_keys: Vec<u32>,
    /// Something changed on screen; the backend renders after this loop iteration.
    pub redraw: bool,
    /// Requests handled by the backend after this iteration (need the renderer).
    pub vt_switch: Option<i32>,
    /// Print key: the backend saves a screenshot of every output.
    pub screenshot_requested: bool,
}

impl Gideon {
    pub fn new(display: Display<Gideon>, loop_handle: LoopHandle<'static, App>, loop_signal: LoopSignal) -> Gideon {
        let dh = display.handle();
        let config = Config::load();

        let compositor_state = CompositorState::new::<Self>(&dh);
        let xdg_shell_state = XdgShellState::new::<Self>(&dh);
        let xdg_decoration_state = XdgDecorationState::new::<Self>(&dh);
        let shm_state = ShmState::new::<Self>(&dh, vec![]);
        let output_manager_state = OutputManagerState::new_with_xdg_output::<Self>(&dh);
        let data_device_state = DataDeviceState::new::<Self>(&dh);
        let primary_selection_state = PrimarySelectionState::new::<Self>(&dh);
        let fractional_scale_state = FractionalScaleManagerState::new::<Self>(&dh);
        let viewporter_state = ViewporterState::new::<Self>(&dh);

        let mut seat_state = SeatState::new();
        let mut seat: Seat<Self> = seat_state.new_wl_seat(&dh, "seat0");
        let xkb = XkbConfig { layout: &config.keyboard_layout, ..Default::default() };
        if seat.add_keyboard(xkb, 400, 30).is_err() {
            tracing::warn!(layout = %config.keyboard_layout, "invalid keyboard layout, falling back to us");
            seat.add_keyboard(XkbConfig { layout: "us", ..Default::default() }, 400, 30)
                .expect("default keymap");
        }
        seat.add_pointer();

        // Wayland socket: clients connect here (name exported as WAYLAND_DISPLAY).
        let listening = ListeningSocketSource::new_auto().expect("cannot create wayland socket");
        let socket_name = listening.socket_name().to_os_string();
        loop_handle
            .insert_source(listening, |stream, _, app: &mut App| {
                if let Err(err) = app.state.display_handle.insert_client(stream, Arc::new(ClientState::default())) {
                    tracing::warn!(?err, "failed to add client");
                }
            })
            .expect("wayland socket source");
        loop_handle
            .insert_source(Generic::new(display, Interest::READ, Mode::Level), |_, display, app: &mut App| {
                // Safety: the display is never dropped while the source exists.
                unsafe {
                    display.get_mut().dispatch_clients(&mut app.state).unwrap();
                }
                Ok(PostAction::Continue)
            })
            .expect("wayland display source");

        Gideon {
            display_handle: dh,
            loop_handle,
            loop_signal,
            socket_name,
            start_time: Instant::now(),
            config,
            compositor_state,
            xdg_shell_state,
            xdg_decoration_state,
            shm_state,
            output_manager_state,
            seat_state,
            data_device_state,
            primary_selection_state,
            fractional_scale_state,
            viewporter_state,
            popups: PopupManager::default(),
            seat,
            space: Space::default(),
            wm: Default::default(),
            cursor_status: CursorImageStatus::default_named(),
            cursor_buffer: render::cursor_buffer(),
            dnd_icon: None,
            suppressed_keys: Vec::new(),
            redraw: true,
            vt_switch: None,
            screenshot_requested: false,
        }
    }

    /// Output containing `pos`, or the first one.
    pub fn output_at(&self, pos: Point<f64, Logical>) -> Option<Output> {
        self.space.output_under(pos).next().or_else(|| self.space.outputs().next()).cloned()
    }

    pub fn output_geometry(&self, output: &Output) -> Rectangle<i32, Logical> {
        self.space.output_geometry(output).unwrap_or_default()
    }

    /// Clamp a pointer position into the area covered by outputs.
    pub fn clamp_to_outputs(&self, pos: Point<f64, Logical>) -> Point<f64, Logical> {
        if self.space.output_under(pos).next().is_some() {
            return pos;
        }
        // Nearest point on any output.
        self.space
            .outputs()
            .filter_map(|o| self.space.output_geometry(o))
            .map(|g| {
                let x = pos.x.clamp(g.loc.x as f64, (g.loc.x + g.size.w - 1) as f64);
                let y = pos.y.clamp(g.loc.y as f64, (g.loc.y + g.size.h - 1) as f64);
                Point::from((x, y))
            })
            .min_by(|a, b| {
                let da = (a.x - pos.x).powi(2) + (a.y - pos.y).powi(2);
                let db = (b.x - pos.x).powi(2) + (b.y - pos.y).powi(2);
                da.total_cmp(&db)
            })
            .unwrap_or(pos)
    }

    pub fn window_for_surface(&self, surface: &WlSurface) -> Option<Window> {
        self.wm
            .windows
            .iter()
            .find(|m| m.window.toplevel().map(|t| t.wl_surface() == surface).unwrap_or(false))
            .map(|m| m.window.clone())
    }

    /// Lay outputs out left to right (in connection order) and refit
    /// maximized/tiled/fullscreen windows to their new geometry.
    pub fn arrange_outputs(&mut self) {
        let outputs: Vec<Output> = self.space.outputs().cloned().collect();
        let mut x = 0;
        for output in outputs {
            output.change_current_state(None, None, None, Some((x, 0).into()));
            self.space.map_output(&output, (x, 0));
            x += self.space.output_geometry(&output).map(|g| g.size.w).unwrap_or(0);
        }
        let focused = self.wm.focused;
        let relayout: Vec<(u64, crate::wm::Layout)> = self
            .wm
            .windows
            .iter()
            .filter(|m| m.placed && m.layout != crate::wm::Layout::Floating)
            .map(|m| (m.id, m.layout))
            .collect();
        for (id, layout) in relayout {
            self.set_layout(id, layout);
        }
        if let Some(id) = focused {
            self.focus(id);
        }
        self.redraw = true;
    }

    pub fn set_output_scale(&mut self, output: &Output, scale: f64) {
        output.change_current_state(None, None, Some(smithay::output::Scale::Fractional(scale)), None);
        self.arrange_outputs();
        tracing::info!(output = %output.name(), scale, "output scale");
    }

    /// Re-read configuration (keyboard layout, background, terminal).
    pub fn reload_config(&mut self) {
        self.config = Config::load();
        if let Some(keyboard) = self.seat.get_keyboard() {
            let layout = self.config.keyboard_layout.clone();
            let xkb = XkbConfig { layout: &layout, ..Default::default() };
            if let Err(err) = keyboard.set_xkb_config(self, xkb) {
                tracing::warn!(?err, "keyboard layout rejected");
            }
        }
        self.redraw = true;
    }

    fn unconstrain_popup(&self, popup: &PopupSurface) {
        let Ok(root) = find_popup_root_surface(&PopupKind::Xdg(popup.clone())) else { return };
        let Some(window) = self.window_for_surface(&root) else { return };
        let Some(window_geo) = self.space.element_geometry(&window) else { return };
        let Some(output) = self.output_at(window_geo.loc.to_f64()) else { return };
        let mut target = self.output_geometry(&output);
        target.loc -= get_popup_toplevel_coords(&PopupKind::Xdg(popup.clone()));
        target.loc -= window_geo.loc;
        popup.with_pending_state(|state| {
            state.geometry = state.positioner.get_unconstrained_geometry(target);
        });
    }
}

#[derive(Default)]
pub struct ClientState {
    pub compositor_state: CompositorClientState,
}

impl ClientData for ClientState {
    fn initialized(&self, _client_id: ClientId) {}
    fn disconnected(&self, _client_id: ClientId, _reason: DisconnectReason) {}
}

// --- wl_compositor, wl_shm ---------------------------------------------------

impl CompositorHandler for Gideon {
    fn compositor_state(&mut self) -> &mut CompositorState {
        &mut self.compositor_state
    }

    fn client_compositor_state<'a>(&self, client: &'a Client) -> &'a CompositorClientState {
        &client.get_data::<ClientState>().unwrap().compositor_state
    }

    fn commit(&mut self, surface: &WlSurface) {
        on_commit_buffer_handler::<Self>(surface);
        if !is_sync_subsurface(surface) {
            let mut root = surface.clone();
            while let Some(parent) = get_parent(&root) {
                root = parent;
            }
            if let Some(window) = self.window_for_surface(&root) {
                window.on_commit();
            }
        }

        // Initial configure for new toplevels.
        if let Some(window) = self.window_for_surface(surface) {
            let initial_sent = with_states(surface, |states| {
                states
                    .data_map
                    .get::<XdgToplevelSurfaceData>()
                    .map(|d| d.lock().unwrap().initial_configure_sent)
                    .unwrap_or(true)
            });
            if !initial_sent {
                window.toplevel().unwrap().send_configure();
            }
            self.window_committed(&window);
        }

        self.popups.commit(surface);
        if let Some(PopupKind::Xdg(ref popup)) = self.popups.find_popup(surface) {
            if !popup.is_initial_configure_sent() {
                let _ = popup.send_configure();
            }
        }
        self.redraw = true;
    }
}

impl BufferHandler for Gideon {
    fn buffer_destroyed(&mut self, _buffer: &wl_buffer::WlBuffer) {}
}

impl ShmHandler for Gideon {
    fn shm_state(&self) -> &ShmState {
        &self.shm_state
    }
}

delegate_compositor!(Gideon);
delegate_shm!(Gideon);
delegate_viewporter!(Gideon);

// --- xdg-shell ------------------------------------------------------------------

impl XdgShellHandler for Gideon {
    fn xdg_shell_state(&mut self) -> &mut XdgShellState {
        &mut self.xdg_shell_state
    }

    fn new_toplevel(&mut self, surface: ToplevelSurface) {
        surface.with_pending_state(|state| {
            state.decoration_mode = Some(DecorationMode::ServerSide);
        });
        let window = Window::new_wayland_window(surface);
        self.add_window(window);
    }

    fn new_popup(&mut self, surface: PopupSurface, _positioner: PositionerState) {
        self.unconstrain_popup(&surface);
        let _ = self.popups.track_popup(PopupKind::Xdg(surface));
    }

    fn reposition_request(&mut self, surface: PopupSurface, positioner: PositionerState, token: u32) {
        surface.with_pending_state(|state| {
            state.geometry = positioner.get_geometry();
            state.positioner = positioner;
        });
        self.unconstrain_popup(&surface);
        surface.send_repositioned(token);
    }

    fn move_request(&mut self, surface: ToplevelSurface, seat: wl_seat::WlSeat, serial: Serial) {
        let Some(seat) = Seat::<Gideon>::from_resource(&seat) else { return };
        let Some(window) = self.window_for_surface(surface.wl_surface()) else { return };
        if let Some(start) = grabs::client_grab_start(&seat, surface.wl_surface(), serial) {
            self.start_move(window, start, serial);
        }
    }

    fn resize_request(
        &mut self,
        surface: ToplevelSurface,
        seat: wl_seat::WlSeat,
        serial: Serial,
        edges: xdg_toplevel::ResizeEdge,
    ) {
        let Some(seat) = Seat::<Gideon>::from_resource(&seat) else { return };
        let Some(window) = self.window_for_surface(surface.wl_surface()) else { return };
        if let Some(start) = grabs::client_grab_start(&seat, surface.wl_surface(), serial) {
            self.start_resize(window, start, grabs::ResizeEdge::from_bits_truncate(edges as u32), serial);
        }
    }

    fn grab(&mut self, _surface: PopupSurface, _seat: wl_seat::WlSeat, _serial: Serial) {
        // Popup grabs (explicit menu dismissal) are not implemented yet; popups
        // still work and close when the client dismisses them.
    }

    fn maximize_request(&mut self, surface: ToplevelSurface) {
        if let Some(id) = self.window_for_surface(surface.wl_surface()).and_then(|w| self.wm.id_of(&w)) {
            self.set_maximized(id, true);
        }
    }

    fn unmaximize_request(&mut self, surface: ToplevelSurface) {
        if let Some(id) = self.window_for_surface(surface.wl_surface()).and_then(|w| self.wm.id_of(&w)) {
            self.set_maximized(id, false);
        }
    }

    fn fullscreen_request(&mut self, surface: ToplevelSurface, _output: Option<wl_output::WlOutput>) {
        if let Some(id) = self.window_for_surface(surface.wl_surface()).and_then(|w| self.wm.id_of(&w)) {
            self.set_fullscreen(id, true);
        } else {
            // Not mapped yet (fullscreen before first commit): remember the wish.
            surface.with_pending_state(|s| s.states.set(xdg_toplevel::State::Fullscreen));
        }
    }

    fn unfullscreen_request(&mut self, surface: ToplevelSurface) {
        if let Some(id) = self.window_for_surface(surface.wl_surface()).and_then(|w| self.wm.id_of(&w)) {
            self.set_fullscreen(id, false);
        }
    }

    fn minimize_request(&mut self, surface: ToplevelSurface) {
        if let Some(id) = self.window_for_surface(surface.wl_surface()).and_then(|w| self.wm.id_of(&w)) {
            self.minimize(id);
        }
    }

    fn toplevel_destroyed(&mut self, surface: ToplevelSurface) {
        self.remove_window(surface.wl_surface());
    }

    fn title_changed(&mut self, _surface: ToplevelSurface) {
        self.redraw = true;
    }
}

delegate_xdg_shell!(Gideon);

// --- xdg-decoration: GideonOS draws window decorations itself -------------------

impl XdgDecorationHandler for Gideon {
    fn new_decoration(&mut self, toplevel: ToplevelSurface) {
        toplevel.with_pending_state(|state| state.decoration_mode = Some(DecorationMode::ServerSide));
        if toplevel.is_initial_configure_sent() {
            toplevel.send_pending_configure();
        }
    }

    fn request_mode(&mut self, toplevel: ToplevelSurface, _mode: DecorationMode) {
        self.new_decoration(toplevel);
    }

    fn unset_mode(&mut self, toplevel: ToplevelSurface) {
        self.new_decoration(toplevel);
    }
}

delegate_xdg_decoration!(Gideon);

// --- seat, clipboard, drag and drop ------------------------------------------------

impl SeatHandler for Gideon {
    type KeyboardFocus = WlSurface;
    type PointerFocus = WlSurface;
    type TouchFocus = WlSurface;

    fn seat_state(&mut self) -> &mut SeatState<Gideon> {
        &mut self.seat_state
    }

    fn cursor_image(&mut self, _seat: &Seat<Self>, image: CursorImageStatus) {
        self.cursor_status = image;
        self.redraw = true;
    }

    fn focus_changed(&mut self, seat: &Seat<Self>, focused: Option<&WlSurface>) {
        let dh = &self.display_handle;
        let client = focused.and_then(|s| dh.get_client(s.id()).ok());
        set_data_device_focus(dh, seat, client.clone());
        set_primary_focus(dh, seat, client);
    }
}

delegate_seat!(Gideon);

impl SelectionHandler for Gideon {
    type SelectionUserData = ();
}

impl DataDeviceHandler for Gideon {
    fn data_device_state(&self) -> &DataDeviceState {
        &self.data_device_state
    }
}

impl ClientDndGrabHandler for Gideon {
    fn started(&mut self, _source: Option<smithay::reexports::wayland_server::protocol::wl_data_source::WlDataSource>, icon: Option<WlSurface>, _seat: Seat<Self>) {
        self.dnd_icon = icon;
        self.redraw = true;
    }

    fn dropped(&mut self, _target: Option<WlSurface>, _validated: bool, _seat: Seat<Self>) {
        self.dnd_icon = None;
        self.redraw = true;
    }
}

impl ServerDndGrabHandler for Gideon {}

delegate_data_device!(Gideon);

impl PrimarySelectionHandler for Gideon {
    fn primary_selection_state(&self) -> &PrimarySelectionState {
        &self.primary_selection_state
    }
}

delegate_primary_selection!(Gideon);

// --- outputs and scaling ---------------------------------------------------------------

impl OutputHandler for Gideon {}
delegate_output!(Gideon);

impl FractionalScaleHandler for Gideon {
    fn new_fractional_scale(&mut self, surface: WlSurface) {
        // Preferred scale = scale of the output the surface's window is on.
        let mut root = surface.clone();
        while let Some(parent) = get_parent(&root) {
            root = parent;
        }
        let scale = self
            .window_for_surface(&root)
            .and_then(|w| self.space.outputs_for_element(&w).into_iter().next())
            .or_else(|| self.space.outputs().next().cloned())
            .map(|o| o.current_scale().fractional_scale())
            .unwrap_or(1.0);
        with_states(&surface, |states| {
            with_fractional_scale(states, |fs| fs.set_preferred_scale(scale));
        });
    }
}

delegate_fractional_scale!(Gideon);

/// Surface (and position) under a point, ignoring decorations.
pub fn surface_under(window: &Window, window_render_loc: Point<i32, Logical>, pos: Point<f64, Logical>) -> Option<(WlSurface, Point<f64, Logical>)> {
    window
        .surface_under(pos - window_render_loc.to_f64(), WindowSurfaceType::ALL)
        .map(|(s, p)| (s, (p + window_render_loc).to_f64()))
}
