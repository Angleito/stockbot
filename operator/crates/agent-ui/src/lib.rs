//! Generic agent supervision model: ids, statuses, world snapshot, events,
//! navigation targets, and fake-domain fixtures.
//!
//! Widgets must never mutate [`WorldState`](crate::state::WorldState)
//! directly; every change flows through
//! [`WorldState::apply`](crate::state::WorldState::apply).

pub mod events;
pub mod fixtures;
pub mod ids;
pub mod state;
pub mod status;
pub mod view;
pub mod views;

pub use events::AgentEvent;
pub use ids::{AgentId, ArtifactId, EventId, WorkerId};
pub use state::{AgentState, Artifact, AuditEvent, Evidence, Selected, WorkerState, WorldState};
pub use status::{AgentStatus, ProgressState};
pub use view::View;
