//! Region-clipped decrypt animation backed by `ttfx::Session`.
//! No stdout/terminal ownership: everything draws into the caller's
//! `Rect`/`Buffer` only.

use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier},
};

/// Frame-driven region animation. Settles to static UI when [`Animation::finished`].
pub trait Animation {
    /// Viewport hint; [`Animation::render`] still clips to its `Rect` argument.
    fn resize(&mut self, width: u16, height: u16);
    /// Advance one frame; no-op once [`Animation::finished`].
    fn tick(&mut self);
    /// Draw current frame clipped to `area`; never touches cells outside it.
    fn render(&self, area: Rect, buf: &mut Buffer);
    /// True when the frame equals the final static content.
    fn finished(&self) -> bool;
}

/// Deterministic seed so the same input always plays the same frames.
const SEED: u64 = 42;

fn options() -> ttfx::SessionOptions {
    ttfx::SessionOptions {
        seed: Some(SEED),
        frame_rate: 30,
        palette: None,
        background: None,
        bands: false,
    }
}

/// Decrypt effect over the joined lines, clipped to `area` on render.
///
/// Session dims are fixed at construction; [`Animation::resize`] is a
/// viewport hint only and never rebuilds (so it never restarts) the effect.
pub struct TtfxAnimation {
    session: ttfx::Session,
    view_w: u16,
    view_h: u16,
}

impl TtfxAnimation {
    /// Join `lines` to text; dims are max line length x line count (min 1x1).
    /// Viewport starts at full dims.
    pub fn new(lines: Vec<String>) -> Self {
        let width = lines
            .iter()
            .map(|l| l.chars().count() as u32)
            .max()
            .unwrap_or(0)
            .max(1);
        let height = (lines.len() as u32).max(1);
        let text = lines.join("\n");
        let session = ttfx::Session::new_with_options(&text, "decrypt", width, height, options())
            .expect("decrypt session");
        Self { session, view_w: width as u16, view_h: height as u16 }
    }

    /// Blank placeholder of the given size.
    pub fn with_size(width: u16, height: u16) -> Self {
        let session =
            ttfx::Session::new_with_options("", "decrypt", width as u32, height as u32, options())
                .expect("decrypt session");
        Self { session, view_w: width, view_h: height }
    }

    fn rgb(raw: u32) -> Color {
        Color::Rgb(
            ((raw >> 16) & 0xFF) as u8,
            ((raw >> 8) & 0xFF) as u8,
            (raw & 0xFF) as u8,
        )
    }
}

impl Animation for TtfxAnimation {
    /// Viewport hint: clamps [`Animation::render`] only. Never touches the
    /// session, so repeated layout passes with any dims can't restart it.
    fn resize(&mut self, width: u16, height: u16) {
        self.view_w = width;
        self.view_h = height;
    }

    fn tick(&mut self) {
        if self.finished() {
            return;
        }
        self.session.advance();
    }

    fn render(&self, area: Rect, buf: &mut Buffer) {
        if area.is_empty() {
            return;
        }
        let frame = self.session.frame();
        let rows = (self.session.height() as u16).min(self.view_h).min(area.height);
        let cols = (self.session.width() as u16).min(self.view_w).min(area.width);
        for y in 0..rows {
            for x in 0..cols {
                let Some((symbol, fg, bg, flags)) = frame.get(x as usize, y as usize) else {
                    continue;
                };
                let Some(cell) = buf.cell_mut((area.x + x, area.y + y)) else {
                    continue;
                };
                cell.set_char(if symbol == 0 {
                    ' '
                } else {
                    char::from_u32(symbol).unwrap_or(' ')
                });
                if fg != 0 {
                    cell.set_fg(Self::rgb(fg));
                }
                if bg != 0 {
                    cell.set_bg(Self::rgb(bg));
                }
                if flags & 1 != 0 {
                    cell.modifier.insert(Modifier::BOLD);
                }
            }
        }
    }

    fn finished(&self) -> bool {
        self.session.done()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lines() -> Vec<String> {
        vec![
            "decrypt demo line one......".to_string(),
            "decrypt demo line two......".to_string(),
        ]
    }

    fn symbols(area: Rect, buf: &Buffer) -> Vec<String> {
        (0..area.height)
            .map(|y| {
                (0..area.width)
                    .map(|x| {
                        buf.cell((area.x + x, area.y + y))
                            .unwrap()
                            .symbol()
                            .to_owned()
                    })
                    .collect()
            })
            .collect()
    }

    #[test]
    fn deterministic_seed_same_frame_twice() {
        let mut a = TtfxAnimation::new(lines());
        let mut b = TtfxAnimation::new(lines());
        for _ in 0..5 {
            a.tick();
            b.tick();
        }
        let area = Rect::new(0, 0, 26, 2);
        let mut ba = Buffer::empty(area);
        let mut bb = Buffer::empty(area);
        a.render(area, &mut ba);
        b.render(area, &mut bb);
        assert_eq!(symbols(area, &ba), symbols(area, &bb));
    }

    #[test]
    fn tick_advances_frame() {
        let mut anim = TtfxAnimation::new(lines());
        let area = Rect::new(0, 0, 26, 2);
        let mut before = Buffer::empty(area);
        anim.render(area, &mut before);
        let still_blank = symbols(area, &before)
            .iter()
            .all(|row| row.chars().all(|s| s == ' '));
        assert!(still_blank);
        anim.tick();
        let mut after = Buffer::empty(area);
        anim.render(area, &mut after);
        assert_ne!(symbols(area, &before), symbols(area, &after));
    }

    #[test]
    fn render_clips_to_rect() {
        let mut anim = TtfxAnimation::new(lines());
        anim.resize(2, 1);
        for _ in 0..5 {
            anim.tick();
        }
        let mut buf = Buffer::empty(Rect::new(0, 0, 26, 2));
        let area = Rect::new(0, 0, 2, 1);
        anim.render(area, &mut buf);
        for y in 0..2 {
            for x in 0..26 {
                let inside = x < 2 && y < 1;
                if !inside {
                    assert_eq!(buf.cell((x, y)).unwrap().symbol(), " ");
                }
            }
        }
    }

    #[test]
    fn finished_eventually_true_and_static() {
        let mut anim = TtfxAnimation::new(lines());
        for _ in 0..10_000 {
            if anim.finished() {
                break;
            }
            anim.tick();
        }
        assert!(anim.finished());
        let area = Rect::new(0, 0, 26, 2);
        let mut buf = Buffer::empty(area);
        anim.render(area, &mut buf);
        let settled = buf.content.clone();
        anim.tick();
        anim.render(area, &mut buf);
        assert_eq!(buf.content, settled);
    }
}
