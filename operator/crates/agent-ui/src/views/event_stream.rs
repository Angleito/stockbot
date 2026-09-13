//! Bottom event tail: the last N audit summaries, oldest first.
//!
//! Pure builders ([`event_lines`], [`event_stream`]) take `&WorldState` and
//! never mutate; [`render_event_stream`] blits the widget.

use ratatui::{
    Frame,
    layout::Rect,
    text::Line,
    widgets::{Block, Paragraph},
};

use crate::state::WorldState;

/// Oldest-first tail of at most `max` summaries.
pub fn event_lines(world: &WorldState, max: usize) -> Vec<Line<'static>> {
    if max == 0 {
        return Vec::new();
    }
    if world.events.is_empty() {
        return vec![Line::from("(no events)")];
    }
    let skip = world.events.len().saturating_sub(max);
    world
        .events
        .iter()
        .skip(skip)
        .map(|event| Line::from(format!("{} {}", event.id, event.summary)))
        .collect()
}

/// Pure widget builder.
pub fn event_stream(world: &WorldState, max: usize) -> Paragraph<'static> {
    Paragraph::new(event_lines(world, max)).block(Block::bordered().title("Events"))
}

/// Frame helper; fits the tail to the area height so short screens clip
/// instead of panic. No-op on empty areas.
pub fn render_event_stream(frame: &mut Frame, area: Rect, world: &WorldState) {
    if area.is_empty() {
        return;
    }
    let max = area.height.saturating_sub(2) as usize;
    frame.render_widget(event_stream(world, max), area);
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::seed_world;

    #[test]
    fn tail_caps_at_max_oldest_first() {
        let world = seed_world();
        let max = 1;
        let lines = event_lines(&world, max);
        assert_eq!(lines.len(), 1);
        let last = world.events.last().unwrap();
        assert_eq!(
            lines[0],
            Line::from(format!("{} {}", last.id, last.summary))
        );
    }

    #[test]
    fn empty_world_reports_no_events() {
        let world = WorldState::new();
        assert_eq!(event_lines(&world, 10), vec![Line::from("(no events)")]);
        assert!(event_lines(&world, 0).is_empty());
    }
}
