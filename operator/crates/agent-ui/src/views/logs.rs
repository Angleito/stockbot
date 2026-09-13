//! Raw log view: ground truth for one worker.
//!
//! Renders [`WorkerState::logs`] verbatim, oldest first, with no truncation,
//! filtering, or formatting. Empty logs and unknown ids render a placeholder
//! line instead of panicking.

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    widgets::{Block, Borders, Paragraph},
};

use crate::{ids::WorkerId, state::WorldState};

/// Pure log lines: the worker's raw log buffer verbatim. Empty when the
/// worker is unknown or has logged nothing.
pub fn log_lines(world: &WorldState, id: &WorkerId) -> Vec<String> {
    world
        .workers
        .get(id)
        .map(|w| w.logs.clone())
        .unwrap_or_default()
}

/// Resize-safe render: header + raw lines + hint. Never panics on small
/// areas or missing workers.
pub fn render(frame: &mut Frame, area: Rect, world: &WorldState, id: &WorkerId) {
    let name = world
        .workers
        .get(id)
        .map(|w| w.name.clone())
        .unwrap_or_else(|| "<unknown>".to_owned());
    let chunks = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(area);
    let lines = log_lines(world, id);
    let body = if lines.is_empty() {
        "(no log lines)".to_owned()
    } else {
        lines.join("\n")
    };
    frame.render_widget(
        Paragraph::new(body).block(
            Block::default()
                .borders(Borders::ALL)
                .title(format!("logs: {name}")),
        ),
        chunks[0],
    );
    frame.render_widget(Paragraph::new("q: back"), chunks[1]);
}
