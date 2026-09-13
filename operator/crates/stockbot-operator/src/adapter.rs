//! Fake Stockbot runtime -> generic [`AgentEvent`](agent_ui::AgentEvent) adapter.
//!
//! Finance concepts (SEC agent, 8-K / Form 4 / 10-Q workers, filings, URLs,
//! accession numbers, thesis links) live ONLY here. Renderers never import
//! this module; the pipeline is always
//! `StockbotEvent -> adapt -> AgentEvent -> WorldState::apply`.

use agent_ui::{
    AgentEvent, AgentId, AgentStatus, WorkerId,
    events::artifact,
    state::Evidence,
};

/// Stockbot-side agent kinds. Only SEC exists on this side.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SecAgentKind {
    Sec,
}

impl SecAgentKind {
    pub fn agent_id(self) -> AgentId {
        AgentId::new("agent-sec")
    }

    pub fn name(self) -> &'static str {
        "SEC"
    }
}

/// Stockbot-side worker kinds: one collector per filing family.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkerKind {
    EightK,
    Form4,
    TenQ,
}

impl WorkerKind {
    pub fn worker_id(self) -> WorkerId {
        match self {
            Self::EightK => WorkerId::new("worker-8k"),
            Self::Form4 => WorkerId::new("worker-form4"),
            Self::TenQ => WorkerId::new("worker-10q"),
        }
    }

    pub fn agent_id(self) -> AgentId {
        AgentId::new("agent-sec")
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::EightK => "8K",
            Self::Form4 => "Form4",
            Self::TenQ => "10Q",
        }
    }
}

/// Fake collector/tool/runtime events on the Stockbot side.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum StockbotEvent {
    /// Collector spawned an agent.
    AgentSpawned { agent: SecAgentKind },
    /// Collector spawned a filing worker.
    WorkerSpawned { worker: WorkerKind },
    /// Collector staged an agent lifecycle step.
    AgentStaged {
        agent: SecAgentKind,
        status: AgentStatus,
    },
    /// Collector staged a worker lifecycle step.
    WorkerStaged {
        worker: WorkerKind,
        status: AgentStatus,
    },
    /// Collector progress tick.
    WorkerProgressed {
        worker: WorkerKind,
        done: u64,
        total: u64,
    },
    /// Tool emitted one log line.
    ToolObserved { worker: WorkerKind, line: String },
    /// Tool finished: runtime identity for the worker card.
    ToolFinished {
        worker: WorkerKind,
        model: String,
        objective: String,
        started_at: String,
        runtime_secs: u64,
    },
    /// Tool started: deterministic runtime text, never a finding.
    ToolStarted { worker: WorkerKind, tool: String },
    /// Collector entered a new stage: deterministic runtime, never a finding.
    StageStarted { worker: WorkerKind, stage: String },
    /// Thesis link attached to a worker.
    ThesisLinked { worker: WorkerKind, thesis: String },
    /// Filing parsed into a finished artifact.
    ArtifactCreated {
        worker: WorkerKind,
        artifact_id: String,
        label: String,
        body: String,
    },
    /// Filing URL / accession number linked as supporting evidence.
    EvidenceLinked {
        worker: WorkerKind,
        label: String,
        detail: String,
    },
    /// Collector marked a worker done.
    WorkerCompleted { worker: WorkerKind },
    /// Collector finished with an audit note.
    CollectorDone { summary: String },
}

/// Map one Stockbot event to one generic event, preserving ids 1:1.
pub fn adapt(event: StockbotEvent) -> AgentEvent {
    match event {
        StockbotEvent::AgentSpawned { agent } => AgentEvent::AgentAdded {
            id: agent.agent_id(),
            name: agent.name().to_owned(),
        },
        StockbotEvent::AgentStaged { agent, status } => AgentEvent::AgentStatus {
            id: agent.agent_id(),
            status,
        },
        StockbotEvent::WorkerSpawned { worker } => AgentEvent::WorkerAdded {
            id: worker.worker_id(),
            agent_id: worker.agent_id(),
            name: worker.name().to_owned(),
        },
        StockbotEvent::ToolStarted { worker, tool } => AgentEvent::WorkerStageChanged {
            worker_id: worker.worker_id(),
            stage: tool,
        },
        StockbotEvent::StageStarted { worker, stage } => AgentEvent::WorkerStageChanged {
            worker_id: worker.worker_id(),
            stage,
        },
        StockbotEvent::WorkerStaged { worker, status } => AgentEvent::WorkerStatus {
            id: worker.worker_id(),
            status,
        },
        StockbotEvent::WorkerProgressed { worker, done, total } => {
            AgentEvent::WorkerProgress {
                id: worker.worker_id(),
                done,
                total,
            }
        }
        StockbotEvent::ToolObserved { worker, line } => AgentEvent::WorkerLog {
            id: worker.worker_id(),
            line,
        },
        StockbotEvent::ToolFinished {
            worker,
            model,
            objective,
            started_at,
            runtime_secs,
        } => AgentEvent::WorkerDetails {
            id: worker.worker_id(),
            started_at,
            runtime_secs,
            model,
            objective,
        },
        StockbotEvent::ThesisLinked { worker, thesis } => AgentEvent::FindingAdded {
            worker_id: worker.worker_id(),
            finding: thesis,
        },
        StockbotEvent::ArtifactCreated {
            worker,
            artifact_id,
            label,
            body,
        } => AgentEvent::ArtifactAdded {
            worker_id: worker.worker_id(),
            artifact: artifact(artifact_id, &label, &body),
        },
        StockbotEvent::EvidenceLinked {
            worker,
            label,
            detail,
        } => AgentEvent::EvidenceAdded {
            worker_id: worker.worker_id(),
            evidence: Evidence { label, detail },
        },
        StockbotEvent::WorkerCompleted { worker } => AgentEvent::WorkerStatus {
            id: worker.worker_id(),
            status: AgentStatus::Done,
        },
        StockbotEvent::CollectorDone { summary } => AgentEvent::AuditAdded { summary },
    }
}

/// Fake collector run mirroring the SEC slice of
/// [`fixtures::fake_sequence`](agent_ui::fixtures::fake_sequence),
/// with identical ids/payloads so the adapted world matches it.
pub fn fake_stockbot_sequence() -> Vec<StockbotEvent> {
    use StockbotEvent::*;
    vec![
        AgentSpawned {
            agent: SecAgentKind::Sec,
        },
        WorkerSpawned {
            worker: WorkerKind::EightK,
        },
        WorkerSpawned {
            worker: WorkerKind::Form4,
        },
        WorkerSpawned {
            worker: WorkerKind::TenQ,
        },
        AgentStaged {
            agent: SecAgentKind::Sec,
            status: AgentStatus::Working,
        },
        WorkerStaged {
            worker: WorkerKind::EightK,
            status: AgentStatus::Working,
        },
        ToolStarted {
            worker: WorkerKind::EightK,
            tool: "search_sec_filings".to_owned(),
        },
        StageStarted {
            worker: WorkerKind::EightK,
            stage: "fetching".to_owned(),
        },
        ToolObserved {
            worker: WorkerKind::EightK,
            line: "opened batch".to_owned(),
        },
        WorkerProgressed {
            worker: WorkerKind::EightK,
            done: 1,
            total: 4,
        },
        ToolFinished {
            worker: WorkerKind::EightK,
            model: "demo model".to_owned(),
            objective: "demo objective".to_owned(),
            started_at: "demo start".to_owned(),
            runtime_secs: 95,
        },
        ThesisLinked {
            worker: WorkerKind::EightK,
            thesis: "demo finding: batch open".to_owned(),
        },
        WorkerStaged {
            worker: WorkerKind::Form4,
            status: AgentStatus::Waiting,
        },
        WorkerCompleted {
            worker: WorkerKind::TenQ,
        },
        WorkerProgressed {
            worker: WorkerKind::TenQ,
            done: 4,
            total: 4,
        },
        ToolFinished {
            worker: WorkerKind::TenQ,
            model: "demo model".to_owned(),
            objective: "demo objective".to_owned(),
            started_at: "demo start".to_owned(),
            runtime_secs: 210,
        },
        ThesisLinked {
            worker: WorkerKind::TenQ,
            thesis: "demo finding: batch done".to_owned(),
        },
        ArtifactCreated {
            worker: WorkerKind::TenQ,
            artifact_id: "artifact-10q-1".to_owned(),
            label: "summary".to_owned(),
            body: "done".to_owned(),
        },
        EvidenceLinked {
            worker: WorkerKind::EightK,
            label: "note".to_owned(),
            detail: "see batch".to_owned(),
        },
        CollectorDone {
            summary: "demo stream ready".to_owned(),
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use agent_ui::{WorldState, fixtures::seed_world};

    #[test]
    fn adapt_preserves_ids_and_maps_details_findings_artifacts_evidence() {
        // WorkerDetails carries model/objective/started/runtime.
        let details = adapt(StockbotEvent::ToolFinished {
            worker: WorkerKind::EightK,
            model: "m".to_owned(),
            objective: "o".to_owned(),
            started_at: "s".to_owned(),
            runtime_secs: 7,
        });
        assert_eq!(
            details,
            AgentEvent::WorkerDetails {
                id: WorkerId::new("worker-8k"),
                started_at: "s".to_owned(),
                runtime_secs: 7,
                model: "m".to_owned(),
                objective: "o".to_owned(),
            }
        );

        // Thesis link -> generic finding.
        let finding = adapt(StockbotEvent::ThesisLinked {
            worker: WorkerKind::TenQ,
            thesis: "thesis/42 supports 10-Q trend".to_owned(),
        });
        assert_eq!(
            finding,
            AgentEvent::FindingAdded {
                worker_id: WorkerId::new("worker-10q"),
                finding: "thesis/42 supports 10-Q trend".to_owned(),
            }
        );

        // Filing -> artifact, URL/accession -> evidence.
        let mut world = WorldState::new();
        world.apply(adapt(StockbotEvent::AgentSpawned {
            agent: SecAgentKind::Sec,
        }));
        world.apply(adapt(StockbotEvent::WorkerSpawned {
            worker: WorkerKind::TenQ,
        }));
        world.apply(adapt(StockbotEvent::ArtifactCreated {
            worker: WorkerKind::TenQ,
            artifact_id: "artifact-10q-9".to_owned(),
            label: "10-Q summary".to_owned(),
            body: "revenue up".to_owned(),
        }));
        world.apply(adapt(StockbotEvent::EvidenceLinked {
            worker: WorkerKind::TenQ,
            label: "10-Q filing".to_owned(),
            detail: "accession 000123-26-000007 https://sec.gov/ixviewer/x".to_owned(),
        }));
        let worker = &world.workers[&WorkerId::new("worker-10q")];
        assert_eq!(worker.artifacts.len(), 1);
        assert_eq!(worker.artifacts[0].label, "10-Q summary");
        assert_eq!(worker.evidence.len(), 1);
        assert!(worker.evidence[0].detail.contains("000123-26-000007"));
    }

    #[test]
    fn stage_events_never_land_in_findings() {
        let mut world = WorldState::new();
        world.apply(adapt(StockbotEvent::AgentSpawned {
            agent: SecAgentKind::Sec,
        }));
        world.apply(adapt(StockbotEvent::WorkerSpawned {
            worker: WorkerKind::EightK,
        }));
        world.apply(adapt(StockbotEvent::ToolStarted {
            worker: WorkerKind::EightK,
            tool: "search_sec_filings".to_owned(),
        }));
        world.apply(adapt(StockbotEvent::StageStarted {
            worker: WorkerKind::EightK,
            stage: "collect".to_owned(),
        }));
        let worker = &world.workers[&WorkerId::new("worker-8k")];
        assert!(worker.findings.is_empty());
        assert!(worker.logs.is_empty());
        assert_eq!(worker.current_stage, "collect");
    }

    #[test]
    fn stockbot_sequence_matches_seed_world_via_apply_only() {
        // Pipeline under test: StockbotEvent -> adapt -> AgentEvent -> apply.
        let mut via_adapter = WorldState::new();
        for event in fake_stockbot_sequence() {
            via_adapter.apply(adapt(event));
        }
        let seeded = seed_world();

        // Same Agent/Worker hierarchy (NEWS/MARKET placeholders stay in
        // fixtures only; the adapter owns just the SEC slice).
        assert_eq!(
            via_adapter.agents[&AgentId::new("agent-sec")],
            seeded.agents[&AgentId::new("agent-sec")]
        );
        for id in ["worker-8k", "worker-form4", "worker-10q"] {
            let id = WorkerId::new(id);
            assert_eq!(via_adapter.workers[&id], seeded.workers[&id], "{id} differs");
        }
        assert_eq!(via_adapter.events, seeded.events);
    }
}
