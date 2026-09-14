//! Herdr-style shell chrome: sidebar + full-height Pi chat.
//!
//! Demo/fake only. State lives in `crate::workspace`; this module only
//! splits layout and blits widgets. Never imports Herdr types.

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::{Color, Style, Stylize},
    text::{Line, Span},
    widgets::{Block, Paragraph, Wrap},
};

use crate::workspace::ChatState;

/// Fixed sidebar width; workspace takes the rest.
pub const SIDEBAR_WIDTH: u16 = 20;

/// Horizontal `sidebar | workspace` split.
pub fn root_split(area: Rect) -> (Rect, Rect) {
    if area.is_empty() {
        return (area, area);
    }
    let chunks = Layout::horizontal([Constraint::Length(SIDEBAR_WIDTH), Constraint::Min(0)])
        .split(area);
    (chunks[0], chunks[1])
}

/// Compact Herdr rows. `titles` is `(title, active, unread)` per workspace:
/// `active` drives the subtle status dot (green/gray), `unread` adds a `*`.
/// `selected` drives the `●/○` marker plus the yellow-bold-selected idiom
/// copied from `agent_card`; `scroll` is the first visible index.
/// A trailing `+ new` row fills leftover space.
pub fn render_sidebar(
    frame: &mut Frame,
    area: Rect,
    titles: &[(String, bool, bool)],
    selected: usize,
    scroll: u16,
) {
    if area.is_empty() {
        return;
    }
    let capacity = area.height.saturating_sub(2) as usize; // borders
    if capacity == 0 {
        frame.render_widget(Block::bordered().title("workspaces"), area);
        return;
    }
    let width = area.width.saturating_sub(2) as usize;
    let max_title = width.saturating_sub(5); // "● "+" ●"/" *"
    let mut lines: Vec<Line<'static>> = titles
        .iter()
        .enumerate()
        .skip(scroll as usize)
        .take(capacity.saturating_sub(1)) // reserve one row for `+ new`
        .map(|(i, (title, active, unread))| {
            let is_sel = i == selected;
            let row_style = if is_sel {
                Style::default().fg(Color::Yellow).bold()
            } else {
                Style::default()
            };
            let mut spans = vec![
                Span::styled(if is_sel { "● " } else { "○ " }, row_style),
                Span::styled(truncate(title, max_title), row_style),
            ];
            let (dot, dot_color) = if *active { (" ●", Color::Green) } else { (" ○", Color::Gray) };
            spans.push(Span::styled(dot.to_string(), Style::default().fg(dot_color)));
            if *unread {
                spans.push(Span::styled(" *", Style::default().fg(Color::Yellow)));
            }
            Line::from(spans)
        })
        .collect();
    if lines.len() < capacity {
        lines.push(Line::from(Span::styled(
            "+ new",
            Style::default().fg(Color::DarkGray),
        )));
    }
    frame.render_widget(
        Paragraph::new(lines).block(Block::bordered().title("workspaces")),
        area,
    );
}

/// Full-height Pi chat: `> user` history, `Stockbot:` replies, `> input`
/// with cursor. Shows the tail that fits; resize-safe.
pub fn render_chat(frame: &mut Frame, area: Rect, chat: &ChatState) {
    if area.is_empty() {
        return;
    }
    let chunks = Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(area);
    let mut lines: Vec<Line<'static>> = Vec::new();
    for msg in &chat.messages {
        if msg.from_user {
            for (j, part) in msg.text.split('\n').enumerate() {
                let prefix = if j == 0 { "> " } else { "  " };
                lines.push(Line::from(vec![
                    Span::styled(prefix.to_string(), Style::default().fg(Color::Gray)),
                    Span::raw(part.to_string()),
                ]));
            }
        } else {
            for part in msg.text.split('\n') {
                lines.push(Line::from(vec![
                    Span::styled("Stockbot: ", Style::default().fg(Color::Cyan)),
                    Span::raw(part.to_string()),
                ]));
            }
        }
    }
    let budget = chunks[0].height.saturating_sub(2) as usize; // borders
    let skip = lines.len().saturating_sub(budget);
    frame.render_widget(
        Paragraph::new(lines.into_iter().skip(skip).collect::<Vec<_>>())
            .block(Block::bordered().title("pi"))
            .wrap(Wrap { trim: true }),
        chunks[0],
    );
    frame.render_widget(
        Paragraph::new(format!("> {}▌", chat.input)).block(Block::bordered()),
        chunks[1],
    );
}

fn truncate(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    s.chars().take(max.saturating_sub(1)).collect::<String>() + "…"
}
