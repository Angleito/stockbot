//! Region-clipped decrypt stub. No stdout/terminal ownership:
//! everything draws into the caller's `Rect`/`Buffer` only.

use ratatui::{buffer::Buffer, layout::Rect};

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

/// Scramble glyphs for unrevealed cells (deterministic cycle, no RNG).
const SCRAMBLE: [char; 8] = ['#', '@', '%', '&', '*', '+', '=', '?'];

/// Packed-cell decrypt: reveals left-to-right, top-to-bottom, one row per
/// tick; unrevealed non-blank cells show a deterministic scramble glyph.
pub struct TtfxAnimation {
    cells: Vec<Vec<char>>,
    width: u16,
    height: u16,
    view_w: u16,
    view_h: u16,
    revealed: usize,
    frame: usize,
}

impl TtfxAnimation {
    /// Pack target lines (short rows space-padded to the longest).
    pub fn new(lines: Vec<String>) -> Self {
        let width = lines
            .iter()
            .map(|l| l.chars().count() as u16)
            .max()
            .unwrap_or(0);
        let height = lines.len() as u16;
        let cells = lines
            .into_iter()
            .map(|l| {
                let mut row: Vec<char> = l.chars().collect();
                row.resize(width as usize, ' ');
                row
            })
            .collect();
        Self {
            cells,
            width,
            height,
            view_w: width,
            view_h: height,
            revealed: 0,
            frame: 0,
        }
    }

    /// Blank placeholder of the given size.
    pub fn with_size(width: u16, height: u16) -> Self {
        Self {
            cells: vec![vec![' '; width as usize]; height as usize],
            width,
            height,
            view_w: width,
            view_h: height,
            revealed: 0,
            frame: 0,
        }
    }

    fn total(&self) -> usize {
        self.width as usize * self.height as usize
    }

    fn target(&self, row: usize, col: usize) -> char {
        self.cells[row][col]
    }
}

impl Animation for TtfxAnimation {
    fn resize(&mut self, width: u16, height: u16) {
        self.view_w = width;
        self.view_h = height;
    }

    fn tick(&mut self) {
        if self.finished() {
            return;
        }
        self.frame += 1;
        let step = self.width.max(1) as usize;
        self.revealed = (self.revealed + step).min(self.total());
    }

    fn render(&self, area: Rect, buf: &mut Buffer) {
        if area.is_empty() {
            return;
        }
        let rows = (self.height.min(self.view_h).min(area.height)) as usize;
        let cols = (self.width.min(self.view_w).min(area.width)) as usize;
        for r in 0..rows {
            for c in 0..cols {
                let idx = r * self.width as usize + c;
                let t = self.target(r, c);
                let ch = if t == ' ' || idx < self.revealed {
                    t
                } else {
                    SCRAMBLE[(idx + self.frame) % SCRAMBLE.len()]
                };
                if let Some(cell) = buf.cell_mut((area.x + c as u16, area.y + r as u16)) {
                    cell.set_char(ch);
                }
            }
        }
    }

    fn finished(&self) -> bool {
        self.revealed >= self.total()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn decrypt_lines() -> Vec<String> {
        (0..8)
            .map(|i| format!("decrypt stub row {i:02} data......"))
            .map(|mut s| {
                s.truncate(30);
                while s.len() < 30 {
                    s.push('.');
                }
                s
            })
            .collect()
    }

    #[test]
    fn decrypt_30x8_runs_and_leaves_outside_untouched() {
        let lines = decrypt_lines();
        assert_eq!(lines.len(), 8);
        assert!(lines.iter().all(|l| l.chars().count() == 30));

        let mut anim = TtfxAnimation::new(lines.clone());
        assert!(!anim.finished());

        let area = Rect::new(5, 2, 30, 8);
        let mut buf = Buffer::empty(Rect::new(0, 0, 40, 12));
        for y in 0..12 {
            for x in 0..40 {
                if let Some(cell) = buf.cell_mut((x, y)) {
                    cell.set_char('X');
                }
            }
        }

        // Pre-finish frames animate but never leak outside the region.
        anim.tick();
        anim.render(area, &mut buf);
        for y in 0..12 {
            for x in 0..40 {
                let inside = x >= 5 && x < 35 && y >= 2 && y < 10;
                if !inside {
                    assert_eq!(buf.cell((x, y)).unwrap().symbol(), "X", "leak at {x},{y}");
                }
            }
        }

        for _ in 0..100 {
            if anim.finished() {
                break;
            }
            anim.tick();
            anim.render(area, &mut buf);
        }
        assert!(anim.finished());

        // Settled frame equals the static target text.
        for (r, line) in lines.iter().enumerate() {
            for (c, want) in line.chars().enumerate() {
                let got = buf
                    .cell((area.x + c as u16, area.y + r as u16))
                    .unwrap()
                    .symbol()
                    .to_owned();
                assert_eq!(got, want.to_string(), "mismatch at {c},{r}");
            }
        }
        // Outside still untouched after the full run.
        for y in 0..12 {
            for x in 0..40 {
                let inside = x >= 5 && x < 35 && y >= 2 && y < 10;
                if !inside {
                    assert_eq!(buf.cell((x, y)).unwrap().symbol(), "X", "leak at {x},{y}");
                }
            }
        }

        // Finished animation is static: further ticks change nothing.
        let settled = buf.content.clone();
        anim.tick();
        anim.render(area, &mut buf);
        assert_eq!(buf.content, settled);
    }
}
