//! One agent card: status header plus one progress line per worker.
//!
//! Pure builders ([`worker_lines`], [`agent_card`]) take `&WorldState` plus
//! selection and never mutate; [`render_agent_card`] blits the widget.

use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Style, Stylize},
    text::{Line, Span},
    widgets::{Block, Paragraph},
};

use crate::{
    ids::AgentId,
    state::{AgentState, WorldState},
};

/// One progress line per worker, in registration order.
pub fn worker_lines(world: &WorldState, agent_id: &AgentId) -> Vec<Line<'static>> {
    let Some(agent) = world.agents.get(agent_id) else {
        return vec![Line::from("(gone)")];
    };
    if agent.workers.is_empty() {
        return vec![Line::from("(no workers)")];
    }
    agent.workers
        .iter()
        .map(|wid| match world.workers.get(wid) {
            None => Line::from("(gone)"),
            Some(worker) => {
                let dot = Span::styled(
                    super::status::dot(worker.status),
                    super::status::style(worker.status),
                );
                let rest = Span::raw(format!(
                    " {} {}% {}/{} {:?}",
                    worker.name,
                    worker.progress.percent(),
                    worker.progress.done,
                    worker.progress.total,
                    worker.status,
                ));
                Line::from(vec![dot, rest])
            }
        })
        .collect()
}

/// Card title: status dot plus name and status text.
fn title(agent: &AgentState) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            super::status::dot(agent.status),
            super::status::style(agent.status),
        ),
        Span::raw(format!(" {} {:?}", agent.name, agent.status)),
    ])
}

/// Pure widget builder; `selected` drives the border highlight.
pub fn agent_card(world: &WorldState, agent_id: &AgentId, selected: bool) -> Paragraph<'static> {
    let Some(agent) = world.agents.get(agent_id) else {
        return Paragraph::new("(gone)").block(Block::bordered().title("?"));
    };
    let mut block = Block::bordered().title(title(agent));
    if selected {
        block = block.border_style(Style::default().fg(Color::Yellow).bold());
    }
    Paragraph::new(worker_lines(world, agent_id)).block(block)
}

/// Frame helper; no-op on empty areas so tiny terminals never panic.
pub fn render_agent_card(
    frame: &mut Frame,
    area: Rect,
    world: &WorldState,
    agent_id: &AgentId,
    selected: bool,
) {
    if area.is_empty() {
        return;
    }
    frame.render_widget(agent_card(world, agent_id, selected), area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::seed_world;

    #[test]
    fn worker_lines_match_registered_worker_count() {
        let world = seed_world();
        for agent in world.agents.values() {
            assert_eq!(
                worker_lines(&world, &agent.id).len().max(1),
                agent.workers.len().max(1)
            );
        }
    }

    #[test]
    fn unknown_agent_renders_placeholder_without_panic() {
        let world = seed_world();
        // Widget builder must not panic; line builder reports the miss.
        let _card = agent_card(&world, &AgentId::new("no-such-agent"), false);
        assert_eq!(
            worker_lines(&world, &AgentId::new("no-such-agent")),
            vec![Line::from("(gone)")]
        );
    }
}
