//! Building the frame for an output, the cursor image, and screenshots.

use std::time::{Duration, Instant};

use smithay::{
    backend::{
        allocator::Fourcc,
        renderer::{
            damage::OutputDamageTracker,
            element::{
                memory::{MemoryRenderBuffer, MemoryRenderBufferRenderElement},
                solid::{SolidColorBuffer, SolidColorRenderElement},
                surface::{render_elements_from_surface_tree, WaylandSurfaceRenderElement},
                AsRenderElements, Kind,
            },
            Bind, ExportMem, ImportAll, ImportMem, Offscreen, Renderer, Texture,
        },
    },
    input::pointer::{CursorImageAttributes, CursorImageStatus},
    output::Output,
    render_elements,
    utils::{IsAlive, Logical, Physical, Point, Rectangle, Scale, Size, Transform},
    wayland::compositor::with_states,
};

use crate::{
    state::Gideon,
    wm::{deco_rects, DecoPart, Layout, MERIDIAN_H, TITLE_H},
};

render_elements! {
    pub GideonElement<R> where R: ImportAll + ImportMem;
    Surface=WaylandSurfaceRenderElement<R>,
    Solid=SolidColorRenderElement,
    Memory=MemoryRenderBufferRenderElement<R>,
}

/// Window open animation length (docs/DESIGN.md m.window, simplified to a fade).
pub const FADE: Duration = Duration::from_millis(150);

const BAR_FOCUSED: [f32; 4] = [0.122, 0.141, 0.173, 1.0]; // bg.raised #1f242c
const BAR: [f32; 4] = [0.09, 0.106, 0.129, 1.0]; // bg.surface #171b21
const ACCENT: [f32; 4] = [0.239, 0.722, 0.776, 1.0]; // accent #3db8c6
const BUTTON: [f32; 4] = [0.227, 0.259, 0.306, 1.0]; // border.strong #3a424e
const BUTTON_HOVER: [f32; 4] = [0.369, 0.4, 0.447, 1.0]; // fg.disabled #5e6672
const DANGER: [f32; 4] = [0.941, 0.416, 0.416, 1.0]; // danger #f06a6a

/// The default pointer: an arrow drawn into a memory buffer (no cursor theme needed).
pub fn cursor_buffer() -> MemoryRenderBuffer {
    const ART: [&str; 18] = [
        "B...........",
        "BB..........",
        "BWB.........",
        "BWWB........",
        "BWWWB.......",
        "BWWWWB......",
        "BWWWWWB.....",
        "BWWWWWWB....",
        "BWWWWWWWB...",
        "BWWWWWWWWB..",
        "BWWWWWWWWWB.",
        "BWWWWWWBBBBB",
        "BWWWBWWB....",
        "BWWB.BWWB...",
        "BWB..BWWB...",
        "BB....BWWB..",
        "B.....BWWB..",
        ".......BB...",
    ];
    let (w, h) = (ART[0].len(), ART.len());
    let mut pixels = Vec::with_capacity(w * h * 4);
    for row in ART {
        for c in row.bytes() {
            // Argb8888 little-endian: B, G, R, A
            pixels.extend_from_slice(match c {
                b'B' => &[0x16, 0x12, 0x0f, 0xff],
                b'W' => &[0xef, 0xeb, 0xe8, 0xff],
                _ => &[0, 0, 0, 0],
            });
        }
    }
    MemoryRenderBuffer::from_slice(&pixels, Fourcc::Argb8888, (w as i32, h as i32), 1, Transform::Normal, None)
}

pub fn fade_alpha(mapped_at: Option<Instant>) -> f32 {
    match mapped_at {
        Some(t) => (t.elapsed().as_secs_f32() / FADE.as_secs_f32()).clamp(0.0, 1.0),
        None => 1.0,
    }
}

fn solid<R>(buffer: &SolidColorBuffer, rect: Rectangle<i32, Logical>, origin: Point<i32, Logical>, scale: f64, alpha: f32) -> GideonElement<R>
where
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Clone + Texture + Send + 'static,
{
    let loc = (rect.loc - origin).to_physical_precise_round(scale);
    GideonElement::Solid(SolidColorRenderElement::from_buffer(buffer, loc, scale, alpha, Kind::Unspecified))
}

/// Elements for `output`, topmost first. Returns (elements, still_animating).
pub fn output_elements<R>(state: &mut Gideon, renderer: &mut R, output: &Output) -> (Vec<GideonElement<R>>, bool)
where
    R: Renderer + ImportAll + ImportMem,
    R::TextureId: Clone + Texture + Send + 'static,
{
    let mut elements: Vec<GideonElement<R>> = Vec::new();
    let mut animating = false;
    let Some(geo) = state.space.output_geometry(output) else { return (elements, false) };
    let scale = output.current_scale().fractional_scale();
    let origin = geo.loc;

    // Pointer (and drag-and-drop icon) on top.
    let pointer = state.seat.get_pointer().map(|p| p.current_location()).unwrap_or_default();
    if geo.to_f64().contains(pointer) {
        let rel = pointer - origin.to_f64();
        match &state.cursor_status {
            CursorImageStatus::Hidden => {}
            CursorImageStatus::Surface(surface) if surface.alive() => {
                let hotspot = with_states(surface, |states| {
                    states
                        .data_map
                        .get::<std::sync::Mutex<CursorImageAttributes>>()
                        .map(|a| a.lock().unwrap().hotspot)
                        .unwrap_or_default()
                });
                let loc = (rel - hotspot.to_f64()).to_physical(scale).to_i32_round();
                elements.extend(
                    render_elements_from_surface_tree::<_, WaylandSurfaceRenderElement<R>>(renderer, surface, loc, scale, 1.0, Kind::Cursor)
                        .into_iter()
                        .map(GideonElement::Surface),
                );
            }
            _ => {
                if let Ok(el) = MemoryRenderBufferRenderElement::from_buffer(
                    renderer,
                    rel.to_physical(scale),
                    &state.cursor_buffer,
                    None,
                    None,
                    None,
                    Kind::Cursor,
                ) {
                    elements.push(GideonElement::Memory(el));
                }
            }
        }
        if let Some(icon) = state.dnd_icon.as_ref().filter(|s| s.alive()) {
            let loc = rel.to_physical(scale).to_i32_round();
            elements.extend(
                render_elements_from_surface_tree::<_, WaylandSurfaceRenderElement<R>>(renderer, icon, loc, scale, 1.0, Kind::Unspecified)
                    .into_iter()
                    .map(GideonElement::Surface),
            );
        }
    }

    let focused = state.wm.focused;
    let windows: Vec<_> = state.space.elements().rev().cloned().collect();
    for window in windows {
        let Some(id) = state.wm.id_of(&window) else { continue };
        let (placed, layout, mapped_at, hover) = {
            let m = state.wm.get(id).unwrap();
            (m.placed, m.layout, m.mapped_at, m.hover)
        };
        if !placed {
            continue;
        }
        let Some(loc) = state.space.element_location(&window) else { continue };
        let size = window.geometry().size;
        let frame = Rectangle::new((loc.x, loc.y - TITLE_H).into(), (size.w, size.h + TITLE_H).into());
        let fullscreen = layout == Layout::Fullscreen;
        if !frame.overlaps(geo) && !(fullscreen && Rectangle::new(loc, size).overlaps(geo)) {
            continue;
        }
        let alpha = fade_alpha(mapped_at);
        animating |= alpha < 1.0;

        let render_loc = (loc - window.geometry().loc - origin).to_physical_precise_round(scale);
        elements.extend(
            window
                .render_elements::<WaylandSurfaceRenderElement<R>>(renderer, render_loc, Scale::from(scale), alpha)
                .into_iter()
                .map(GideonElement::Surface),
        );

        if fullscreen {
            // A fullscreen window hides everything beneath it on this output.
            break;
        }

        let is_focused = focused == Some(id);
        let m = state.wm.get_mut(id).unwrap();
        let rects = deco_rects(loc, size.w);
        for (i, part) in [DecoPart::Minimize, DecoPart::Maximize, DecoPart::Close].into_iter().enumerate() {
            let color = match (part, hover == Some(part)) {
                (DecoPart::Close, true) => DANGER,
                (_, true) => BUTTON_HOVER,
                _ => BUTTON,
            };
            m.deco.buttons[i].set_color(color);
            let rect = rects.iter().find(|(p, _)| *p == part).unwrap().1;
            elements.push(solid(&m.deco.buttons[i], rect, origin, scale, alpha));
        }
        let bar = rects[3].1;
        if is_focused {
            m.deco.meridian.update((size.w, MERIDIAN_H), ACCENT);
            elements.push(solid(&m.deco.meridian, Rectangle::new(bar.loc, (size.w, MERIDIAN_H).into()), origin, scale, alpha));
        }
        m.deco.bar.update((size.w, TITLE_H), if is_focused { BAR_FOCUSED } else { BAR });
        elements.push(solid(&m.deco.bar, bar, origin, scale, alpha));
    }
    (elements, animating)
}

pub fn clear_color(state: &Gideon) -> [f32; 4] {
    let [r, g, b] = state.config.background;
    [r, g, b, 1.0]
}

/// Render `elements` offscreen and read back RGBA pixels (for screenshots).
pub fn capture<R, T>(
    renderer: &mut R,
    elements: &[GideonElement<R>],
    size: Size<i32, Physical>,
    scale: f64,
    clear: [f32; 4],
) -> Result<Vec<u8>, String>
where
    R: Renderer + ImportAll + ImportMem + Offscreen<T> + Bind<T> + ExportMem,
    R::TextureId: Clone + Texture + Send + 'static,
{
    let buffer_size = size.to_logical(1).to_buffer(1, Transform::Normal);
    let mut target: T = renderer.create_buffer(Fourcc::Abgr8888, buffer_size).map_err(|e| format!("{e:?}"))?;
    let mut fb = renderer.bind(&mut target).map_err(|e| format!("{e:?}"))?;
    let mut tracker = OutputDamageTracker::new(size, scale, Transform::Normal);
    tracker
        .render_output(renderer, &mut fb, 0, elements, clear)
        .map_err(|e| format!("{e:?}"))?;
    let mapping = renderer
        .copy_framebuffer(&fb, Rectangle::from_size(buffer_size), Fourcc::Abgr8888)
        .map_err(|e| format!("{e:?}"))?;
    let bytes = renderer.map_texture(&mapping).map_err(|e| format!("{e:?}"))?;
    Ok(bytes.to_vec())
}

/// Save RGBA pixels as PNG, or as binary PPM when the path ends in `.ppm`
/// (handy for tests that check exact pixel values).
pub fn write_png(path: &std::path::Path, size: Size<i32, Physical>, rgba: &[u8]) -> Result<(), String> {
    if path.extension().and_then(|e| e.to_str()) == Some("ppm") {
        let mut out = format!("P6\n{} {}\n255\n", size.w, size.h).into_bytes();
        out.extend(rgba.chunks_exact(4).flat_map(|p| [p[0], p[1], p[2]]));
        return std::fs::write(path, out).map_err(|e| format!("{}: {e}", path.display()));
    }
    let file = std::fs::File::create(path).map_err(|e| format!("{}: {e}", path.display()))?;
    let mut encoder = png::Encoder::new(std::io::BufWriter::new(file), size.w as u32, size.h as u32);
    encoder.set_color(png::ColorType::Rgba);
    encoder.set_depth(png::BitDepth::Eight);
    let mut writer = encoder.write_header().map_err(|e| e.to_string())?;
    writer.write_image_data(rgba).map_err(|e| e.to_string())
}
