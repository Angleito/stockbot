//! Central navigation shell: owns [`WorldState`], translates widget intent plus
//! keyboard/mouse input into [`AgentEvent::SelectionChanged`] /
//! [`AgentEvent::ViewChanged`] via [`WorldState::apply`]. Widgets never mutate
//! state; all navigation funnels through [`App`].

use agent_ui::{
    AgentEvent, Selected, View, WorldState,
    ids::{AgentId, WorkerId},
};
use crossterm::event::KeyCode;

/// Widget-emitted navigation request. Widgets hit-test their own mouse events
/// and emit one of the `Open*` variants; keyboard arrives via [`App::on_key`],
/// which translates keys into these same intents.
/// Mouse intents land with the widgets wave (hit-testing); keyboard uses Parent/Child/Next/Prev now.
#[allow(dead_code)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    /// Click (or programmatic request) on an agent row -> `View::Agent`.
    OpenAgent(AgentId),
    /// Click on a worker row -> `View::Worker`.
    OpenWorker(WorkerId),
    /// RAW LOGS action -> `View::Logs`.
    OpenLogs(WorkerId),
    /// Esc -> parent view (`Logs` -> `Worker` -> `Agent` -> `World`).
    Parent,
    /// Enter -> child view (`World` -> `Agent` -> `Worker` -> `Logs`).
    Child,
    /// Move highlight to next item in the current list.
    Next,
    /// Move highlight to previous item in the current list.
    Prev,
}

/// Central app: [`WorldState`] plus navigation. The current view lives in
/// `world.current_view`; [`App`] is the only writer of it and of `selected`.
pub struct App {
    world: WorldState,
}

impl App {
    pub fn new(world: WorldState) -> Self {
        Self { world }
    }

    pub fn world(&self) -> &WorldState {
        &self.world
    }

    pub fn view(&self) -> &View {
        &self.world.current_view
    }

    /// Fold one event into state. Live runtime updates arrive here;
    /// navigation arrives via [`App::dispatch`]. Still funnels through
    /// [`WorldState::apply`], the sole mutator.
    pub fn apply(&mut self, event: AgentEvent) {
        self.world.apply(event);
    }

    /// Fold one widget intent into state. Unknown ids are ignored.
    pub fn dispatch(&mut self, intent: Intent) {
        match intent {
            Intent::OpenAgent(id) => self.open_agent(&id),
            Intent::OpenWorker(id) => self.open_worker(&id),
            Intent::OpenLogs(id) => self.open_logs(&id),
            Intent::Parent => self.go_parent(),
            Intent::Child => self.enter_child(),
            Intent::Next => self.step(true),
            Intent::Prev => self.step(false),
        }
    }

    /// Keyboard: Enter drills into the child, Esc goes to the parent,
    /// Up/`k` and Down/`j` move the highlight. All other keys ignored.
    pub fn on_key(&mut self, code: KeyCode) {
        match code {
            KeyCode::Esc => self.dispatch(Intent::Parent),
            KeyCode::Enter => self.dispatch(Intent::Child),
            KeyCode::Up | KeyCode::Char('k') => self.dispatch(Intent::Prev),
            KeyCode::Down | KeyCode::Char('j') => self.dispatch(Intent::Next),
            _ => {}
        }
    }

    /// Mouse: click on an agent row navigates to its `Agent` view.
    /// Unknown ids are ignored.
    pub fn clicked_agent(&mut self, id: &AgentId) {
        self.open_agent(id);
    }

    /// Mouse: click on a worker row navigates to its `Worker` view.
    /// Unknown ids are ignored.
    pub fn clicked_worker(&mut self, id: &WorkerId) {
        self.open_worker(id);
    }

    /// Mouse: RAW LOGS action navigates to the worker's `Logs` view.
    /// Unknown ids are ignored.
    pub fn clicked_raw_logs(&mut self, id: &WorkerId) {
        self.open_logs(id);
    }

    /// Enter -> child view. With no usable selection, falls back to the first
    /// child so keyboard alone can drill World -> Agent -> Worker -> Logs.
    pub fn enter_child(&mut self) {
        match (self.world.current_view.clone(), self.world.selected.clone()) {
            (View::World, Some(Selected::Agent(id)))
                if self.world.agents.contains_key(&id) =>
            {
                self.open_agent(&id);
            }
            (View::World, _) => {
                if let Some(first) = self.sorted_agents().into_iter().next() {
                    self.open_agent(&first);
                }
            }
            (View::Agent(agent_id), Some(Selected::Worker(worker_id))) => {
                let ours = self
                    .world
                    .agents
                    .get(&agent_id)
                    .is_some_and(|agent| agent.workers.contains(&worker_id));
                if ours {
                    self.open_worker(&worker_id);
                } else if let Some(first) = self.first_worker_of(&agent_id) {
                    self.open_worker(&first);
                }
            }
            (View::Agent(agent_id), _) => {
                if let Some(first) = self.first_worker_of(&agent_id) {
                    self.open_worker(&first);
                }
            }
            (View::Worker(id), _) => self.open_logs(&id),
            (View::Logs(_), _) => {}
        }
    }

    /// Esc -> parent view. Esc at `World` stays put.
    pub fn go_parent(&mut self) {
        match self.world.current_view.clone() {
            View::World => {}
            View::Agent(id) => {
                if self.world.agents.contains_key(&id) {
                    self.world
                        .apply(AgentEvent::SelectionChanged(Some(Selected::Agent(id))));
                }
                self.world.apply(AgentEvent::ViewChanged(View::World));
            }
            View::Worker(id) => match self.world.workers.get(&id) {
                Some(worker) => {
                    let agent_id = worker.agent_id.clone();
                    self.world
                        .apply(AgentEvent::SelectionChanged(Some(Selected::Worker(id))));
                    self.world
                        .apply(AgentEvent::ViewChanged(View::Agent(agent_id)));
                }
                None => self.world.apply(AgentEvent::ViewChanged(View::World)),
            },
            View::Logs(id) => {
                if self.world.workers.contains_key(&id) {
                    self.world
                        .apply(AgentEvent::SelectionChanged(Some(Selected::Worker(
                            id.clone(),
                        ))));
                    self.world
                        .apply(AgentEvent::ViewChanged(View::Worker(id)));
                } else {
                    self.world.apply(AgentEvent::ViewChanged(View::World));
                }
            }
        }
    }

    fn open_agent(&mut self, id: &AgentId) {
        if self.world.agents.contains_key(id) {
            self.world
                .apply(AgentEvent::SelectionChanged(Some(Selected::Agent(
                    id.clone(),
                ))));
            self.world
                .apply(AgentEvent::ViewChanged(View::Agent(id.clone())));
        }
    }

    fn open_worker(&mut self, id: &WorkerId) {
        if self.world.workers.contains_key(id) {
            self.world
                .apply(AgentEvent::SelectionChanged(Some(Selected::Worker(
                    id.clone(),
                ))));
            self.world
                .apply(AgentEvent::ViewChanged(View::Worker(id.clone())));
        }
    }

    fn open_logs(&mut self, id: &WorkerId) {
        if self.world.workers.contains_key(id) {
            self.world
                .apply(AgentEvent::SelectionChanged(Some(Selected::Worker(
                    id.clone(),
                ))));
            self.world
                .apply(AgentEvent::ViewChanged(View::Logs(id.clone())));
        }
    }

    /// Move the highlight within the current list, wrapping at the ends.
    /// No list here (`Worker`/`Logs` views) or an empty list: no-op.
    fn step(&mut self, forward: bool) {
        match self.world.current_view.clone() {
            View::World => {
                let ids = self.sorted_agents();
                if ids.is_empty() {
                    return;
                }
                let next = match &self.world.selected {
                    Some(Selected::Agent(cur)) => ids
                        .iter()
                        .position(|id| id == cur)
                        .map(|i| ids[(i + if forward { 1 } else { ids.len() - 1 }) % ids.len()].clone()),
                    _ => Some(if forward {
                        ids[0].clone()
                    } else {
                        ids[ids.len() - 1].clone()
                    }),
                };
                if let Some(id) = next {
                    self.world
                        .apply(AgentEvent::SelectionChanged(Some(Selected::Agent(id))));
                }
            }
            View::Agent(agent_id) => {
                let workers = match self.world.agents.get(&agent_id) {
                    Some(agent) => agent.workers.clone(),
                    None => return,
                };
                if workers.is_empty() {
                    return;
                }
                let next = match &self.world.selected {
                    Some(Selected::Worker(cur)) => workers
                        .iter()
                        .position(|id| id == cur)
                        .map(|i| {
                            workers[(i + if forward { 1 } else { workers.len() - 1 })
                                % workers.len()]
                                .clone()
                        }),
                    _ => Some(if forward {
                        workers[0].clone()
                    } else {
                        workers[workers.len() - 1].clone()
                    }),
                };
                if let Some(id) = next {
                    self.world
                        .apply(AgentEvent::SelectionChanged(Some(Selected::Worker(id))));
                }
            }
            View::Worker(_) | View::Logs(_) => {}
        }
    }

    fn sorted_agents(&self) -> Vec<AgentId> {
        let mut ids: Vec<AgentId> = self.world.agents.keys().cloned().collect();
        ids.sort();
        ids
    }

    fn first_worker_of(&self, agent_id: &AgentId) -> Option<WorkerId> {
        self.world
            .agents
            .get(agent_id)
            .and_then(|agent| agent.workers.first().cloned())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use agent_ui::fixtures::seed_world;

    fn app_at_world() -> App {
        let mut world = seed_world();
        world.apply(AgentEvent::ViewChanged(View::World));
        App::new(world)
    }

    #[test]
    fn keyboard_drills_to_logs_and_back_with_unknowns_ignored() {
        let mut app = app_at_world();
        // Seed selects agent-sec, the first agent alphabetically too.
        assert_eq!(*app.view(), View::World);

        app.on_key(KeyCode::Enter);
        assert_eq!(*app.view(), View::Agent(AgentId::new("agent-sec")));

        app.on_key(KeyCode::Down);
        assert_eq!(*app.view(), View::Agent(AgentId::new("agent-sec")));
        assert_eq!(
            app.world().selected,
            Some(Selected::Worker(WorkerId::new("worker-8k")))
        );
        app.on_key(KeyCode::Enter);
        assert_eq!(*app.view(), View::Worker(WorkerId::new("worker-8k")));

        app.on_key(KeyCode::Enter);
        assert_eq!(*app.view(), View::Logs(WorkerId::new("worker-8k")));

        // Mouse path: RAW LOGS + row clicks.
        app.on_key(KeyCode::Esc);
        assert_eq!(*app.view(), View::Worker(WorkerId::new("worker-8k")));
        app.clicked_raw_logs(&WorkerId::new("worker-8k"));
        assert_eq!(*app.view(), View::Logs(WorkerId::new("worker-8k")));
        app.on_key(KeyCode::Esc);
        app.clicked_worker(&WorkerId::new("worker-form4"));
        assert_eq!(*app.view(), View::Worker(WorkerId::new("worker-form4")));
        app.on_key(KeyCode::Esc);
        assert_eq!(*app.view(), View::Agent(AgentId::new("agent-sec")));
        app.clicked_agent(&AgentId::new("agent-news"));
        assert_eq!(*app.view(), View::Agent(AgentId::new("agent-news")));
        app.on_key(KeyCode::Esc);
        assert_eq!(*app.view(), View::World);

        // Esc at World stays; unknown ids are ignored.
        app.on_key(KeyCode::Esc);
        assert_eq!(*app.view(), View::World);
        app.dispatch(Intent::OpenAgent(AgentId::new("nope")));
        app.dispatch(Intent::OpenWorker(WorkerId::new("nope")));
        app.dispatch(Intent::OpenLogs(WorkerId::new("nope")));
        app.clicked_agent(&AgentId::new("nope"));
        assert_eq!(*app.view(), View::World);
    }
}
