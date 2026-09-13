//! Generic agent children tree.
//!
//! Renders [`AgentState`] plus its workers from [`WorldState`] relationships
//! only (`AgentState.workers` joined against `WorldState.workers`). Never
//! matches on names. Status dots are static text; edges are static box
//! glyphs, never animated.

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    style::{Color, Style},
    text::{Line, Span},
    widgets::{Block, Borders, List, ListItem, Paragraph},
};

use crate::{
    ids::{AgentId, WorkerId},
    state::WorldState,
    status::AgentStatus,
};

/// Static dot for a lifecycle state. Color carries the meaning; the glyph
/// stays a dot (idle states use the hollow dot).
pub fn status_glyph(status: AgentStatus) -> &'static str {
    match status {
        AgentStatus::Working => "●",
        AgentStatus::Idle | AgentStatus::Waiting => "○",
        AgentStatus::Blocked | AgentStatus::Done | AgentStatus::Failed => "●",
    }
}

/// Generic status word. No domain language.
pub fn status_label(status: AgentStatus) -> &'static str {
    match status {
        AgentStatus::Working => "running",
        AgentStatus::Idle => "idle",
        AgentStatus::Waiting => "idle",
        AgentStatus::Blocked => "blocked",
        AgentStatus::Done => "completed",
        AgentStatus::Failed => "failed",
    }
}

fn status_color(status: AgentStatus) -> Color {
    match status {
        AgentStatus::Working => Color::Green,
        AgentStatus::Idle | AgentStatus::Waiting => Color::Gray,
        AgentStatus::Blocked => Color::Yellow,
        AgentStatus::Done => Color::Blue,
        AgentStatus::Failed => Color::Red,
    }
}

/// Pure tree lines for tests and screenshots. First line is the agent, the
/// rest are `AgentState.workers` in order; missing workers render as
/// `<missing>`.
pub fn agent_lines(agent: &crate::state::AgentState, world: &WorldState) -> Vec<String> {
    let mut out = vec![format!(
        "{} {} [{}]",
        status_glyph(agent.status),
        agent.name,
        status_label(agent.status)
    )];
    let last = agent.workers.len().saturating_sub(1);
    for (i, wid) in agent.workers.iter().enumerate() {
        let edge = if i == last { "└─" } else { "├─" };
        match world.workers.get(wid) {
            Some(w) => out.push(format!(
                "{edge} {} {} [{}]",
                status_glyph(w.status),
                w.name,
                status_label(w.status)
            )),
            None => out.push(format!("{edge} ○ <missing>")),
        }
    }
    out
}

/// Resize-safe render: header + tree list + hint. No panics down to 80x24;
/// unknown id renders a placeholder.
pub fn render(frame: &mut Frame, area: Rect, world: &WorldState, id: &AgentId) {
    let Some(agent) = world.agents.get(id) else {
        frame.render_widget(
            Paragraph::new("unknown agent").block(block("agent")),
            area,
        );
        return;
    };
    let chunks = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(1),
        Constraint::Length(1),
    ])
    .split(area);
    let header = Paragraph::new(Line::from(vec![
        Span::styled(
            status_glyph(agent.status).to_string(),
            Style::default().fg(status_color(agent.status)),
        ),
        Span::raw(format!(" {} [{}]", agent.name, status_label(agent.status))),
    ]))
    .block(block("agent"));
    frame.render_widget(header, chunks[0]);

    let items: Vec<ListItem> = agent_lines(agent, world)
        .into_iter()
        .skip(1)
        .map(|l| ListItem::new(Line::raw(l)))
        .collect();
    let list = List::new(items).block(Block::default().borders(Borders::ALL).title("workers"));
    frame.render_widget(list, chunks[1]);

    frame.render_widget(Paragraph::new("enter: open worker · q: back"), chunks[2]);
}
/// Hit-test a click at terminal cell `(x, y)` against the worker list rows.
/// Mirrors [`render`]: the same header-3 / list / hint-1 vertical split, with
/// list row `i` mapping to `agent.workers[i]` in order. Header, hint, list
/// borders, empty space past the last worker, unknown ids, and clicks
/// outside the list all return `None`, never panic.
pub fn worker_at(
    area: Rect,
    world: &WorldState,
    id: &AgentId,
    x: u16,
    y: u16,
) -> Option<WorkerId> {
    if area.is_empty() {
        return None;
    }
    let agent = world.agents.get(id)?;
    if agent.workers.is_empty() {
        return None;
    }
    if x < area.x
        || y < area.y
        || x >= area.x.saturating_add(area.width)
        || y >= area.y.saturating_add(area.height)
    {
        return None;
    }
    // Same split as `render`: header 3, list fill, hint 1.
    let chunks = Layout::vertical([
        Constraint::Length(3),
        Constraint::Min(1),
        Constraint::Length(1),
    ])
    .split(area);
    let list = chunks[1];
    if list.is_empty() {
        return None;
    }
    if x < list.x
        || y < list.y
        || x >= list.x.saturating_add(list.width)
        || y >= list.y.saturating_add(list.height)
    {
        return None;
    }
    // The list carries an ALL-borders block: rows start one cell in and the
    // last row sits above the bottom border.
    let inner_h = list.height.saturating_sub(2);
    if inner_h == 0 {
        return None;
    }
    let inner_y = list.y.saturating_add(1);
    let inner_right = list.x.saturating_add(list.width).saturating_sub(1);
    if y < inner_y || y >= inner_y.saturating_add(inner_h) {
        return None;
    }
    if x <= list.x || x >= inner_right {
        return None;
    }
    let row = y.saturating_sub(inner_y) as usize;
    // Rows map 1:1 to `workers[i]`; clicks past the last worker miss.
    agent.workers.get(row).cloned()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::seed_world;

    fn area() -> Rect {
        Rect::new(0, 0, 80, 24)
    }

    fn list_first_row(area: Rect) -> u16 {
        let chunks = Layout::vertical([
            Constraint::Length(3),
            Constraint::Min(1),
            Constraint::Length(1),
        ])
        .split(area);
        chunks[1].y.saturating_add(1)
    }

    #[test]
    fn click_row_selects_worker_in_order() {
        let world = seed_world();
        let id = AgentId::new("agent-sec");
        let first = list_first_row(area());
        // agent-sec workers render in order: 8k, form4, 10q.
        assert_eq!(
            worker_at(area(), &world, &id, 5, first),
            Some(crate::ids::WorkerId::new("worker-8k"))
        );
        assert_eq!(
            worker_at(area(), &world, &id, 5, first + 1),
            Some(crate::ids::WorkerId::new("worker-form4"))
        );
        assert_eq!(
            worker_at(area(), &world, &id, 5, first + 2),
            Some(crate::ids::WorkerId::new("worker-10q"))
        );
    }

    #[test]
    fn click_header_hint_borders_and_empty_space_miss() {
        let world = seed_world();
        let id = AgentId::new("agent-sec");
        let view = area();
        let chunks = Layout::vertical([
            Constraint::Length(3),
            Constraint::Min(1),
            Constraint::Length(1),
        ])
        .split(view);
        // Header row and hint row never hit.
        assert_eq!(worker_at(view, &world, &id, 5, chunks[0].y), None);
        assert_eq!(worker_at(view, &world, &id, 5, chunks[2].y), None);
        // List border cells never hit.
        assert_eq!(worker_at(view, &world, &id, chunks[1].x, chunks[1].y + 1), None);
        // Empty space past the last worker (3 workers, tall list) misses.
        let first = list_first_row(view);
        assert_eq!(worker_at(view, &world, &id, 5, first + 3), None);
        // Outside the view misses, never panics.
        assert_eq!(worker_at(view, &world, &id, 5, 30), None);
    }

    #[test]
    fn unknown_id_empty_and_tiny_screens_miss_without_panic() {
        let world = seed_world();
        let view = area();
        let first = list_first_row(view);
        assert_eq!(
            worker_at(view, &world, &AgentId::new("nope"), 5, first),
            None
        );
        let empty = WorldState::new();
        assert_eq!(
            worker_at(view, &empty, &AgentId::new("agent-sec"), 5, first),
            None
        );
        assert_eq!(
            worker_at(Rect::new(0, 0, 0, 0), &world, &AgentId::new("agent-sec"), 0, 0),
            None
        );
        for (w, h) in [(80, 24), (40, 10), (20, 6), (8, 4), (1, 1)] {
            let tiny = Rect::new(0, 0, w, h);
            let _ = worker_at(tiny, &world, &AgentId::new("agent-sec"), 1, 1);
            let _ = worker_at(tiny, &world, &AgentId::new("nope"), 0, 0);
        }
    }
}

fn block(title: &'static str) -> Block<'static> {
    Block::default().borders(Borders::ALL).title(title)
}
