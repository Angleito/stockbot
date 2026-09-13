//! Status glyph + color. Generic: matches only on [`AgentStatus`](crate::status::AgentStatus).

use ratatui::style::{Color, Style};

use crate::status::AgentStatus;

/// Text glyph for a lifecycle state; shape alone carries the meaning.
pub fn dot(status: AgentStatus) -> &'static str {
    match status {
        AgentStatus::Idle => "○",
        AgentStatus::Working => "●",
        AgentStatus::Waiting => "◐",
        AgentStatus::Blocked => "■",
        AgentStatus::Done => "✔",
        AgentStatus::Failed => "✖",
    }
}

/// Glyph color for a lifecycle state.
pub fn color(status: AgentStatus) -> Color {
    match status {
        AgentStatus::Idle => Color::Gray,
        AgentStatus::Working => Color::Green,
        AgentStatus::Waiting => Color::Yellow,
        AgentStatus::Blocked => Color::Red,
        AgentStatus::Done => Color::Blue,
        AgentStatus::Failed => Color::LightRed,
    }
}

/// Combined glyph style.
pub fn style(status: AgentStatus) -> Style {
    Style::default().fg(color(status))
}
