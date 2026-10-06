//! Window management: window model, workspaces, focus and window states.
//!
//! `Space` holds only the windows that are visible (active workspace, not
//! minimized); `WindowManager` is the source of truth for every window.

use std::time::Instant;

use smithay::{
    backend::renderer::element::solid::SolidColorBuffer,
    desktop::Window,
    reexports::{
        wayland_protocols::xdg::shell::server::xdg_toplevel::State as XdgState,
        wayland_server::protocol::wl_surface::WlSurface,
    },
    utils::{Logical, Point, Rectangle, Size, SERIAL_COUNTER},
    wayland::{compositor::with_states, shell::xdg::XdgToplevelSurfaceData},
};

use crate::{grabs::ResizeEdge, state::Gideon};

/// Title bar height in logical pixels (docs/DESIGN.md §6).
pub const TITLE_H: i32 = 36;
/// Accent "meridian" line on the focused window's title bar.
pub const MERIDIAN_H: i32 = 2;
pub const BUTTON: i32 = 20;
pub const BUTTON_GAP: i32 = 8;
pub const WORKSPACES: usize = 9;
const MIN_SIZE: i32 = 120;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Layout {
    Floating,
    Maximized,
    Fullscreen,
    TiledLeft,
    TiledRight,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DecoPart {
    Bar,
    Minimize,
    Maximize,
    Close,
}

/// Interactive resize in progress (kept until the client's final commit so
/// top/left resizes can move the window as it shrinks or grows).
#[derive(Debug, Clone, Copy)]
pub struct ResizeState {
    pub edges: ResizeEdge,
    pub initial: Rectangle<i32, Logical>,
    pub finishing: bool,
}

pub struct Decoration {
    pub bar: SolidColorBuffer,
    pub meridian: SolidColorBuffer,
    pub buttons: [SolidColorBuffer; 3],
}

pub struct Managed {
    pub id: u64,
    pub window: Window,
    pub workspace: usize,
    pub minimized: bool,
    pub layout: Layout,
    /// Floating geometry to restore after maximize/fullscreen/tiling.
    pub restore: Option<Rectangle<i32, Logical>>,
    /// Last location (also used while unmapped).
    pub loc: Point<i32, Logical>,
    pub placed: bool,
    pub mapped_at: Option<Instant>,
    pub z: u64,
    pub hover: Option<DecoPart>,
    pub resize: Option<ResizeState>,
    pub deco: Decoration,
}

#[derive(Default)]
pub struct WindowManager {
    pub windows: Vec<Managed>,
    pub active: usize,
    pub focused: Option<u64>,
    next_id: u64,
    next_z: u64,
    /// For double-click on the title bar.
    pub last_bar_click: Option<(u64, Instant)>,
}

impl WindowManager {
    pub fn id_of(&self, window: &Window) -> Option<u64> {
        self.windows.iter().find(|m| &m.window == window).map(|m| m.id)
    }
    pub fn get(&self, id: u64) -> Option<&Managed> {
        self.windows.iter().find(|m| m.id == id)
    }
    pub fn get_mut(&mut self, id: u64) -> Option<&mut Managed> {
        self.windows.iter_mut().find(|m| m.id == id)
    }
    pub fn by_window_mut(&mut self, window: &Window) -> Option<&mut Managed> {
        self.windows.iter_mut().find(|m| &m.window == window)
    }
    fn bump_z(&mut self) -> u64 {
        self.next_z += 1;
        self.next_z
    }
    /// Windows of the active workspace, topmost first.
    pub fn stack_on_active(&self) -> Vec<u64> {
        let mut v: Vec<&Managed> = self.windows.iter().filter(|m| m.workspace == self.active).collect();
        v.sort_by_key(|m| std::cmp::Reverse(m.z));
        v.into_iter().map(|m| m.id).collect()
    }
}

pub fn title_of(window: &Window) -> (String, String) {
    let Some(toplevel) = window.toplevel() else { return Default::default() };
    with_states(toplevel.wl_surface(), |states| {
        states
            .data_map
            .get::<XdgToplevelSurfaceData>()
            .map(|d| {
                let d = d.lock().unwrap();
                (d.title.clone().unwrap_or_default(), d.app_id.clone().unwrap_or_default())
            })
            .unwrap_or_default()
    })
}

/// Rectangles of the decoration parts for a window whose content starts at `loc`.
pub fn deco_rects(loc: Point<i32, Logical>, width: i32) -> [(DecoPart, Rectangle<i32, Logical>); 4] {
    let bar = Rectangle::new((loc.x, loc.y - TITLE_H).into(), (width, TITLE_H).into());
    let y = loc.y - TITLE_H + (TITLE_H - BUTTON) / 2;
    let button = |i: i32| {
        let x = loc.x + width - (BUTTON_GAP + BUTTON) * (3 - i);
        Rectangle::new((x, y).into(), (BUTTON, BUTTON).into())
    };
    [
        (DecoPart::Minimize, button(0)),
        (DecoPart::Maximize, button(1)),
        (DecoPart::Close, button(2)),
        (DecoPart::Bar, bar),
    ]
}

impl Gideon {
    pub fn has_decoration(&self, id: u64) -> bool {
        self.wm.get(id).map(|m| m.layout != Layout::Fullscreen).unwrap_or(false)
    }

    /// New toplevel: tracked now, placed on its first commit with content.
    pub fn add_window(&mut self, window: Window) {
        let id = {
            self.wm.next_id += 1;
            self.wm.next_id
        };
        let z = self.wm.bump_z();
        let deco = Decoration {
            bar: SolidColorBuffer::new((1, TITLE_H), [0.09, 0.106, 0.129, 1.0]),
            meridian: SolidColorBuffer::new((1, MERIDIAN_H), [0.239, 0.722, 0.776, 1.0]),
            buttons: std::array::from_fn(|_| SolidColorBuffer::new((BUTTON, BUTTON), [0.37, 0.4, 0.45, 1.0])),
        };
        self.wm.windows.push(Managed {
            id,
            window: window.clone(),
            workspace: self.wm.active,
            minimized: false,
            layout: Layout::Floating,
            restore: None,
            loc: (0, 0).into(),
            placed: false,
            mapped_at: None,
            z,
            hover: None,
            resize: None,
            deco,
        });
        // Mapped off to the side until it has a size; not rendered before placement.
        self.space.map_element(window, (-100_000, -100_000), false);
        tracing::info!(id, "new window");
    }

    /// Called on every commit of a toplevel's surface.
    pub fn window_committed(&mut self, window: &Window) {
        let size = window.geometry().size;
        let Some(m) = self.wm.by_window_mut(window) else { return };
        let id = m.id;

        if !m.placed && size.w > 0 && size.h > 0 {
            m.placed = true;
            m.mapped_at = Some(Instant::now());
            let wants_fullscreen = window
                .toplevel()
                .map(|t| t.current_state().states.contains(XdgState::Fullscreen) || t.with_pending_state(|s| s.states.contains(XdgState::Fullscreen)))
                .unwrap_or(false);
            let loc = self.initial_location(size);
            if let Some(m) = self.wm.get_mut(id) {
                m.loc = loc;
            }
            self.space.map_element(window.clone(), loc, false);
            if wants_fullscreen {
                self.set_fullscreen(id, true);
            }
            self.focus(id);
            let (title, app_id) = title_of(window);
            tracing::info!(id, %title, %app_id, w = size.w, h = size.h, x = loc.x, y = loc.y, "window mapped");
            return;
        }

        // Interactive resize from the top/left edges: keep the opposite edge fixed.
        if let Some(rs) = m.resize {
            let mut loc = m.loc;
            if rs.edges.intersects(ResizeEdge::LEFT) {
                loc.x = rs.initial.loc.x + (rs.initial.size.w - size.w);
            }
            if rs.edges.intersects(ResizeEdge::TOP) {
                loc.y = rs.initial.loc.y + (rs.initial.size.h - size.h);
            }
            if rs.finishing {
                m.resize = None;
            }
            if loc != m.loc {
                m.loc = loc;
                self.space.map_element(window.clone(), loc, false);
            }
        }
    }

    /// Centre new windows on the output under the pointer, cascading.
    fn initial_location(&self, size: Size<i32, Logical>) -> Point<i32, Logical> {
        let pointer = self.seat.get_pointer().map(|p| p.current_location()).unwrap_or_default();
        let Some(output) = self.output_at(pointer) else { return (0, TITLE_H).into() };
        let geo = self.output_geometry(&output);
        let cascade = (self
            .wm
            .windows
            .iter()
            .filter(|m| m.workspace == self.wm.active && m.placed && !m.minimized)
            .count() as i32
            - 1)
            .max(0)
            % 8
            * 32;
        let x = geo.loc.x + ((geo.size.w - size.w) / 2).max(0) + cascade;
        let y = geo.loc.y + ((geo.size.h - size.h - TITLE_H) / 2).max(0) + TITLE_H + cascade;
        (x, y).into()
    }

    pub fn remove_window(&mut self, surface: &WlSurface) {
        let Some(window) = self.window_for_surface(surface) else { return };
        let id = self.wm.id_of(&window);
        self.space.unmap_elem(&window);
        self.wm.windows.retain(|m| m.window != window);
        if id.is_some() && self.wm.focused == id {
            self.wm.focused = None;
            self.focus_top();
        }
        self.redraw = true;
        tracing::info!(?id, "window closed");
    }

    /// Give keyboard focus to and raise a window (restoring it if minimized).
    pub fn focus(&mut self, id: u64) {
        let Some(m) = self.wm.get(id) else { return };
        if m.workspace != self.wm.active {
            self.switch_workspace(m.workspace);
        }
        if self.wm.get(id).map(|m| m.minimized).unwrap_or(false) {
            self.unminimize(id);
        }
        let z = self.wm.bump_z();
        let Some(m) = self.wm.get_mut(id) else { return };
        m.z = z;
        let window = m.window.clone();
        self.space.raise_element(&window, false);
        for other in &self.wm.windows {
            other.window.set_activated(other.id == id);
            if let Some(t) = other.window.toplevel() {
                if t.is_initial_configure_sent() {
                    t.send_pending_configure();
                }
            }
        }
        self.wm.focused = Some(id);
        if let (Some(keyboard), Some(t)) = (self.seat.get_keyboard(), window.toplevel()) {
            keyboard.set_focus(self, Some(t.wl_surface().clone()), SERIAL_COUNTER.next_serial());
        }
        self.redraw = true;
    }

    /// Focus the topmost visible window of the active workspace (or nothing).
    pub fn focus_top(&mut self) {
        let next = self
            .wm
            .stack_on_active()
            .into_iter()
            .find(|id| self.wm.get(*id).map(|m| !m.minimized && m.placed).unwrap_or(false));
        match next {
            Some(id) => self.focus(id),
            None => {
                self.wm.focused = None;
                if let Some(keyboard) = self.seat.get_keyboard() {
                    keyboard.set_focus(self, None, SERIAL_COUNTER.next_serial());
                }
            }
        }
    }

    pub fn close(&mut self, id: u64) {
        if let Some(t) = self.wm.get(id).and_then(|m| m.window.toplevel().cloned()) {
            t.send_close();
        }
    }

    pub fn minimize(&mut self, id: u64) {
        let Some(m) = self.wm.get_mut(id) else { return };
        if m.minimized {
            return;
        }
        m.minimized = true;
        let window = m.window.clone();
        if let Some(loc) = self.space.element_location(&window) {
            if let Some(m) = self.wm.get_mut(id) {
                m.loc = loc;
            }
        }
        self.space.unmap_elem(&window);
        if self.wm.focused == Some(id) {
            self.focus_top();
        }
        self.redraw = true;
    }

    pub fn unminimize(&mut self, id: u64) {
        let Some(m) = self.wm.get_mut(id) else { return };
        if !m.minimized {
            return;
        }
        m.minimized = false;
        m.mapped_at = Some(Instant::now());
        let (window, loc, ws) = (m.window.clone(), m.loc, m.workspace);
        if ws == self.wm.active {
            self.space.map_element(window, loc, false);
        }
        self.redraw = true;
    }

    /// Content rectangle (below the title bar) of an output's usable area.
    fn work_area(&self, id: u64) -> Option<Rectangle<i32, Logical>> {
        let window = self.wm.get(id)?.window.clone();
        let output = self
            .space
            .outputs_for_element(&window)
            .into_iter()
            .next()
            .or_else(|| self.space.outputs().next().cloned())?;
        Some(self.output_geometry(&output))
    }

    fn current_rect(&self, id: u64) -> Option<Rectangle<i32, Logical>> {
        let m = self.wm.get(id)?;
        let loc = self.space.element_location(&m.window).unwrap_or(m.loc);
        Some(Rectangle::new(loc, m.window.geometry().size))
    }

    /// Apply a layout: configure size/states and position the window.
    pub fn set_layout(&mut self, id: u64, layout: Layout) {
        let Some(area) = self.work_area(id) else { return };
        let Some(current) = self.current_rect(id) else { return };
        let Some(m) = self.wm.get_mut(id) else { return };
        if m.minimized {
            m.minimized = false;
        }
        let old = m.layout;
        if old == Layout::Floating && layout != Layout::Floating {
            m.restore = Some(current);
        }
        let below_bar = Rectangle::new(
            (area.loc.x, area.loc.y + TITLE_H).into(),
            (area.size.w, area.size.h - TITLE_H).into(),
        );
        let half = area.size.w / 2;
        let target = match layout {
            Layout::Floating => m.restore.take().unwrap_or(current),
            Layout::Maximized => below_bar,
            Layout::Fullscreen => area,
            Layout::TiledLeft => Rectangle::new(below_bar.loc, (half, below_bar.size.h).into()),
            Layout::TiledRight => {
                Rectangle::new((below_bar.loc.x + half, below_bar.loc.y).into(), (area.size.w - half, below_bar.size.h).into())
            }
        };
        m.layout = layout;
        m.loc = target.loc;
        let window = m.window.clone();
        if let Some(t) = window.toplevel() {
            t.with_pending_state(|s| {
                for st in [XdgState::Maximized, XdgState::Fullscreen, XdgState::TiledLeft, XdgState::TiledRight] {
                    s.states.unset(st);
                }
                match layout {
                    Layout::Maximized => {
                        s.states.set(XdgState::Maximized);
                    }
                    Layout::Fullscreen => {
                        s.states.set(XdgState::Fullscreen);
                    }
                    Layout::TiledLeft => {
                        s.states.set(XdgState::TiledLeft);
                    }
                    Layout::TiledRight => {
                        s.states.set(XdgState::TiledRight);
                    }
                    Layout::Floating => {}
                }
                s.size = Some(target.size);
            });
            if t.is_initial_configure_sent() {
                t.send_pending_configure();
            }
        }
        self.space.map_element(window, target.loc, false);
        self.focus(id);
        tracing::info!(id, ?layout, "layout");
    }

    pub fn set_maximized(&mut self, id: u64, on: bool) {
        self.set_layout(id, if on { Layout::Maximized } else { Layout::Floating });
    }

    pub fn toggle_maximized(&mut self, id: u64) {
        let on = self.wm.get(id).map(|m| m.layout != Layout::Maximized).unwrap_or(false);
        self.set_maximized(id, on);
    }

    pub fn set_fullscreen(&mut self, id: u64, on: bool) {
        self.set_layout(id, if on { Layout::Fullscreen } else { Layout::Floating });
    }

    pub fn toggle_fullscreen(&mut self, id: u64) {
        let on = self.wm.get(id).map(|m| m.layout != Layout::Fullscreen).unwrap_or(false);
        self.set_fullscreen(id, on);
    }

    pub fn tile(&mut self, id: u64, left: bool) {
        let target = if left { Layout::TiledLeft } else { Layout::TiledRight };
        let current = self.wm.get(id).map(|m| m.layout);
        self.set_layout(id, if current == Some(target) { Layout::Floating } else { target });
    }

    /// Move a floating window (IPC); `loc` is the content's top-left.
    pub fn move_window(&mut self, id: u64, loc: Point<i32, Logical>) {
        let Some(m) = self.wm.get_mut(id) else { return };
        m.loc = loc;
        let (window, visible) = (m.window.clone(), !m.minimized && m.workspace == self.wm.active);
        if visible {
            self.space.map_element(window, loc, false);
        }
        self.redraw = true;
    }

    pub fn resize_window(&mut self, id: u64, size: Size<i32, Logical>) {
        let size = Size::from((size.w.max(MIN_SIZE), size.h.max(MIN_SIZE / 2)));
        if let Some(t) = self.wm.get(id).and_then(|m| m.window.toplevel().cloned()) {
            t.with_pending_state(|s| s.size = Some(size));
            t.send_pending_configure();
        }
    }

    pub fn switch_workspace(&mut self, ws: usize) {
        if ws >= WORKSPACES || ws == self.wm.active {
            return;
        }
        // Remember positions, unmap the old workspace, map the new one in z order.
        let visible: Vec<Window> = self.space.elements().cloned().collect();
        for window in visible {
            if let Some(loc) = self.space.element_location(&window) {
                if let Some(m) = self.wm.by_window_mut(&window) {
                    if m.placed {
                        m.loc = loc;
                    }
                }
            }
            if self.wm.windows.iter().any(|m| m.window == window && m.placed) {
                self.space.unmap_elem(&window);
            }
        }
        self.wm.active = ws;
        let mut show: Vec<(u64, Window, Point<i32, Logical>)> = self
            .wm
            .windows
            .iter()
            .filter(|m| m.workspace == ws && !m.minimized && m.placed)
            .map(|m| (m.z, m.window.clone(), m.loc))
            .collect();
        show.sort_by_key(|(z, _, _)| *z);
        for (_, window, loc) in show {
            self.space.map_element(window, loc, false);
        }
        self.focus_top();
        self.redraw = true;
        tracing::info!(workspace = ws + 1, "workspace");
    }

    pub fn move_to_workspace(&mut self, id: u64, ws: usize) {
        if ws >= WORKSPACES {
            return;
        }
        let Some(m) = self.wm.get_mut(id) else { return };
        if m.workspace == ws {
            return;
        }
        m.workspace = ws;
        let window = m.window.clone();
        if let Some(loc) = self.space.element_location(&window) {
            if let Some(m) = self.wm.get_mut(id) {
                m.loc = loc;
            }
        }
        self.space.unmap_elem(&window);
        if self.wm.focused == Some(id) {
            self.focus_top();
        }
        self.redraw = true;
    }

    /// Alt+Tab: bring the most recently used other window forward.
    pub fn cycle_windows(&mut self) {
        let stack = self.wm.stack_on_active();
        let next = stack.iter().copied().find(|id| Some(*id) != self.wm.focused && self.wm.get(*id).map(|m| m.placed).unwrap_or(false));
        // Rotate: send the current one to the bottom so repeated presses cycle all.
        if let Some(cur) = self.wm.focused {
            if let Some(m) = self.wm.get_mut(cur) {
                m.z = 0;
            }
        }
        if let Some(id) = next {
            self.focus(id);
        }
    }

    /// Topmost hit among decorations and window contents.
    pub fn hit_test(&self, pos: Point<f64, Logical>) -> Option<Hit> {
        for window in self.space.elements().rev() {
            let Some(id) = self.wm.id_of(window) else { continue };
            let Some(m) = self.wm.get(id) else { continue };
            if !m.placed {
                continue;
            }
            let loc = self.space.element_location(window).unwrap_or(m.loc);
            if self.has_decoration(id) {
                for (part, rect) in deco_rects(loc, window.geometry().size.w) {
                    if rect.to_f64().contains(pos) {
                        return Some(Hit::Decoration(id, part));
                    }
                }
            }
            let render_loc = loc - window.geometry().loc;
            if let Some((surface, surface_loc)) = crate::state::surface_under(window, render_loc, pos) {
                return Some(Hit::Surface(id, surface, surface_loc));
            }
        }
        None
    }
}

pub enum Hit {
    Decoration(u64, DecoPart),
    Surface(u64, WlSurface, Point<f64, Logical>),
}
