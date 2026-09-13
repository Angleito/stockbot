//! Fake-domain fixtures.
//!
//! The ONLY file in `src` allowed to name concrete domains (SEC, NEWS,
//! MARKET, 8K, Form4, 10Q). Everything here is built through
//! [`WorldState::apply`], proving the generic renderer needs no domain
//! knowledge: it matches on ids/statuses, never on these names.

use crate::{
    events::{artifact, AgentEvent},
    ids::{AgentId, WorkerId},
    state::{Evidence, Selected, WorldState},
    status::AgentStatus,
    view::View,
};

/// Full demo stream: three fake agents, their workers, and sample updates.
pub fn fake_sequence() -> Vec<AgentEvent> {
    vec![
        AgentEvent::AgentAdded {
            id: AgentId::new("agent-sec"),
            name: "SEC".to_owned(),
        },
        AgentEvent::AgentAdded {
            id: AgentId::new("agent-news"),
            name: "NEWS".to_owned(),
        },
        AgentEvent::AgentAdded {
            id: AgentId::new("agent-market"),
            name: "MARKET".to_owned(),
        },
        AgentEvent::WorkerAdded {
            id: WorkerId::new("worker-8k"),
            agent_id: AgentId::new("agent-sec"),
            name: "8K".to_owned(),
        },
        AgentEvent::WorkerAdded {
            id: WorkerId::new("worker-form4"),
            agent_id: AgentId::new("agent-sec"),
            name: "Form4".to_owned(),
        },
        AgentEvent::WorkerAdded {
            id: WorkerId::new("worker-10q"),
            agent_id: AgentId::new("agent-sec"),
            name: "10Q".to_owned(),
        },
        AgentEvent::WorkerAdded {
            id: WorkerId::new("worker-wire"),
            agent_id: AgentId::new("agent-news"),
            name: "Wire".to_owned(),
        },
        AgentEvent::WorkerAdded {
            id: WorkerId::new("worker-recap"),
            agent_id: AgentId::new("agent-news"),
            name: "Recap".to_owned(),
        },
        AgentEvent::WorkerAdded {
            id: WorkerId::new("worker-board"),
            agent_id: AgentId::new("agent-market"),
            name: "Board".to_owned(),
        },
        AgentEvent::AgentStatus {
            id: AgentId::new("agent-sec"),
            status: AgentStatus::Working,
        },
        AgentEvent::WorkerStatus {
            id: WorkerId::new("worker-8k"),
            status: AgentStatus::Working,
        },
        AgentEvent::WorkerProgress {
            id: WorkerId::new("worker-8k"),
            done: 1,
            total: 4,
        },
        AgentEvent::WorkerLog {
            id: WorkerId::new("worker-8k"),
            line: "opened batch".to_owned(),
        },
        AgentEvent::WorkerDetails {
            id: WorkerId::new("worker-8k"),
            started_at: "demo start".to_owned(),
            runtime_secs: 95,
            model: "demo model".to_owned(),
            objective: "demo objective".to_owned(),
        },
        AgentEvent::WorkerStageChanged {
            worker_id: WorkerId::new("worker-8k"),
            stage: "fetching".to_owned(),
        },
        AgentEvent::FindingAdded {
            worker_id: WorkerId::new("worker-8k"),
            finding: "demo finding: batch open".to_owned(),
        },
        AgentEvent::WorkerStatus {
            id: WorkerId::new("worker-form4"),
            status: AgentStatus::Waiting,
        },
        AgentEvent::WorkerStatus {
            id: WorkerId::new("worker-10q"),
            status: AgentStatus::Done,
        },
        AgentEvent::WorkerProgress {
            id: WorkerId::new("worker-10q"),
            done: 4,
            total: 4,
        },
        AgentEvent::WorkerDetails {
            id: WorkerId::new("worker-10q"),
            started_at: "demo start".to_owned(),
            runtime_secs: 210,
            model: "demo model".to_owned(),
            objective: "demo objective".to_owned(),
        },
        AgentEvent::FindingAdded {
            worker_id: WorkerId::new("worker-10q"),
            finding: "demo finding: batch done".to_owned(),
        },
        AgentEvent::ArtifactAdded {
            worker_id: WorkerId::new("worker-10q"),
            artifact: artifact("artifact-10q-1", "summary", "done"),
        },
        AgentEvent::EvidenceAdded {
            worker_id: WorkerId::new("worker-8k"),
            evidence: Evidence {
                label: "note".to_owned(),
                detail: "see batch".to_owned(),
            },
        },
        AgentEvent::AuditAdded {
            summary: "demo stream ready".to_owned(),
        },
        AgentEvent::SelectionChanged(Some(Selected::Agent(AgentId::new("agent-sec")))),
        AgentEvent::ViewChanged(View::Agent(AgentId::new("agent-sec"))),
    ]
}

/// Fold [`fake_sequence`] into a fresh world using `apply` only.
pub fn seed_world() -> WorldState {
    let mut world = WorldState::new();
    for event in fake_sequence() {
        world.apply(event);
    }
    world
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fake_sequence_builds_sec_agent_with_three_workers() {
        let world = seed_world();

        let agent = world
            .agents
            .values()
            .find(|a| a.name == "SEC")
            .expect("SEC agent present");
        let mut names: Vec<&str> = agent
            .workers
            .iter()
            .map(|id| world.workers[id].name.as_str())
            .collect();
        names.sort_unstable();
        assert_eq!(names, ["10Q", "8K", "Form4"]);
        assert_eq!(agent.status, AgentStatus::Working);

        // Remainder of the fake world came along too.
        assert!(world.agents.values().any(|a| a.name == "NEWS"));
        assert!(world.agents.values().any(|a| a.name == "MARKET"));
        assert_eq!(world.workers.len(), 6);
        assert_eq!(world.events.len(), 1);
        assert_eq!(
            world.selected,
            Some(Selected::Agent(AgentId::new("agent-sec")))
        );
        assert_eq!(
            world.current_view,
            View::Agent(AgentId::new("agent-sec"))
        );
    }
}
