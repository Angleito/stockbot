//! Live event flow: Stockbot runtime -> [`adapt`](crate::adapter::adapt) ->
//! [`WorldState::apply`]. Ids and payloads travel as typed [`StockbotEvent`]
//! and land through the sole mutator; no LLM-text parsing anywhere.
//!
//! RAW LOGS ground truth: live Herdr PTY text when the worker's pane exists,
//! fixture [`WorkerState::logs`](agent_ui::WorkerState::logs) otherwise. Pane
//! text is split into lines verbatim, never interpreted into stages/findings,
//! and the socket stays in this trusted operator crate (never in renderers).

use agent_ui::{WorldState, ids::WorkerId};

use crate::adapter::{self, StockbotEvent, fake_stockbot_sequence};
use crate::app::App;

/// Scrollback rows requested per pane read. Used by pane_text; staged until render shows RAW LOGS.
#[allow(dead_code)]
const RAW_LOG_LINES: u32 = 200;

/// Fold one typed runtime event into the world. Sole path for live updates. Exercised in tests; LiveFeed uses adapt directly per tick.
#[allow(dead_code)]
pub fn apply_stockbot(world: &mut WorldState, event: StockbotEvent) {
    world.apply(adapter::adapt(event));
}

/// Fold a whole runtime sequence; returns events consumed. Test + future batch hook.
#[allow(dead_code)]
pub fn apply_all(
    world: &mut WorldState,
    events: impl IntoIterator<Item = StockbotEvent>,
) -> usize {
    let mut n = 0;
    for event in events {
        apply_stockbot(world, event);
        n += 1;
    }
    n
}

/// Fresh world driven from the fake-live sequence via `apply` only. Test helper.
#[allow(dead_code)]
pub fn fake_live_world() -> WorldState {
    let mut world = WorldState::new();
    apply_all(&mut world, fake_stockbot_sequence());
    world
}

/// Incremental live feed: one typed event per tick so the UI fills without
/// manual refresh. Each [`LiveFeed::pump_one`] is a single adapt->apply step.
pub struct LiveFeed {
    queue: std::vec::IntoIter<StockbotEvent>,
}

impl LiveFeed {
    pub fn from_fake() -> Self {
        Self {
            queue: fake_stockbot_sequence().into_iter(),
        }
    }

    /// Apply the next event to the app; `false` once the feed is exhausted.
    /// Applied events are visible on the next draw, no refresh step needed.
    pub fn pump_one(&mut self, app: &mut App) -> bool {
        match self.queue.next() {
            Some(event) => {
                app.apply(adapter::adapt(event));
                true
            }
            None => false,
        }
    }
}

/// RAW LOGS ground truth for one worker: live Herdr PTY text when its pane
/// exists, fixture logs when the pane is absent. Raw lines only. Wired when render shows Logs view.
#[allow(dead_code)]
pub fn raw_logs(world: &WorldState, id: &WorkerId) -> Vec<String> {
    raw_logs_with_pane(world, id, pane_text(id))
}

/// [`raw_logs`] with the pane read injected (deterministic under test).
#[allow(dead_code)]
pub fn raw_logs_with_pane(
    world: &WorldState,
    id: &WorkerId,
    pane_text: Option<String>,
) -> Vec<String> {
    match pane_text {
        // ponytail: verbatim split; never parse stages/findings from PTY text.
        Some(text) => text.lines().map(str::to_owned).collect(),
        None => fixture_logs(world, id),
    }
}

#[allow(dead_code)]
fn fixture_logs(world: &WorldState, id: &WorkerId) -> Vec<String> {
    world
        .workers
        .get(id)
        .map(|w| w.logs.clone())
        .unwrap_or_default()
}

/// Live pane text via the trusted-operator socket; `None` when Herdr is
/// absent (`NotConnected`) or the read fails. Never panics. Wired when render shows Logs view.
#[allow(dead_code)]
fn pane_text(id: &WorkerId) -> Option<String> {
    herdr_client::Client::new()
        .pane_read(id.as_str(), RAW_LOG_LINES)
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use agent_ui::{AgentId, AgentStatus, fixtures::seed_world};

    #[test]
    fn live_sequence_drives_world_agent_worker_without_refresh() {
        // Pipeline under test: StockbotEvent -> adapt -> apply only.
        let mut world = WorldState::new();
        apply_all(&mut world, fake_stockbot_sequence());

        // AgentSpawned lands the SEC agent with its staged status.
        let agent = &world.agents[&AgentId::new("agent-sec")];
        assert_eq!(agent.name, "SEC");
        assert_eq!(agent.status, AgentStatus::Working);
        assert_eq!(agent.workers.len(), 3);

        // WorkerSpawned nests all three collectors under the agent.
        for id in ["worker-8k", "worker-form4", "worker-10q"] {
            let worker = &world.workers[&WorkerId::new(id)];
            assert_eq!(worker.agent_id, AgentId::new("agent-sec"), "{id}");
            assert!(agent.workers.contains(&worker.id), "{id}");
        }

        // WorkerStageChanged lands the stage; tool/stage text never leaks
        // into findings or logs.
        let eight_k = &world.workers[&WorkerId::new("worker-8k")];
        assert_eq!(eight_k.current_stage, "fetching");
        assert_eq!(eight_k.findings, vec!["demo finding: batch open"]);
        assert_eq!(eight_k.logs, vec!["opened batch"]);

        // ArtifactCreated / EvidenceLinked land on their workers.
        let ten_q = &world.workers[&WorkerId::new("worker-10q")];
        assert_eq!(ten_q.artifacts.len(), 1);
        assert_eq!(ten_q.artifacts[0].label, "summary");
        assert_eq!(eight_k.evidence.len(), 1);

        // WorkerCompleted marks the worker done.
        assert_eq!(ten_q.status, AgentStatus::Done);
        assert_eq!(
            world.workers[&WorkerId::new("worker-form4")].status,
            AgentStatus::Waiting
        );
        assert_eq!(world.events.last().map(|e| &e.summary).unwrap().as_str(), "demo stream ready");

        // Same SEC slice as the seeded world, via live events only.
        let seeded = seed_world();
        assert_eq!(
            world.agents[&AgentId::new("agent-sec")],
            seeded.agents[&AgentId::new("agent-sec")]
        );
        for id in ["worker-8k", "worker-form4", "worker-10q"] {
            let id = WorkerId::new(id);
            assert_eq!(world.workers[&id], seeded.workers[&id], "{id} differs");
        }
        assert_eq!(world.events, seeded.events);
    }

    #[test]
    fn feed_pump_is_visible_without_refresh() {
        let mut app = App::new();
        // New shell starts in Chat mode with no world; pump into an empty
        // research world so the feed builds it, as before.
        app.current_mut().research = Some(crate::workspace::ResearchState {
            world: WorldState::new(),
            tick: 0,
            title: "TEST RESEARCH".to_owned(),
            query: "test".to_owned(),
            cursor: 0,
        });
        let mut feed = LiveFeed::from_fake();
        // First event (AgentSpawned) is visible immediately after one pump.
        assert!(feed.pump_one(&mut app));
        assert!(app
            .current()
            .research
            .as_ref()
            .unwrap()
            .world
            .agents
            .contains_key(&AgentId::new("agent-sec")));
        let mut pumped = 1;
        while feed.pump_one(&mut app) {
            pumped += 1;
        }
        assert_eq!(pumped, fake_stockbot_sequence().len());
        assert_eq!(app.current().research.as_ref().unwrap().world.workers.len(), 3);
        assert!(!feed.pump_one(&mut app));
    }

    #[test]
    fn raw_logs_fall_back_to_fixture_when_pane_absent() {
        let world = fake_live_world();
        let id = WorkerId::new("worker-8k");
        // No Herdr daemon here: pane absent -> fixture logs.
        assert_eq!(raw_logs(&world, &id), vec!["opened batch"]);
        // Injected pane text wins verbatim; absent pane falls back.
        assert_eq!(
            raw_logs_with_pane(&world, &id, Some("a\nb".to_owned())),
            vec!["a", "b"]
        );
        assert_eq!(
            raw_logs_with_pane(&world, &id, None),
            vec!["opened batch"]
        );
        // Unknown workers yield no lines, never a panic.
        assert!(raw_logs_with_pane(&world, &WorkerId::new("nope"), None).is_empty());
        assert!(raw_logs_with_pane(&world, &WorkerId::new("nope"), Some(String::new())).is_empty());
    }
}
