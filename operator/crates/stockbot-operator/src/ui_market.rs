//! Fake market startup splash + tiny market glyphs. Demo only.
//!
//! Never owns layout: every render takes the caller's `Rect` and draws inside
//! it only (no splits, no `Layout`). The integrator owns all splitting and
//! calls [`StartupAnim::skip`] on Esc/Enter/Space.

use agent_ui_ttfx::{Animation, TtfxAnimation};
use crossterm::event::KeyCode;
use ratatui::{
    Frame,
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::Paragraph,
};

/// ~1.5s at the shell's 50ms tick.
pub struct StartupAnim {
    pub frame: usize,
}

impl StartupAnim {
    pub const TOTAL: usize = 30;

    pub fn new() -> Self {
        Self { frame: 0 }
    }

    pub fn tick(&mut self) {
        if !self.done() {
            self.frame += 1;
        }
    }

    pub fn done(&self) -> bool {
        self.frame >= Self::TOTAL
    }

    pub fn skip(&mut self) {
        self.frame = Self::TOTAL;
    }

    /// Keys the integrator maps to [`StartupAnim::skip`].
    pub fn skip_key(key: KeyCode) -> bool {
        matches!(key, KeyCode::Esc | KeyCode::Enter | KeyCode::Char(' '))
    }

    /// Candle wicks -> NVDA/SPY/AMD/VIX tickers -> price trace -> STOCKBOT.
    pub fn render(&self, frame: &mut Frame, area: Rect) {
        if area.is_empty() {
            return;
        }
        let dim = Style::default().fg(Color::DarkGray);
        let mut lines: Vec<Line> = vec![Line::styled("  │  ┃  │  ┃  │  ┃", dim)];
        if self.frame >= 4 {
            lines.push(Line::styled("  ┃  │  ┃  │  ┃  │", dim));
        }
        if self.frame >= 8 {
            let n = ((self.frame - 8) / 2 + 1).min(TICKERS.len());
            for (sym, price, chg) in &TICKERS[..n] {
                let fg = if *chg >= 0.0 { Color::Green } else { Color::Red };
                lines.push(Line::from(vec![
                    Span::styled(
                        format!("{sym:<4} "),
                        Style::default().add_modifier(Modifier::BOLD),
                    ),
                    Span::raw(format!("{price:>7.2} ")),
                    Span::styled(format!("{chg:+.1}%"), Style::default().fg(fg)),
                ]));
            }
        }
        if self.frame >= 16 {
            let n = ((self.frame - 16) * 2 + 2).min(TRACE.len());
            let (lo, hi) = TRACE.iter().fold((u64::MAX, 0), |(l, h), v| (l.min(*v), h.max(*v)));
            let s: String = TRACE[..n].iter().map(|v| block_for(*v, lo, hi)).collect();
            lines.push(Line::styled(format!("TRACE {s}"), Style::default().fg(Color::Cyan)));
        }
        if self.frame >= 24 {
            lines.push(Line::styled(
                "STOCKBOT",
                Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
            ));
            lines.push(Line::styled("fake market open — Esc skips", dim));
        }
        frame.render_widget(Paragraph::new(lines), area);
    }
}

impl Default for StartupAnim {
    fn default() -> Self {
        Self::new()
    }
}

const TICKERS: [(&str, f64, f64); 4] = [
    ("NVDA", 187.42, 1.8),
    ("SPY", 572.10, 0.4),
    ("AMD", 122.55, -0.6),
    ("VIX", 14.22, -2.1),
];

const TRACE: [u64; 16] = [3, 5, 4, 6, 5, 7, 6, 8, 7, 9, 8, 10, 9, 11, 12, 11];

const BLOCKS: [char; 4] = ['▁', '▃', '▅', '▇'];

fn block_for(v: u64, min: u64, max: u64) -> char {
    if max <= min {
        return BLOCKS[1];
    }
    let idx = ((v - min) * (BLOCKS.len() as u64 - 1) / (max - min)) as usize;
    BLOCKS[idx]
}

/// Rightmost values mapped onto ▁▃▅▇, clipped to `area`.
pub fn sparkline(frame: &mut Frame, area: Rect, values: &[u64]) {
    if area.is_empty() || values.is_empty() {
        return;
    }
    let n = (area.width as usize).min(values.len());
    let tail = &values[values.len() - n..];
    let (lo, hi) = tail.iter().fold((u64::MAX, 0), |(l, h), v| (l.min(*v), h.max(*v)));
    let s: String = tail.iter().map(|v| block_for(*v, lo, hi)).collect();
    frame.render_widget(Paragraph::new(s), area);
}

/// Same ▁▃▅▇ alphabet as [`sparkline`], dimmed for volume rows.
pub fn volume_blocks(frame: &mut Frame, area: Rect, volumes: &[u64]) {
    if area.is_empty() || volumes.is_empty() {
        return;
    }
    let n = (area.width as usize).min(volumes.len());
    let tail = &volumes[volumes.len() - n..];
    let (lo, hi) = tail.iter().fold((u64::MAX, 0), |(l, h), v| (l.min(*v), h.max(*v)));
    let s: String = tail.iter().map(|v| block_for(*v, lo, hi)).collect();
    frame.render_widget(
        Paragraph::new(s).style(Style::default().fg(Color::DarkGray)),
        area,
    );
}

/// Alternating ● / ◉; ticks 0,1,2 render ●◉●.
pub fn status_pulse_glyph(tick: usize) -> &'static str {
    if tick % 2 == 0 { "●" } else { "◉" }
}

/// Single-glyph pulse clipped to `area`.
pub fn status_pulse(frame: &mut Frame, area: Rect, tick: usize) {
    if area.is_empty() {
        return;
    }
    frame.render_widget(
        Paragraph::new(status_pulse_glyph(tick)).style(Style::default().fg(Color::Green)),
        area,
    );
}

/// Thin TTFX input: owns no layout, just normalizes the title string.
/// Render it via [`TransientTitle`], which clips to the caller's `Rect`.
pub fn transient_title(s: &str) -> String {
    s.to_string()
}

/// Region-clipped decrypt over one title line
/// (`STOCKBOT` / `RESEARCH STARTING` / `agent-spawned …`). Never layouts.
pub struct TransientTitle {
    inner: TtfxAnimation,
}

impl TransientTitle {
    pub fn new(text: &str) -> Self {
        Self { inner: TtfxAnimation::new(vec![transient_title(text)]) }
    }
}

impl Animation for TransientTitle {
    fn resize(&mut self, width: u16, height: u16) {
        self.inner.resize(width, height);
    }

    fn tick(&mut self) {
        self.inner.tick();
    }

    fn render(&self, area: Rect, buf: &mut Buffer) {
        self.inner.render(area, buf);
    }

    fn finished(&self) -> bool {
        self.inner.finished()
    }
}
