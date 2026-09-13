//! World snapshot plus the sole mutation entry point, [`WorldState::apply`].

use std::collections::HashMap;

use crate::{
    events::AgentEvent,
    ids::{AgentId, ArtifactId, EventId, WorkerId},
    status::{AgentStatus, ProgressState},
    view::View,
};

/// One supervised unit of work.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AgentState {
    pub id: AgentId,
    pub name: String,
    pub status: AgentStatus,
    pub workers: Vec<WorkerId>,
}

/// One task running under an agent.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerState {
    pub id: WorkerId,
    pub agent_id: AgentId,
    pub name: String,
    pub status: AgentStatus,
    pub progress: ProgressState,
    pub started_at: String,
    pub runtime_secs: u64,
    pub model: String,
    pub objective: String,
    pub current_stage: String,
    pub findings: Vec<String>,
    pub logs: Vec<String>,
    pub artifacts: Vec<Artifact>,
    pub evidence: Vec<Evidence>,
}

/// A finished output produced by a worker.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Artifact {
    pub id: ArtifactId,
    pub label: String,
    pub body: String,
}

/// A supporting note attached to a worker.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Evidence {
    pub label: String,
    pub detail: String,
}

/// One append-only audit log entry.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct AuditEvent {
    pub id: EventId,
    pub summary: String,
}

/// Current list highlight.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Selected {
    Agent(AgentId),
    Worker(WorkerId),
}

/// Whole UI snapshot. Mutate only via [`WorldState::apply`].
#[derive(Clone, Debug, Default)]
pub struct WorldState {
    pub agents: HashMap<AgentId, AgentState>,
    pub workers: HashMap<WorkerId, WorkerState>,
    pub events: Vec<AuditEvent>,
    pub selected: Option<Selected>,
    pub current_view: View,
}

impl WorldState {
    pub fn new() -> Self {
        Self::default()
    }

    fn audit(&mut self, summary: String) {
        let id = EventId::new(format!("e{}", self.events.len()));
        self.events.push(AuditEvent { id, summary });
    }

    /// Fold one event into the snapshot. Unknown ids are ignored.
    pub fn apply(&mut self, event: AgentEvent) {
        match event {
            AgentEvent::AgentAdded { id, name } => {
                self.agents.entry(id.clone()).or_insert(AgentState {
                    id,
                    name,
                    status: AgentStatus::Idle,
                    workers: Vec::new(),
                });
            }
            AgentEvent::AgentStatus { id, status } => {
                if let Some(agent) = self.agents.get_mut(&id) {
                    agent.status = status;
                }
            }
            AgentEvent::WorkerAdded { id, agent_id, name } => {
                if self.agents.contains_key(&agent_id) && !self.workers.contains_key(&id) {
                    if let Some(agent) = self.agents.get_mut(&agent_id) {
                        agent.workers.push(id.clone());
                    }
                    self.workers.insert(
                        id.clone(),
                        WorkerState {
                            id,
                            agent_id,
                            name,
                            status: AgentStatus::Idle,
                            progress: ProgressState::default(),
                            started_at: String::new(),
                            runtime_secs: 0,
                            model: String::new(),
                            objective: String::new(),
                            current_stage: String::new(),
                            findings: Vec::new(),
                            logs: Vec::new(),
                            artifacts: Vec::new(),
                            evidence: Vec::new(),
                        },
                    );
                }
            }
            AgentEvent::WorkerStatus { id, status } => {
                if let Some(worker) = self.workers.get_mut(&id) {
                    worker.status = status;
                }
            }
            AgentEvent::WorkerProgress { id, done, total } => {
                if let Some(worker) = self.workers.get_mut(&id) {
                    worker.progress = ProgressState::new(done, total);
                }
            }
            AgentEvent::WorkerDetails {
                id,
                started_at,
                runtime_secs,
                model,
                objective,
            } => {
                if let Some(worker) = self.workers.get_mut(&id) {
                    worker.started_at = started_at;
                    worker.runtime_secs = runtime_secs;
                    worker.model = model;
                    worker.objective = objective;
                }
            }
            AgentEvent::WorkerLog { id, line } => {
                if let Some(worker) = self.workers.get_mut(&id) {
                    worker.logs.push(line);
                }
            }
            AgentEvent::FindingAdded { worker_id, finding } => {
                if let Some(worker) = self.workers.get_mut(&worker_id) {
                    worker.findings.push(finding);
                }
            }
            AgentEvent::WorkerStageChanged { worker_id, stage } => {
                if let Some(worker) = self.workers.get_mut(&worker_id) {
                    worker.current_stage = stage;
                }
            }
            AgentEvent::ArtifactAdded { worker_id, artifact } => {
                if let Some(worker) = self.workers.get_mut(&worker_id) {
                    worker.artifacts.push(artifact);
                }
            }
            AgentEvent::EvidenceAdded { worker_id, evidence } => {
                if let Some(worker) = self.workers.get_mut(&worker_id) {
                    worker.evidence.push(evidence);
                }
            }
            AgentEvent::AuditAdded { summary } => self.audit(summary),
            AgentEvent::SelectionChanged(selected) => self.selected = selected,
            AgentEvent::ViewChanged(view) => self.current_view = view,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reducer_links_worker_to_agent() {
        let mut world = WorldState::new();
        world.apply(AgentEvent::AgentAdded {
            id: AgentId::new("a"),
            name: "alpha".to_owned(),
        });
        world.apply(AgentEvent::WorkerAdded {
            id: WorkerId::new("w"),
            agent_id: AgentId::new("a"),
            name: "first".to_owned(),
        });
        world.apply(AgentEvent::WorkerProgress {
            id: WorkerId::new("w"),
            done: 3,
            total: 4,
        });
        assert_eq!(world.agents.len(), 1);
        assert_eq!(world.agents[&AgentId::new("a")].workers.len(), 1);
        assert_eq!(world.workers[&WorkerId::new("w")].progress.percent(), 75);
    }

    #[test]
    fn unknown_ids_are_ignored() {
        let mut world = WorldState::new();
        world.apply(AgentEvent::WorkerStatus {
            id: WorkerId::new("ghost"),
            status: AgentStatus::Done,
        });
        assert!(world.workers.is_empty());
    }
}
