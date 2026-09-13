//! Navigation targets. Only the app shell sets these; widgets emit intent.

use crate::ids::{AgentId, WorkerId};

/// Screen the app shell shows. Widgets request one via intent, never set it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum View {
    World,
    Agent(AgentId),
    Worker(WorkerId),
    Logs(WorkerId),
}

impl Default for View {
    fn default() -> Self {
        Self::World
    }
}
