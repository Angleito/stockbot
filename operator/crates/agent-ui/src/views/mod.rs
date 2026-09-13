//! Detail views: agent tree, worker page, raw logs, world dashboard.
//!
//! Owned by AgentWorkerViews (agent/worker/logs); WorldDashboard owns
//! world/agent_card/event_stream/status. All exports live here per Main's
//! ownership fix.
pub mod agent;
pub mod agent_card;
pub mod event_stream;
pub mod logs;
pub mod status;
pub mod worker;
pub mod world;
