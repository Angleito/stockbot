//! Reducer input. Every [`WorldState`](crate::state::WorldState) mutation
//! flows through [`WorldState::apply`]; widgets never touch state directly.

use crate::{
    ids::{AgentId, ArtifactId, WorkerId},
    state::{Artifact, Evidence, Selected},
    status::AgentStatus,
    view::View,
};

/// One state transition. The app shell alone may apply `SelectionChanged` /
/// `ViewChanged` (translating widget intent into navigation); all other
/// variants arrive from agent/worker updates.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum AgentEvent {
    AgentAdded {
        id: AgentId,
        name: String,
    },
    AgentStatus {
        id: AgentId,
        status: AgentStatus,
    },
    WorkerAdded {
        id: WorkerId,
        agent_id: AgentId,
        name: String,
    },
    WorkerStatus {
        id: WorkerId,
        status: AgentStatus,
    },
    WorkerProgress {
        id: WorkerId,
        done: u64,
        total: u64,
    },
    WorkerDetails {
        id: WorkerId,
        started_at: String,
        runtime_secs: u64,
        model: String,
        objective: String,
    },
    WorkerLog {
        id: WorkerId,
        line: String,
    },
    WorkerStageChanged {
        worker_id: WorkerId,
        stage: String,
    },
    FindingAdded {
        worker_id: WorkerId,
        finding: String,
    },
    ArtifactAdded {
        worker_id: WorkerId,
        artifact: Artifact,
    },
    EvidenceAdded {
        worker_id: WorkerId,
        evidence: Evidence,
    },
    AuditAdded {
        summary: String,
    },
    SelectionChanged(Option<Selected>),
    ViewChanged(View),
}

/// Convenience: build an artifact without importing its id type first.
pub fn artifact(id: impl Into<ArtifactId>, label: &str, body: &str) -> Artifact {
    Artifact {
        id: id.into(),
        label: label.to_owned(),
        body: body.to_owned(),
    }
}
