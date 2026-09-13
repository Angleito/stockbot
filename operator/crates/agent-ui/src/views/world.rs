//! World screen: agent cards on top, event tail at the bottom.
//!
//! [`render_world`] is the entry point. It reads the highlight from
//! `world.selected` and never mutates. Layout is resize-safe: proportional
//! constraints only, empty areas are no-ops, over-long text truncates.

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    widgets::Paragraph,
};

use crate::ids::AgentId;
use crate::state::{AgentState, Selected, WorldState};

use super::agent_card::render_agent_card;
use super::event_stream::render_event_stream;

/// Bottom-strip height on roomy screens; shrunk on short ones.
const EVENTS_HEIGHT: u16 = 7;

/// Render the whole world into `area`.
pub fn render_world(frame: &mut Frame, area: Rect, world: &WorldState) {
    if area.is_empty() {
        return;
    }
    // Short screens (< 6 rows) skip the event strip; it needs border + 1 line.
    let show_events = area.height >= 6;
    if !show_events {
        render_agents(frame, area, world);
        return;
    }
    let events_h = EVENTS_HEIGHT.min(area.height / 2).max(3);
    let chunks = Layout::vertical([Constraint::Min(0), Constraint::Length(events_h)]).split(area);
    render_agents(frame, chunks[0], world);
    render_event_stream(frame, chunks[1], world);
}

/// Agent cards side by side in name order; `(no agents)` when empty.
fn render_agents(frame: &mut Frame, area: Rect, world: &WorldState) {
    if area.is_empty() {
        return;
    }
    let mut agents: Vec<&AgentState> = world.agents.values().collect();
    agents.sort_by(|a, b| a.name.cmp(&b.name));
    if agents.is_empty() {
        frame.render_widget(Paragraph::new("(no agents)"), area);
        return;
    }
    let columns = Layout::horizontal(vec![Constraint::Fill(1); agents.len()]).split(area);
    for (agent, column) in agents.iter().zip(columns.iter()) {
        render_agent_card(frame, *column, world, &agent.id, is_selected(world, agent));
    }
}

/// Hit-test a click at terminal cell `(x, y)` against the agent columns.
/// Mirrors [`render_world`]: the event strip (when shown) never hits, and
/// columns reuse the same horizontal `Fill(1)` split in name order so a
/// resize moves render and hit together. Outside content (or empty world)
/// returns `None`, never panics.
pub fn agent_at(area: Rect, world: &WorldState, x: u16, y: u16) -> Option<AgentId> {
    if area.is_empty() || world.agents.is_empty() {
        return None;
    }
    let in_area =
        x >= area.x && y >= area.y && x < area.x.saturating_add(area.width) && y < area.y.saturating_add(area.height);
    if !in_area {
        return None;
    }
    let mut agents: Vec<&AgentState> = world.agents.values().collect();
    agents.sort_by(|a, b| a.name.cmp(&b.name));
    // Same split as `render_world`: short screens skip the event strip.
    let show_events = area.height >= 6;
    let agents_area = if !show_events {
        area
    } else {
        let events_h = EVENTS_HEIGHT.min(area.height / 2).max(3);
        let chunks =
            Layout::vertical([Constraint::Min(0), Constraint::Length(events_h)]).split(area);
        // Event-strip exclusion: clicks on the tail select nothing.
        if y >= chunks[1].y {
            return None;
        }
        chunks[0]
    };
    if agents_area.is_empty() {
        return None;
    }
    let columns = Layout::horizontal(vec![Constraint::Fill(1); agents.len()]).split(agents_area);
    for (agent, column) in agents.iter().zip(columns.iter()) {
        let hit = x >= column.x
            && y >= column.y
            && x < column.x.saturating_add(column.width)
            && y < column.y.saturating_add(column.height);
        if hit {
            return Some(agent.id.clone());
        }
    }
    None
}

/// Highlight the selected agent, or the owner of the selected worker.
fn is_selected(world: &WorldState, agent: &AgentState) -> bool {
    match &world.selected {
        Some(Selected::Agent(id)) => id == &agent.id,
        Some(Selected::Worker(id)) => world
            .workers
            .get(id)
            .is_some_and(|worker| worker.agent_id == agent.id),
        None => false,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    use crate::fixtures::seed_world;

    /// Collect every visible cell row-major so tests stay backend-agnostic.
    fn screen_text(terminal: &Terminal<TestBackend>) -> String {
        let buffer = terminal.backend().buffer();
        let area = buffer.area;
        let mut out = String::new();
        for y in area.top()..area.bottom() {
            for x in area.left()..area.right() {
                out.push_str(buffer[(x, y)].symbol());
            }
            out.push('\n');
        }
        out
    }

    fn draw(world: &WorldState, width: u16, height: u16) -> String {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal
            .draw(|frame| render_world(frame, frame.area(), world))
            .unwrap();
        screen_text(&terminal)
    }

    #[test]
    fn world_shows_every_agent_and_event_tail() {
        let world = seed_world();
        assert_eq!(world.agents.len(), 3);
        let screen = draw(&world, 80, 24);
        for agent in world.agents.values() {
            assert!(screen.contains(agent.name.as_str()), "missing card");
        }
        let last = world.events.last().unwrap();
        assert!(screen.contains(last.summary.as_str()), "missing events");
    }

    #[test]
    fn tiny_screens_render_without_panic() {
        let world = seed_world();
        for (width, height) in [(80, 24), (40, 10), (20, 6), (8, 4), (1, 1)] {
            let _ = draw(&world, width, height);
        }
        let _ = draw(&WorldState::new(), 80, 24);
    }
    #[test]
    fn click_column_selects_agent_in_name_order() {
        use ratatui::layout::Rect;
        let world = seed_world();
        // Names sort MARKET, NEWS, SEC across 90 columns => 30 cells each.
        let area = Rect::new(0, 0, 90, 24);
        assert_eq!(
            agent_at(area, &world, 5, 2),
            Some(crate::ids::AgentId::new("agent-market"))
        );
        assert_eq!(
            agent_at(area, &world, 45, 2),
            Some(crate::ids::AgentId::new("agent-news"))
        );
        assert_eq!(
            agent_at(area, &world, 85, 2),
            Some(crate::ids::AgentId::new("agent-sec"))
        );
    }

    #[test]
    fn click_event_strip_and_outside_selects_nothing() {
        use ratatui::layout::Rect;
        let world = seed_world();
        let area = Rect::new(0, 0, 90, 24);
        // Bottom rows are the event tail (7 rows at height 24) => no hit.
        assert_eq!(agent_at(area, &world, 5, 23), None);
        assert_eq!(agent_at(area, &world, 45, 20), None);
        // Outside the view area => None, never panics.
        assert_eq!(agent_at(area, &world, 95, 2), None);
        assert_eq!(agent_at(area, &world, 5, 30), None);
    }

    #[test]
    fn empty_world_and_tiny_screens_never_hit_or_panic() {
        use ratatui::layout::Rect;
        let empty = WorldState::new();
        let area = Rect::new(0, 0, 90, 24);
        assert_eq!(agent_at(area, &empty, 5, 2), None);
        assert_eq!(agent_at(Rect::new(0, 0, 0, 0), &empty, 0, 0), None);
        let world = seed_world();
        // Short screens skip the event strip; hit-testing stays panic-free.
        let small = Rect::new(0, 0, 30, 4);
        let _ = agent_at(small, &world, 2, 1);
        let _ = agent_at(Rect::new(0, 0, 1, 1), &world, 0, 0);
        // Shell-offset areas hit by absolute cells; above the area misses.
        let offset = Rect::new(0, 1, 90, 22);
        assert_eq!(
            agent_at(offset, &world, 5, 2),
            Some(crate::ids::AgentId::new("agent-market"))
        );
        assert_eq!(agent_at(offset, &world, 5, 0), None);
    }
}
