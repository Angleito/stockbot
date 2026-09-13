//! Lifecycle states shared by agents and workers.
//!
//! The renderer only matches on these variants; it never inspects what a
//! worker is actually doing.

/// Where an agent or worker stands in its lifecycle.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum AgentStatus {
    Idle,
    Working,
    Waiting,
    Blocked,
    Done,
    Failed,
}

impl AgentStatus {
    /// Terminal states never transition out.
    pub fn is_terminal(self) -> bool {
        matches!(self, Self::Done | Self::Failed)
    }
}

/// Bounded progress bar state: `done` units finished out of `total`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ProgressState {
    pub done: u64,
    pub total: u64,
}

impl ProgressState {
    pub fn new(done: u64, total: u64) -> Self {
        Self { done, total }
    }

    /// Whole-percent completion; 0 when `total` is 0. Caps at 100.
    pub fn percent(self) -> u8 {
        if self.total == 0 {
            return 0;
        }
        self.done.saturating_mul(100).saturating_div(self.total).min(100) as u8
    }
}

impl Default for ProgressState {
    fn default() -> Self {
        Self { done: 0, total: 0 }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn terminal_states() {
        assert!(AgentStatus::Done.is_terminal());
        assert!(AgentStatus::Failed.is_terminal());
        assert!(!AgentStatus::Working.is_terminal());
        assert!(!AgentStatus::Idle.is_terminal());
    }

    #[test]
    fn percent_math() {
        assert_eq!(ProgressState::new(1, 2).percent(), 50);
        assert_eq!(ProgressState::new(0, 0).percent(), 0);
        assert_eq!(ProgressState::new(9, 2).percent(), 100);
    }
}
