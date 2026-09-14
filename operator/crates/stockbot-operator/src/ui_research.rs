//! Fake research pane: seeded world plus top-region-only view dispatcher.
//! Demo/fake only. Never renders the chat area; the caller clips `area` to
//! the top region and draws chat below.

use agent_ui::{
    View,
    views::{agent, logs, worker, world},
};
use ratatui::{Frame, layout::Rect};

use crate::workspace::{ResearchState, research_title};

/// Empty STARTING session for `query`; the app tick fills it per-tick.
/// Same title rule as `Workspace::start_research`.
pub fn fake_research(query: &str) -> ResearchState {
    ResearchState {
        world: agent_ui::WorldState::new(),
        tick: 0,
        title: research_title(query),
        query: query.trim().to_owned(),
        cursor: 0,
    }
}

/// Draw the research world into the TOP region only (`area`).
/// Empty world renders `TITLE` + STARTING (agents appear per-tick);
/// dispatch mirrors Overview -> Agent -> Worker -> Logs. Hit-testing stays
/// aligned via the reused `agent_at` / `worker_at` splits.
pub fn render_research(frame: &mut Frame, area: Rect, research: &ResearchState, view: &View) {
    use ratatui::{
        layout::{Constraint, Layout},
        style::{Color, Modifier, Style},
        text::Line,
        widgets::Paragraph,
    };
    if area.is_empty() {
        return;
    }
    if research.world.agents.is_empty() {
        let chunks =
            Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(area);
        frame.render_widget(
            Paragraph::new(vec![
                Line::styled(
                    research.title.clone(),
                    Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
                ),
                Line::styled("● STARTING — preparing research...", Style::default()),
            ]),
            chunks[0],
        );
        frame.render_widget(Paragraph::new("Preparing research..."), chunks[1]);
        return;
    }
    let chunks =
        Layout::vertical([Constraint::Length(2), Constraint::Min(0)]).split(area);
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                research.title.clone(),
                Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD),
            ),
            Line::styled("● RUNNING", Style::default().fg(Color::Green)),
        ]),
        chunks[0],
    );
    match view {
        View::World => world::render_world(frame, chunks[1], &research.world),
        View::Agent(id) => agent::render(frame, chunks[1], &research.world, id),
        View::Worker(id) => worker::render(frame, chunks[1], &research.world, id),
        View::Logs(id) => logs::render(frame, chunks[1], &research.world, id),
    }
}
