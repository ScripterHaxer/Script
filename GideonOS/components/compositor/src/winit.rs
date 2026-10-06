//! Nested development backend: the compositor runs inside a window (X11 or
//! Wayland host), rendering with GLES. Not used on a real GideonOS session.

use std::time::Duration;

use smithay::{
    backend::{
        renderer::{
            damage::OutputDamageTracker,
            gles::{GlesRenderbuffer, GlesRenderer},
        },
        winit::{self, WinitEvent, WinitGraphicsBackend},
    },
    output::{Mode, Output, PhysicalProperties, Subpixel},
    reexports::calloop::EventLoop,
    utils::{Physical, Rectangle, Size, Transform},
};

use crate::{render, state::Gideon, App, Backend};

pub struct WinitBackend {
    backend: WinitGraphicsBackend<GlesRenderer>,
    output: Output,
    damage_tracker: OutputDamageTracker,
}

impl WinitBackend {
    pub fn init(event_loop: &mut EventLoop<'static, App>, state: &mut Gideon) -> Result<WinitBackend, String> {
        let (backend, winit_loop) = winit::init::<GlesRenderer>().map_err(|e| e.to_string())?;
        let mode = Mode { size: backend.window_size(), refresh: 60_000 };
        let output = Output::new(
            "WINIT-1".into(),
            PhysicalProperties { size: (0, 0).into(), subpixel: Subpixel::Unknown, make: "GideonOS".into(), model: "nested".into() },
        );
        let _global = output.create_global::<Gideon>(&state.display_handle);
        output.change_current_state(
            Some(mode),
            Some(Transform::Flipped180),
            Some(smithay::output::Scale::Fractional(state.config.scale)),
            Some((0, 0).into()),
        );
        output.set_preferred(mode);
        state.space.map_output(&output, (0, 0));
        let damage_tracker = OutputDamageTracker::from_output(&output);

        event_loop
            .handle()
            .insert_source(winit_loop, |event, _, app| {
                let Backend::Winit(w) = &mut app.backend else { return };
                match event {
                    WinitEvent::Resized { size, .. } => {
                        w.output.change_current_state(Some(Mode { size, refresh: 60_000 }), None, None, None);
                        app.state.arrange_outputs();
                    }
                    WinitEvent::Input(event) => app.state.process_input(event),
                    WinitEvent::Redraw => w.render(&mut app.state),
                    WinitEvent::CloseRequested => app.state.loop_signal.stop(),
                    _ => {}
                }
            })
            .map_err(|e| e.to_string())?;

        Ok(WinitBackend { backend, output, damage_tracker })
    }

    fn render(&mut self, state: &mut Gideon) {
        let size = self.backend.window_size();
        let age = self.backend.buffer_age().unwrap_or(0);
        let rendered = {
            let Ok((renderer, mut fb)) = self.backend.bind() else { return };
            let (elements, animating) = render::output_elements(state, renderer, &self.output);
            if animating {
                state.redraw = true;
            }
            self.damage_tracker
                .render_output(renderer, &mut fb, age, &elements, render::clear_color(state))
                .map(|r| r.damage.cloned())
        };
        match rendered {
            Ok(damage) => {
                let damage = damage.unwrap_or_else(|| vec![Rectangle::from_size(size)]);
                if let Err(err) = self.backend.submit(Some(&damage)) {
                    tracing::warn!(%err, "submit failed");
                }
            }
            Err(err) => tracing::warn!(?err, "render failed"),
        }
        let time = state.start_time.elapsed();
        for window in state.space.elements() {
            window.send_frame(&self.output, time, Some(Duration::ZERO), |_, _| Some(self.output.clone()));
        }
    }

    pub fn after_dispatch(&mut self, state: &mut Gideon) {
        if state.redraw {
            state.redraw = false;
            self.backend.window().request_redraw();
        }
    }

    pub fn screenshot(&mut self, state: &mut Gideon, output: &Output) -> Result<(Size<i32, Physical>, Vec<u8>), String> {
        let size = output.current_mode().ok_or("no mode")?.size;
        let scale = output.current_scale().fractional_scale();
        let renderer = self.backend.renderer();
        let (elements, _) = render::output_elements(state, renderer, output);
        let clear = render::clear_color(state);
        let rgba = render::capture::<_, GlesRenderbuffer>(renderer, &elements, size, scale, clear)?;
        Ok((size, rgba))
    }
}
