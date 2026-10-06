# GideonOS Design System — "Meridian" (v0, draft)

Status: **specification only.** Nothing graphical is implemented yet (graphics arrive in M3–M5).
These tokens will live in the `gideon-ui` crate as the single source of truth. The compositor
(decorations), the shell and every system app read them from there instead of hard-coding values.

## 1. Principles

1. **Calm by default.** Neutral surfaces with one accent colour. Colour carries meaning (focus,
   state, alerts), not decoration.
2. **Fast feels good.** UI responds within one frame. Animations are short, interruptible and
   never block input. "Reduce motion" turns them into fades or nothing.
3. **Keyboard first, pointer friendly.** Everything is reachable by keyboard with visible focus,
   and every target is at least 32 px (pointer) or 44 px (touch) in logical pixels.
4. **Resolution independent.** Everything is specified in logical pixels (lp) and scales with
   output scale (1.0, 1.25, 1.5, 1.75, 2.0 …). Icons are vector. Suitable for 1080p, 1440p and 4K.
5. **One system.** Shell, apps, dialogs and window decorations share the same tokens and
   components.

It is deliberately not a Windows or macOS look-alike. Its signature elements are the
**meridian line** (a 2 lp accent line marking focus and the active window title), a floating
**dock-panel** detached from the screen edge, and soft-squared corners.

## 2. Colour

Colour tokens, dark theme first (the default) and light theme second. All text/background
pairs meet WCAG AA (4.5:1 body, 3:1 large text and UI glyphs).

| Token | Dark | Light | Use |
|---|---|---|---|
| `bg.base` | `#0F1216` | `#F4F5F7` | desktop fallback, app background |
| `bg.surface` | `#171B21` | `#FFFFFF` | windows, cards |
| `bg.raised` | `#1F242C` | `#FFFFFF` | menus, popovers, dialogs |
| `bg.sunken` | `#0B0D10` | `#E9EBEF` | text fields, wells |
| `bg.panel` | `#171B21` @ 86 % + blur | `#FFFFFF` @ 82 % + blur | dock-panel, quick settings |
| `fg.primary` | `#E8EBEF` | `#14171C` | body text |
| `fg.secondary` | `#A3ABB7` | `#4D5562` | captions, inactive titles |
| `fg.disabled` | `#5E6672` | `#9AA1AC` | disabled |
| `border.subtle` | `#2A3039` | `#DDE1E6` | separators, card edges |
| `border.strong` | `#3A424E` | `#C3C9D1` | inputs |
| `accent` | `#3DB8C6` | `#0E7F8C` | "Gideon Teal": focus, selection, primary buttons |
| `accent.fg` | `#06181B` | `#FFFFFF` | text on accent |
| `success` | `#4CC38A` | `#1B7F4E` | |
| `warning` | `#E8B04B` | `#9A6400` | |
| `danger` | `#F06A6A` | `#C02B2B` | destructive actions, errors |
| `focus.ring` | `accent` @ 2 lp, 2 lp offset | same | keyboard focus |

The accent colour is user-configurable. Contrast-checked alternatives: Teal (default), Iris
`#8B8CF6`, Amber `#E8A33D`, Rose `#EC6F9A`, Moss `#7FB85A`.

## 3. Typography

* UI: **Inter** (SIL OFL 1.1). Monospace: **JetBrains Mono** (OFL 1.1). Both ship in the image.
* Type scale (lp / line height / weight):

| Token | Size | Line | Weight | Use |
|---|---|---|---|---|
| `display` | 32 | 40 | 600 | welcome, installer headings |
| `title` | 22 | 28 | 600 | page titles |
| `heading` | 17 | 24 | 600 | section headings, dialog titles |
| `body` | 14 | 20 | 400 | default text |
| `body.strong` | 14 | 20 | 600 | emphasis, button labels |
| `caption` | 12 | 16 | 400 | metadata, panel clock date |
| `mono` | 13 | 20 | 400 | terminal, code |

* Window titles use `body.strong`. Line length in reading views is capped at about 80 characters.

## 4. Spacing, sizing, shape

* **4 lp base grid.** Spacing scale: `xs 4 · sm 8 · md 12 · lg 16 · xl 24 · 2xl 32 · 3xl 48`.
* Control heights: compact 28, default 32, large 40 (installer/touch 44).
* Corner radius: `r.sm 6` (inputs, buttons) · `r.md 10` (cards, menus) · `r.lg 14` (windows,
  dialogs, dock-panel) · `r.full` (pills, toggles). Maximized/tiled windows have square corners.
* Elevation (dark theme uses lighter surfaces plus shadow; light theme uses shadow only):
  `e0` none · `e1` 0 1 2 @ 30 % (cards) · `e2` 0 4 12 @ 35 % (menus) · `e3` 0 12 32 @ 45 %
  (focused windows, dialogs). Unfocused windows drop to `e2`.

## 5. Iconography

* Own icon set on a **24 lp grid, 1.75 lp stroke, rounded caps/joins**, 2 lp safe area, drawn as
  SVG. 16 lp and 20 lp variants for dense UI.
* App icons: 128 lp master on a soft-square (r = 28 %) tile, one-colour glyph on a two-tone
  gradient tile.
* Until the set exists, an interim permissively licensed set (Lucide, ISC) is used for glyphs,
  restyled to the stroke spec. Its licence is recorded in the image.
* Third-party apps use freedesktop icon theme lookup with our set as the theme and hicolor as the
  fallback.

## 6. Window decorations (server-side, drawn by the compositor)

* Title bar 36 lp. Title is `body.strong`, left-aligned after an optional app icon (16 lp).
* Controls on the **right**: minimize, maximize/restore, close. Each is 28 lp circular
  hit area with 14 lp glyphs. Close turns `danger` on hover.
* The active window has the **meridian line**: a 2 lp `accent` line along the top edge, plus `e3`.
  Inactive windows have no line, `fg.secondary` title and `e2`.
* Resize handles are 8 lp outside the visible edge (invisible) plus corners.
* Double-click title = maximize. Super+drag = move. Super+right-drag = resize.

**Implemented in M4 (gideon-compositor):** 36 lp title bar, controls on the right, meridian
line on the active window, close hover in `danger`, double-click maximize, Super+drag and
Super+right-drag. **Not yet:** title text and app icon (the compositor has no font rendering
yet), circular button shapes and glyphs (buttons are solid 20 lp squares for now), elevation
shadows, rounded window corners, and invisible outer resize handles (edge resizing works when
the client requests it, and through Super+right-drag).

## 7. Components (initial set)

| Component | Spec summary |
|---|---|
| Button | 32 lp, `r.sm`, padding 0 × 14. Variants: primary (accent), secondary (raised + border), ghost, destructive (danger). Pressed state = 4 % darker plus 0.98 scale |
| Text field | 32 lp, `bg.sunken`, `border.strong`, focus = accent border + ring |
| Toggle | 36×20 pill, knob 16. On = accent |
| Menu | `bg.raised`, `r.md`, `e2`, item 32 lp, 4 lp inner padding, keyboard navigable, type-ahead |
| Dialog | max 480 lp wide, `r.lg`, `e3`, heading + body + right-aligned buttons. Primary action rightmost. Destructive dialogs name the object and default focus to Cancel |
| Notification | 360 lp wide, top-right stack, `bg.raised` @ 95 % + blur, icon + title + body + up to 2 actions, auto-dismiss 6 s (not critical ones), history in quick settings |
| Dock-panel | floating bar 48 lp tall, 8 lp from the bottom edge, `r.lg`. Left: launcher button. Centre: pinned + running apps (running = 4 lp accent dot). Right: tray, network/audio/battery, clock |
| Launcher | centred overlay 640 lp wide, search field focused on open, app grid (88 lp tiles), results grouped by apps / files / settings / actions |
| Quick settings | popover from the panel's right side: tiles for Wi-Fi, Bluetooth, Do-Not-Disturb, dark mode; sliders for volume and brightness; power menu |

## 8. Motion

| Token | Duration | Easing | Use |
|---|---|---|---|
| `m.instant` | 0 | — | reduce motion |
| `m.fast` | 120 ms | `cubic-bezier(0.2, 0, 0, 1)` | hover, press, toggles |
| `m.normal` | 200 ms | `cubic-bezier(0.2, 0, 0, 1)` | menus, popovers, notifications in |
| `m.window` | 240 ms | spring (stiffness 400, damping 32) | open/close/minimize/maximize |
| `m.workspace` | 280 ms | `cubic-bezier(0.3, 0, 0, 1)` | workspace slide, overview |

Exits are about 30 % faster than entrances. Animations are driven by output vblank and
cancel cleanly on new input.

**Implemented in M4:** a 150 ms fade-in for new windows. The other motion tokens are not
implemented yet.

## 9. Keyboard model (defaults)

| Shortcut | Action |
|---|---|
| Super | open/close launcher |
| Super+Tab / Alt+Tab | overview / switch window |
| Super+↑ / ↓ | maximize / restore-minimize |
| Super+← / → | tile half left / right |
| Super+F | fullscreen |
| Super+1…9 | workspace N · Super+Shift+N moves the window |
| Super+Ctrl+← / → | previous / next workspace |
| Super+Enter | terminal |
| Super+E | Files · Super+I Settings · Super+L lock |
| Print | screenshot tool · Shift+Print region |
| Alt+F4 / Super+Q | close window |

## 10. Branding

* Name: **GideonOS**, written as one word with capital G and OS.
* Wordmark: Inter 600, letter-spacing −1 %. Logomark: a circle split by a horizontal
  meridian, in `accent`.
* Boot: logomark centred on `bg.base`, a thin progress meridian below. No scrolling text
  unless the user picks "verbose boot".
* Text-mode identity (M1–M2): ASCII wordmark in `/etc/motd`, `PRETTY_NAME="GideonOS <ver>
  (<codename>)"`, and release codenames in alphabetical order starting with *Genesis*.
