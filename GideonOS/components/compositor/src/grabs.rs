//! Interactive move and resize (pointer grabs).

use smithay::{
    desktop::Window,
    input::{
        pointer::{
            AxisFrame, ButtonEvent, Focus, GestureHoldBeginEvent, GestureHoldEndEvent, GesturePinchBeginEvent,
            GesturePinchEndEvent, GesturePinchUpdateEvent, GestureSwipeBeginEvent, GestureSwipeEndEvent,
            GestureSwipeUpdateEvent, GrabStartData, MotionEvent, PointerGrab, PointerInnerHandle,
            RelativeMotionEvent,
        },
        Seat,
    },
    reexports::{
        wayland_protocols::xdg::shell::server::xdg_toplevel::State as XdgState,
        wayland_server::{protocol::wl_surface::WlSurface, Resource},
    },
    utils::{Logical, Point, Rectangle, Serial, Size},
};

use crate::{
    state::Gideon,
    wm::{Layout, ResizeState},
};

/// Resize edges (same bit values as xdg_toplevel.resize_edge).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ResizeEdge(u32);

impl ResizeEdge {
    pub const TOP: Self = Self(1);
    pub const BOTTOM: Self = Self(2);
    pub const LEFT: Self = Self(4);
    pub const RIGHT: Self = Self(8);
    pub const BOTTOM_RIGHT: Self = Self(2 | 8);

    pub fn from_bits_truncate(bits: u32) -> Self {
        Self(bits & 0xf)
    }
    pub fn intersects(self, other: Self) -> bool {
        self.0 & other.0 != 0
    }
}

/// Validate a client move/resize request against the current pointer grab.
pub fn client_grab_start(seat: &Seat<Gideon>, surface: &WlSurface, serial: Serial) -> Option<GrabStartData<Gideon>> {
    let pointer = seat.get_pointer()?;
    if !pointer.has_grab(serial) {
        return None;
    }
    let start = pointer.grab_start_data()?;
    let (focus, _) = start.focus.as_ref()?;
    if !focus.id().same_client_as(&surface.id()) {
        return None;
    }
    Some(start)
}

/// Grab start data for compositor-initiated grabs (decorations, Super+drag).
pub fn compositor_grab_start(state: &Gideon, button: u32) -> GrabStartData<Gideon> {
    let location = state.seat.get_pointer().map(|p| p.current_location()).unwrap_or_default();
    GrabStartData { focus: None, button, location }
}

const BTN_LEFT: u32 = 0x110;
const BTN_RIGHT: u32 = 0x111;

impl Gideon {
    pub fn start_move(&mut self, window: Window, start: GrabStartData<Gideon>, serial: Serial) {
        let Some(id) = self.wm.id_of(&window) else { return };
        let layout = self.wm.get(id).map(|m| m.layout);
        if layout == Some(Layout::Fullscreen) {
            return;
        }
        let initial = self.space.element_location(&window).unwrap_or_default();
        self.focus(id);
        // A maximized/tiled window is restored only once the pointer really
        // moves (so a click or double-click on its title bar does not restore it).
        let detach = layout != Some(Layout::Floating);
        if let Some(pointer) = self.seat.get_pointer() {
            pointer.set_grab(self, MoveGrab { start, window, initial, detach }, serial, Focus::Clear);
        }
    }

    pub fn start_resize(&mut self, window: Window, start: GrabStartData<Gideon>, edges: ResizeEdge, serial: Serial) {
        let Some(id) = self.wm.id_of(&window) else { return };
        if self.wm.get(id).map(|m| m.layout != Layout::Floating).unwrap_or(true) {
            return;
        }
        let loc = self.space.element_location(&window).unwrap_or_default();
        let initial = Rectangle::new(loc, window.geometry().size);
        if let Some(m) = self.wm.get_mut(id) {
            m.resize = Some(ResizeState { edges, initial, finishing: false });
        }
        if let Some(t) = window.toplevel() {
            t.with_pending_state(|s| {
                s.states.set(XdgState::Resizing);
            });
            t.send_pending_configure();
        }
        self.focus(id);
        if let Some(pointer) = self.seat.get_pointer() {
            pointer.set_grab(self, ResizeGrab { start, window, edges, initial }, serial, Focus::Clear);
        }
    }
}

/// Forward the events a grab does not change.
macro_rules! forward_rest {
    () => {
        fn relative_motion(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, focus: Option<(WlSurface, Point<f64, Logical>)>, event: &RelativeMotionEvent) {
            handle.relative_motion(data, focus, event);
        }
        fn axis(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, details: AxisFrame) {
            handle.axis(data, details)
        }
        fn frame(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>) {
            handle.frame(data);
        }
        fn gesture_swipe_begin(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GestureSwipeBeginEvent) {
            handle.gesture_swipe_begin(data, event)
        }
        fn gesture_swipe_update(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GestureSwipeUpdateEvent) {
            handle.gesture_swipe_update(data, event)
        }
        fn gesture_swipe_end(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GestureSwipeEndEvent) {
            handle.gesture_swipe_end(data, event)
        }
        fn gesture_pinch_begin(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GesturePinchBeginEvent) {
            handle.gesture_pinch_begin(data, event)
        }
        fn gesture_pinch_update(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GesturePinchUpdateEvent) {
            handle.gesture_pinch_update(data, event)
        }
        fn gesture_pinch_end(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GesturePinchEndEvent) {
            handle.gesture_pinch_end(data, event)
        }
        fn gesture_hold_begin(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GestureHoldBeginEvent) {
            handle.gesture_hold_begin(data, event)
        }
        fn gesture_hold_end(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &GestureHoldEndEvent) {
            handle.gesture_hold_end(data, event)
        }
        fn start_data(&self) -> &GrabStartData<Gideon> {
            &self.start
        }
    };
}

pub struct MoveGrab {
    start: GrabStartData<Gideon>,
    window: Window,
    initial: Point<i32, Logical>,
    detach: bool,
}

impl PointerGrab<Gideon> for MoveGrab {
    fn motion(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, _focus: Option<(WlSurface, Point<f64, Logical>)>, event: &MotionEvent) {
        handle.motion(data, None, event);
        let delta = event.location - self.start.location;
        if self.detach {
            if delta.x.abs() + delta.y.abs() < 8.0 {
                return;
            }
            // Drag-to-restore: floating size, title bar centred under the pointer.
            let Some(id) = data.wm.id_of(&self.window) else { return };
            // Read the floating width before set_layout consumes the saved geometry.
            let w = data.wm.get(id).and_then(|m| m.restore).map(|r| r.size.w).unwrap_or_else(|| self.window.geometry().size.w);
            data.set_layout(id, Layout::Floating);
            self.initial = (self.start.location.x as i32 - w / 2, self.start.location.y as i32 + crate::wm::TITLE_H / 2).into();
            self.detach = false;
        }
        let mut loc = (self.initial.to_f64() + delta).to_i32_round::<i32>();
        // Keep the title bar reachable (never above the top of the outputs).
        let top = data.space.outputs().filter_map(|o| data.space.output_geometry(o)).map(|g| g.loc.y).min().unwrap_or(0);
        loc.y = loc.y.max(top + crate::wm::TITLE_H);
        if let Some(id) = data.wm.id_of(&self.window) {
            data.move_window(id, loc);
        }
    }

    fn button(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &ButtonEvent) {
        handle.button(data, event);
        if !handle.current_pressed().contains(&self.start.button) {
            handle.unset_grab(self, data, event.serial, event.time, true);
        }
    }

    fn unset(&mut self, data: &mut Gideon) {
        data.redraw = true;
    }

    forward_rest!();
}

pub struct ResizeGrab {
    start: GrabStartData<Gideon>,
    window: Window,
    edges: ResizeEdge,
    initial: Rectangle<i32, Logical>,
}

impl PointerGrab<Gideon> for ResizeGrab {
    fn motion(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, _focus: Option<(WlSurface, Point<f64, Logical>)>, event: &MotionEvent) {
        handle.motion(data, None, event);
        let delta = event.location - self.start.location;
        let (mut w, mut h) = (self.initial.size.w as f64, self.initial.size.h as f64);
        if self.edges.intersects(ResizeEdge::LEFT) {
            w -= delta.x;
        }
        if self.edges.intersects(ResizeEdge::RIGHT) {
            w += delta.x;
        }
        if self.edges.intersects(ResizeEdge::TOP) {
            h -= delta.y;
        }
        if self.edges.intersects(ResizeEdge::BOTTOM) {
            h += delta.y;
        }
        let size: Size<i32, Logical> = ((w as i32).max(120), (h as i32).max(60)).into();
        if let Some(t) = self.window.toplevel() {
            t.with_pending_state(|s| s.size = Some(size));
            t.send_pending_configure();
        }
    }

    fn button(&mut self, data: &mut Gideon, handle: &mut PointerInnerHandle<'_, Gideon>, event: &ButtonEvent) {
        handle.button(data, event);
        if !handle.current_pressed().contains(&self.start.button) {
            handle.unset_grab(self, data, event.serial, event.time, true);
        }
    }

    fn unset(&mut self, data: &mut Gideon) {
        if let Some(t) = self.window.toplevel() {
            t.with_pending_state(|s| {
                s.states.unset(XdgState::Resizing);
            });
            t.send_pending_configure();
        }
        if let Some(m) = data.wm.by_window_mut(&self.window) {
            if let Some(rs) = m.resize.as_mut() {
                rs.finishing = true;
            }
        }
        data.redraw = true;
    }

    forward_rest!();
}

pub const MOVE_BUTTON: u32 = BTN_LEFT;
pub const RESIZE_BUTTON: u32 = BTN_RIGHT;
