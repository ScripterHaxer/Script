//! Input routing: keyboard shortcuts, pointer focus, decorations, grabs.

use std::time::{Duration, Instant};

use smithay::{
    backend::input::{
        AbsolutePositionEvent, Axis, AxisSource, ButtonState, Event, InputBackend, InputEvent, KeyState,
        KeyboardKeyEvent, PointerAxisEvent, PointerButtonEvent, PointerMotionEvent,
    },
    input::{
        keyboard::{xkb::keysyms as ks, FilterResult, ModifiersState},
        pointer::{AxisFrame, ButtonEvent, MotionEvent, RelativeMotionEvent},
    },
    utils::{Logical, Point, SERIAL_COUNTER},
};

use crate::{
    grabs::{self, ResizeEdge},
    state::Gideon,
    wm::{DecoPart, Hit},
};

/// Compositor actions bound to keys (docs/DESIGN.md §9).
#[derive(Debug, Clone, PartialEq)]
pub enum Action {
    Quit,
    VtSwitch(i32),
    Terminal,
    Close,
    ToggleMaximize,
    MinimizeOrRestore,
    Fullscreen,
    TileLeft,
    TileRight,
    Workspace(usize),
    MoveToWorkspace(usize),
    WorkspaceRelative(i32),
    CycleWindows,
    Screenshot,
}

/// Map a key press to an action. `sym` is the layout-independent (latin) keysym,
/// `modified` the keysym after modifiers (for VT switching).
pub fn shortcut(mods: &ModifiersState, sym: u32, modified: u32) -> Option<Action> {
    if (ks::KEY_XF86Switch_VT_1..=ks::KEY_XF86Switch_VT_12).contains(&modified) {
        return Some(Action::VtSwitch((modified - ks::KEY_XF86Switch_VT_1 + 1) as i32));
    }
    let digit = match sym {
        ks::KEY_1..=ks::KEY_9 => Some((sym - ks::KEY_1) as usize),
        _ => None,
    };
    if mods.logo {
        if let Some(n) = digit {
            return Some(if mods.shift { Action::MoveToWorkspace(n) } else { Action::Workspace(n) });
        }
        return match sym {
            ks::KEY_Return => Some(Action::Terminal),
            ks::KEY_q => Some(Action::Close),
            ks::KEY_Up => Some(Action::ToggleMaximize),
            ks::KEY_Down => Some(Action::MinimizeOrRestore),
            ks::KEY_Left if mods.ctrl => Some(Action::WorkspaceRelative(-1)),
            ks::KEY_Right if mods.ctrl => Some(Action::WorkspaceRelative(1)),
            ks::KEY_Left => Some(Action::TileLeft),
            ks::KEY_Right => Some(Action::TileRight),
            ks::KEY_f => Some(Action::Fullscreen),
            ks::KEY_Tab => Some(Action::CycleWindows),
            ks::KEY_Escape if mods.shift => Some(Action::Quit),
            _ => None,
        };
    }
    if mods.alt && sym == ks::KEY_Tab {
        return Some(Action::CycleWindows);
    }
    if mods.alt && sym == ks::KEY_F4 {
        return Some(Action::Close);
    }
    if sym == ks::KEY_Print {
        return Some(Action::Screenshot);
    }
    None
}

impl Gideon {
    pub fn process_input<B: InputBackend>(&mut self, event: InputEvent<B>) {
        match event {
            InputEvent::Keyboard { event } => self.on_key::<B>(event),
            InputEvent::PointerMotion { event } => {
                let pointer = self.seat.get_pointer().unwrap();
                let pos = self.clamp_to_outputs(pointer.current_location() + event.delta());
                self.pointer_moved(pos, event.time_msec());
                let under = self.focus_under(pos);
                pointer.relative_motion(
                    self,
                    under,
                    &RelativeMotionEvent {
                        delta: event.delta(),
                        delta_unaccel: event.delta_unaccel(),
                        utime: event.time(),
                    },
                );
                pointer.frame(self);
            }
            InputEvent::PointerMotionAbsolute { event } => {
                // Absolute devices (tablets, touch screens, VM tablets) map onto the first output.
                let Some(output) = self.space.outputs().next().cloned() else { return };
                let geo = self.output_geometry(&output);
                let pos = event.position_transformed(geo.size) + geo.loc.to_f64();
                self.pointer_moved(pos, event.time_msec());
                self.seat.get_pointer().unwrap().frame(self);
            }
            InputEvent::PointerButton { event } => self.on_button(event.button_code(), event.state(), event.time_msec()),
            InputEvent::PointerAxis { event } => {
                let source = event.source();
                let mut frame = AxisFrame::new(event.time_msec()).source(source);
                for axis in [Axis::Horizontal, Axis::Vertical] {
                    let amount = event
                        .amount(axis)
                        .unwrap_or_else(|| event.amount_v120(axis).unwrap_or(0.0) * 15.0 / 120.0);
                    if amount != 0.0 {
                        frame = frame.value(axis, amount);
                        if let Some(v120) = event.amount_v120(axis) {
                            frame = frame.v120(axis, v120 as i32);
                        }
                    } else if source == AxisSource::Finger && event.amount(axis) == Some(0.0) {
                        frame = frame.stop(axis);
                    }
                }
                let pointer = self.seat.get_pointer().unwrap();
                pointer.axis(self, frame);
                pointer.frame(self);
            }
            _ => {}
        }
    }

    fn on_key<B: InputBackend>(&mut self, event: B::KeyboardKeyEvent) {
        let keycode = event.key_code();
        let state = event.state();
        let serial = SERIAL_COUNTER.next_serial();
        let time = Event::time_msec(&event);
        let code: u32 = keycode.raw();
        let keyboard = self.seat.get_keyboard().unwrap();
        let action = keyboard.input(self, keycode, state, serial, time, |data, mods, handle| {
            if state == KeyState::Pressed {
                let sym = handle.raw_latin_sym_or_raw_current_sym().map(|s| s.raw()).unwrap_or(0);
                if let Some(action) = shortcut(mods, sym, handle.modified_sym().raw()) {
                    data.suppressed_keys.push(code);
                    return FilterResult::Intercept(Some(action));
                }
            } else if let Some(i) = data.suppressed_keys.iter().position(|k| *k == code) {
                // Release of a key whose press was a shortcut: the client never saw the press.
                data.suppressed_keys.remove(i);
                return FilterResult::Intercept(None);
            }
            FilterResult::Forward
        });
        if let Some(Some(action)) = action {
            self.run_action(action);
        }
    }

    pub fn run_action(&mut self, action: Action) {
        tracing::debug!(?action, "action");
        let focused = self.wm.focused;
        match action {
            Action::Quit => self.loop_signal.stop(),
            Action::VtSwitch(vt) => self.vt_switch = Some(vt),
            Action::Terminal => crate::spawn(&self.config.terminal.clone(), &self.socket_name),
            Action::Close => focused.into_iter().for_each(|id| self.close(id)),
            Action::ToggleMaximize => focused.into_iter().for_each(|id| self.toggle_maximized(id)),
            Action::MinimizeOrRestore => {
                if let Some(id) = focused {
                    if self.wm.get(id).map(|m| m.layout != crate::wm::Layout::Floating).unwrap_or(false) {
                        self.set_layout(id, crate::wm::Layout::Floating);
                    } else {
                        self.minimize(id);
                    }
                }
            }
            Action::Fullscreen => focused.into_iter().for_each(|id| self.toggle_fullscreen(id)),
            Action::TileLeft => focused.into_iter().for_each(|id| self.tile(id, true)),
            Action::TileRight => focused.into_iter().for_each(|id| self.tile(id, false)),
            Action::Workspace(n) => self.switch_workspace(n),
            Action::MoveToWorkspace(n) => focused.into_iter().for_each(|id| self.move_to_workspace(id, n)),
            Action::WorkspaceRelative(d) => {
                let n = (self.wm.active as i32 + d).rem_euclid(crate::wm::WORKSPACES as i32) as usize;
                self.switch_workspace(n);
            }
            Action::CycleWindows => self.cycle_windows(),
            Action::Screenshot => self.screenshot_requested = true,
        }
        self.redraw = true;
    }

    /// Pointer focus target under `pos` (decorations and background have none).
    fn focus_under(&self, pos: Point<f64, Logical>) -> Option<(smithay::reexports::wayland_server::protocol::wl_surface::WlSurface, Point<f64, Logical>)> {
        match self.hit_test(pos) {
            Some(Hit::Surface(_, surface, loc)) => Some((surface, loc)),
            _ => None,
        }
    }

    fn pointer_moved(&mut self, pos: Point<f64, Logical>, time: u32) {
        let pointer = self.seat.get_pointer().unwrap();
        let under = self.focus_under(pos);
        // Hover highlight for decoration buttons.
        let hover = match self.hit_test(pos) {
            Some(Hit::Decoration(id, part)) => Some((id, part)),
            _ => None,
        };
        for m in self.wm.windows.iter_mut() {
            let h = hover.filter(|(id, _)| *id == m.id).map(|(_, p)| p);
            if m.hover != h {
                m.hover = h;
            }
        }
        pointer.motion(self, under, &MotionEvent { location: pos, serial: SERIAL_COUNTER.next_serial(), time });
        self.redraw = true;
    }

    pub fn on_button(&mut self, button: u32, state: ButtonState, time: u32) {
        let pointer = self.seat.get_pointer().unwrap();
        let keyboard = self.seat.get_keyboard().unwrap();
        let serial = SERIAL_COUNTER.next_serial();
        let pos = pointer.current_location();

        if state == ButtonState::Pressed && !pointer.is_grabbed() {
            let super_held = keyboard.modifier_state().logo;
            match self.hit_test(pos) {
                Some(Hit::Decoration(id, part)) => {
                    self.focus(id);
                    if button == grabs::MOVE_BUTTON {
                        match part {
                            DecoPart::Close => self.close(id),
                            DecoPart::Maximize => self.toggle_maximized(id),
                            DecoPart::Minimize => self.minimize(id),
                            DecoPart::Bar => {
                                let now = Instant::now();
                                let double = matches!(self.wm.last_bar_click, Some((last, t)) if last == id && now - t < Duration::from_millis(400));
                                self.wm.last_bar_click = Some((id, now));
                                if double {
                                    self.toggle_maximized(id);
                                } else if let Some(window) = self.wm.get(id).map(|m| m.window.clone()) {
                                    let start = grabs::compositor_grab_start(self, button);
                                    // Deliver the press so the grab sees the button held.
                                    pointer.button(self, &ButtonEvent { button, state, serial, time });
                                    self.start_move(window, start, serial);
                                    pointer.frame(self);
                                    return;
                                }
                            }
                        }
                    }
                    self.redraw = true;
                    pointer.frame(self);
                    return; // decorations swallow the click
                }
                Some(Hit::Surface(id, ..)) => {
                    self.focus(id);
                    if super_held && (button == grabs::MOVE_BUTTON || button == grabs::RESIZE_BUTTON) {
                        if let Some(window) = self.wm.get(id).map(|m| m.window.clone()) {
                            let start = grabs::compositor_grab_start(self, button);
                            pointer.button(self, &ButtonEvent { button, state, serial, time });
                            if button == grabs::MOVE_BUTTON {
                                self.start_move(window, start, serial);
                            } else {
                                self.start_resize(window, start, ResizeEdge::BOTTOM_RIGHT, serial);
                            }
                            pointer.frame(self);
                            return;
                        }
                    }
                }
                None => {}
            }
        }
        pointer.button(self, &ButtonEvent { button, state, serial, time });
        pointer.frame(self);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mods(logo: bool, shift: bool, ctrl: bool, alt: bool) -> ModifiersState {
        ModifiersState { logo, shift, ctrl, alt, ..Default::default() }
    }

    #[test]
    fn design_shortcuts() {
        assert_eq!(shortcut(&mods(true, false, false, false), ks::KEY_Return, 0), Some(Action::Terminal));
        assert_eq!(shortcut(&mods(true, false, false, false), ks::KEY_3, 0), Some(Action::Workspace(2)));
        assert_eq!(shortcut(&mods(true, true, false, false), ks::KEY_3, 0), Some(Action::MoveToWorkspace(2)));
        assert_eq!(shortcut(&mods(true, false, true, false), ks::KEY_Right, 0), Some(Action::WorkspaceRelative(1)));
        assert_eq!(shortcut(&mods(true, false, false, false), ks::KEY_Left, 0), Some(Action::TileLeft));
        assert_eq!(shortcut(&mods(false, false, false, true), ks::KEY_F4, 0), Some(Action::Close));
        assert_eq!(shortcut(&mods(false, false, true, true), ks::KEY_F2, ks::KEY_XF86Switch_VT_2), Some(Action::VtSwitch(2)));
        assert_eq!(shortcut(&mods(false, false, false, false), ks::KEY_a, ks::KEY_a), None);
    }
}
