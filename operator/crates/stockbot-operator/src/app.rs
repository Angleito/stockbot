//! Multi-workspace operator shell: sidebar + Pi chat, optional research above.
//!
//! Demo/fake only. Each [`Workspace`] owns its chat plus an optional seeded
//! research world; [`App`] is the only writer of selection, view, and focus.
//! Research ticks pump one [`fixtures::fake_sequence`] event when it adds
//! progress, otherwise only the counters advance.

use agent_ui::{
    AgentEvent, Selected, View, fixtures,
    ids::{AgentId, WorkerId},
    state::WorldState,
    views::{agent, worker, world},
};
use crossterm::event::{KeyCode, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};

use crate::ui_market::StartupAnim;
use crate::ui_shell::root_split;
use crate::workspace::{ChatBackend, DemoPiBackend, Workspace, WorkspaceMode};

/// Which pane owns the keyboard.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Focus {
    Sidebar,
    Research,
    Chat,
}

/// Workspaces shell. `research_age` is a global transient clock counting ticks
/// since the last `/research` submit (drives the title overlay).
pub struct App {
    workspaces: Vec<Workspace>,
    selected: usize,
    next_id: u64,
    pub focus: Focus,
    pub sidebar_scroll: u16,
    pub startup: StartupAnim,
    pub research_age: u64,
}

impl App {
    /// One `stockbot` Chat workspace, splash playing, focus in Chat.
    pub fn new() -> Self {
        Self {
            workspaces: vec![Workspace::new(0, "stockbot")],
            selected: 0,
            next_id: 1,
            focus: Focus::Chat,
            sidebar_scroll: 0,
            startup: StartupAnim::new(),
            research_age: 0,
        }
    }

    pub fn current(&self) -> &Workspace {
        &self.workspaces[self.selected]
    }

    pub fn current_mut(&mut self) -> &mut Workspace {
        &mut self.workspaces[self.selected]
    }

    pub fn workspaces(&self) -> &[Workspace] {
        &self.workspaces
    }

    pub fn selected(&self) -> usize {
        self.selected
    }

    /// Create `stockbot-N`, auto-select it, focus its chat.
    pub fn new_workspace(&mut self) {
        let id = self.next_id;
        self.next_id = self.next_id.saturating_add(1);
        self.workspaces.push(Workspace::new(id, format!("stockbot-{id}")));
        self.selected = self.workspaces.len() - 1;
        self.focus = Focus::Chat;
    }

    /// Select workspace `i`; Research focus falls back to Chat in Chat mode.
    pub fn select(&mut self, i: usize) {
        if i >= self.workspaces.len() {
            return;
        }
        self.selected = i;
        if (self.selected as u16) < self.sidebar_scroll {
            self.sidebar_scroll = self.selected as u16;
        }
        if self.focus == Focus::Research && self.current().mode() != WorkspaceMode::Research {
            self.focus = Focus::Chat;
        }
    }

    /// Submit the current input via [`DemoPiBackend`]: `/research ...` seeds a
    /// fake world and jumps to Research, everything else appends a bot reply.
    pub fn submit_chat(&mut self) {
        let input = std::mem::take(&mut self.current_mut().chat.input);
        let text = input.trim().to_owned();
        if text.is_empty() {
            return;
        }
        let reply = DemoPiBackend.reply(&text);
        let query = reply.strip_prefix("RESEARCH_START:").map(|q| q.trim().to_owned());
        match query {
            Some(query) => {
                let ws = self.current_mut();
                ws.chat.push_user(text);
                ws.start_research(&query);
                // Drill starts at Overview; the seed's own view would skip it.
                ws.view = View::World;
                if let Some(r) = ws.research.as_mut() {
                    r.world.apply(AgentEvent::ViewChanged(View::World));
                }
                self.research_age = 0;
                self.focus = Focus::Research;
            }
            None => {
                let ws = self.current_mut();
                ws.chat.push_user(text);
                ws.chat.push_bot(reply);
            }
        }
    }

    /// Advance one 50ms frame: splash first, else tick every workspace with
    /// research so background sessions progress while unfocused. Each pumps
    /// the next `fixtures::fake_sequence()` event when it adds progress;
    /// navigation events are skipped so the pump never steals the view.
    pub fn tick(&mut self) {
        if !self.startup.done() {
            self.startup.tick();
            return;
        }
        self.research_age = self.research_age.saturating_add(1);
        let seq = fixtures::fake_sequence();
        if seq.is_empty() {
            return;
        }
        for ws in &mut self.workspaces {
            let Some(r) = ws.research.as_mut() else {
                continue;
            };
            r.tick = r.tick.saturating_add(1);
            // ponytail: linear scan from cursor; wrap once. Timeline is ~26
            // events; index it if it ever grows.
            let mut advanced = false;
            for _ in 0..seq.len() {
                let event = seq[r.cursor % seq.len()].clone();
                r.cursor = r.cursor.saturating_add(1);
                if event_adds_progress(&r.world, &event) {
                    r.world.apply(event);
                    advanced = true;
                    break;
                }
            }
            let _ = advanced;
        }
    }

    /// Handle one key; returns true when the shell should quit.
    /// `q` quits outside Chat (so chat can type it); Ctrl-C always quits.
    /// `+` always creates (chat cannot type `+`); `k`/`j` type in Chat but
    /// navigate elsewhere so words stay typeable.
    pub fn on_key(&mut self, code: KeyCode, mods: KeyModifiers) -> bool {
        if mods.contains(KeyModifiers::CONTROL)
            && matches!(code, KeyCode::Char('c') | KeyCode::Char('C'))
        {
            return true;
        }
        if !self.startup.done() {
            match code {
                KeyCode::Char('q') | KeyCode::Char('Q') => return true,
                _ if StartupAnim::skip_key(code) => self.startup.skip(),
                _ => {}
            }
            return false;
        }
        match code {
            KeyCode::Tab => self.cycle_focus(true),
            KeyCode::BackTab => self.cycle_focus(false),
            KeyCode::Esc => self.esc(),
            KeyCode::Enter => {
                if self.focus == Focus::Research {
                    self.enter_child();
                } else {
                    self.submit_chat();
                }
            }
            KeyCode::Up => self.move_cursor(false),
            KeyCode::Down => self.move_cursor(true),
            KeyCode::Backspace => {
                if self.focus == Focus::Chat {
                    self.current_mut().chat.input.pop();
                }
            }
            KeyCode::Char(c) if typing_mods(mods) => return self.on_char(c),
            _ => {}
        }
        false
    }

    /// Left-click at `(col, row)` on a `cols x rows` terminal. Mirrors the
    /// render splits: sidebar rows select (`+ new` creates), TOP research
    /// hit-tests drill, chat clicks focus Chat.
    pub fn clicked(&mut self, col: u16, row: u16, cols: u16, rows: u16) {
        if !self.startup.done() || cols == 0 || rows < 3 {
            return;
        }
        // Same chrome as render: header 1, body fill, footer 1.
        let body = Rect::new(0, 1, cols, rows - 2);
        if body.is_empty() {
            return;
        }
        let (sidebar, workspace) = root_split(body);
        if in_rect(sidebar, col, row) {
            self.sidebar_click(row, sidebar);
        } else if in_rect(workspace, col, row) {
            self.workspace_click(col, row, workspace);
        }
    }

    /// Compat shim for `LiveFeed::pump_one`: folds one adapted event into the
    /// current research world, if any.
    #[allow(dead_code)]
    pub fn apply(&mut self, event: AgentEvent) {
        if let Some(r) = self.current_mut().research.as_mut() {
            r.world.apply(event);
        }
    }

    fn on_char(&mut self, c: char) -> bool {
        match c {
            '+' => self.new_workspace(),
            '/' => {
                if self.focus == Focus::Chat {
                    self.current_mut().chat.input.push('/');
                } else {
                    self.focus = Focus::Chat;
                }
            }
            'q' | 'Q' => {
                if self.focus == Focus::Chat {
                    self.current_mut().chat.input.push(c);
                } else {
                    return true;
                }
            }
            'k' => {
                if self.focus == Focus::Chat {
                    self.current_mut().chat.input.push('k');
                } else {
                    self.move_cursor(false);
                }
            }
            'j' => {
                if self.focus == Focus::Chat {
                    self.current_mut().chat.input.push('j');
                } else {
                    self.move_cursor(true);
                }
            }
            _ => {
                if self.focus == Focus::Chat {
                    self.current_mut().chat.input.push(c);
                }
            }
        }
        false
    }

    fn cycle_focus(&mut self, forward: bool) {
        // Research is skipped in Chat mode by construction.
        let order: &[Focus] = if self.current().mode() == WorkspaceMode::Research {
            &[Focus::Sidebar, Focus::Research, Focus::Chat]
        } else {
            &[Focus::Sidebar, Focus::Chat]
        };
        let pos = order.iter().position(|&f| f == self.focus).unwrap_or(0);
        let next = if forward {
            (pos + 1) % order.len()
        } else {
            (pos + order.len() - 1) % order.len()
        };
        self.focus = order[next];
    }

    /// Esc: research parent when nested, else park focus in the sidebar.
    fn esc(&mut self) {
        let nested =
            self.current().research.is_some() && self.current().view != View::World;
        if nested {
            self.go_parent();
        } else {
            self.focus = Focus::Sidebar;
        }
    }

    fn move_cursor(&mut self, forward: bool) {
        if self.focus == Focus::Sidebar {
            if forward {
                self.select_next();
            } else {
                self.select_prev();
            }
        } else {
            self.step(forward);
        }
    }

    fn select_prev(&mut self) {
        let n = self.workspaces.len();
        if n == 0 {
            return;
        }
        let prev = (self.selected + n - 1) % n;
        self.select(prev);
    }

    fn select_next(&mut self) {
        let n = self.workspaces.len();
        if n == 0 {
            return;
        }
        let next = (self.selected + 1) % n;
        self.select(next);
        // `select` only clamps upward scroll; wrap-around restarts at top.
        if next == 0 {
            self.sidebar_scroll = 0;
        }
    }

    fn sidebar_click(&mut self, row: u16, sidebar: Rect) {
        let capacity = sidebar.height.saturating_sub(2) as usize;
        if capacity == 0 {
            return;
        }
        let content_y = sidebar.y.saturating_add(1);
        let Some(rel) = row.checked_sub(content_y).map(|r| r as usize) else {
            return;
        };
        let scroll = self.sidebar_scroll as usize;
        let visible = self.workspaces.len().saturating_sub(scroll).min(capacity.saturating_sub(1));
        if rel < visible {
            self.focus = Focus::Sidebar;
            self.select(scroll + rel);
        } else if rel == visible && visible < capacity {
            // Trailing `+ new` row.
            self.new_workspace();
        }
    }

    fn workspace_click(&mut self, col: u16, row: u16, workspace: Rect) {
        if self.current().mode() != WorkspaceMode::Research {
            self.focus = Focus::Chat;
            return;
        }
        let (top, bottom) = research_split(workspace);
        if in_rect(top, col, row) {
            self.focus = Focus::Research;
            self.research_click(col, row, top);
        } else if in_rect(bottom, col, row) {
            self.focus = Focus::Chat;
        }
    }

    fn research_click(&mut self, col: u16, row: u16, top: Rect) {
        use ratatui::layout::{Constraint, Layout};
        // Same title split as `render_research`: title 2, content rest.
        let content = Layout::vertical([Constraint::Length(2), Constraint::Min(0)])
            .split(top)[1];
        match self.current().view.clone() {
            View::World => {
                let hit = self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| world::agent_at(content, &r.world, col, row));
                if let Some(id) = hit {
                    self.open_agent(&id);
                }
            }
            View::Agent(id) => {
                let hit = self.current().research.as_ref().and_then(|r| {
                    agent::worker_at(content, &r.world, &id, col, row)
                });
                if let Some(wid) = hit {
                    self.open_worker(&wid);
                }
            }
            View::Worker(id) => {
                if worker::raw_logs_hit(content, col, row) {
                    self.open_logs(&id);
                }
            }
            View::Logs(_) => {}
        }
    }

    /// Enter drills into the child view, falling back to the first child so
    /// keyboard alone reaches Logs.
    fn enter_child(&mut self) {
        match self.current().view.clone() {
            View::World => {
                let sel = self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| r.world.selected.clone());
                match sel {
                    Some(Selected::Agent(id))
                        if self
                            .current()
                            .research
                            .as_ref()
                            .is_some_and(|r| r.world.agents.contains_key(&id)) =>
                    {
                        self.open_agent(&id);
                    }
                    _ => self.open_first_agent(),
                }
            }
            View::Agent(agent_id) => {
                let ours = self.current().research.as_ref().is_some_and(|r| {
                    matches!(&r.world.selected, Some(Selected::Worker(w))
                        if r.world.agents.get(&agent_id).is_some_and(|a| a.workers.contains(w)))
                });
                match self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| r.world.selected.clone())
                {
                    Some(Selected::Worker(w)) if ours => self.open_worker(&w),
                    _ => self.open_first_worker(&agent_id),
                }
            }
            View::Worker(id) => self.open_logs(&id),
            View::Logs(_) => {}
        }
    }

    /// Esc goes to the parent view. Esc at `World` stays put (caller parks
    /// focus in the sidebar instead).
    fn go_parent(&mut self) {
        match self.current().view.clone() {
            View::World => {}
            View::Agent(id) => {
                self.set_selection(Some(Selected::Agent(id)));
                self.set_view(View::World);
            }
            View::Worker(id) => {
                let agent = self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| r.world.workers.get(&id))
                    .map(|w| w.agent_id.clone());
                match agent {
                    Some(aid) => {
                        self.set_selection(Some(Selected::Worker(id)));
                        self.set_view(View::Agent(aid));
                    }
                    None => self.set_view(View::World),
                }
            }
            View::Logs(id) => {
                let known = self
                    .current()
                    .research
                    .as_ref()
                    .is_some_and(|r| r.world.workers.contains_key(&id));
                if known {
                    self.set_selection(Some(Selected::Worker(id.clone())));
                    self.set_view(View::Worker(id));
                } else {
                    self.set_view(View::World);
                }
            }
        }
    }

    fn open_agent(&mut self, id: &AgentId) {
        let ok = self
            .current()
            .research
            .as_ref()
            .is_some_and(|r| r.world.agents.contains_key(id));
        if !ok {
            return;
        }
        self.set_selection(Some(Selected::Agent(id.clone())));
        self.set_view(View::Agent(id.clone()));
    }

    fn open_worker(&mut self, id: &WorkerId) {
        let ok = self
            .current()
            .research
            .as_ref()
            .is_some_and(|r| r.world.workers.contains_key(id));
        if !ok {
            return;
        }
        self.set_selection(Some(Selected::Worker(id.clone())));
        self.set_view(View::Worker(id.clone()));
    }

    fn open_logs(&mut self, id: &WorkerId) {
        let ok = self
            .current()
            .research
            .as_ref()
            .is_some_and(|r| r.world.workers.contains_key(id));
        if !ok {
            return;
        }
        self.set_selection(Some(Selected::Worker(id.clone())));
        self.set_view(View::Logs(id.clone()));
    }

    /// Move the highlight within the current research list, wrapping.
    fn step(&mut self, forward: bool) {
        match self.current().view.clone() {
            View::World => {
                let ids = self.sorted_agents();
                if ids.is_empty() {
                    return;
                }
                let cur = self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| r.world.selected.clone());
                let next = match cur {
                    Some(Selected::Agent(id)) => ids
                        .iter()
                        .position(|x| x == &id)
                        .map(|i| ids[(i + if forward { 1 } else { ids.len() - 1 }) % ids.len()].clone()),
                    _ => Some(if forward {
                        ids[0].clone()
                    } else {
                        ids[ids.len() - 1].clone()
                    }),
                };
                if let Some(id) = next {
                    self.set_selection(Some(Selected::Agent(id)));
                }
            }
            View::Agent(agent_id) => {
                let workers = self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| r.world.agents.get(&agent_id))
                    .map(|a| a.workers.clone())
                    .unwrap_or_default();
                if workers.is_empty() {
                    return;
                }
                let cur = self
                    .current()
                    .research
                    .as_ref()
                    .and_then(|r| r.world.selected.clone());
                let next = match cur {
                    Some(Selected::Worker(id)) => workers
                        .iter()
                        .position(|x| x == &id)
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
                    self.set_selection(Some(Selected::Worker(id)));
                }
            }
            View::Worker(_) | View::Logs(_) => {}
        }
    }

    fn set_selection(&mut self, sel: Option<Selected>) {
        if let Some(r) = self.current_mut().research.as_mut() {
            r.world.apply(AgentEvent::SelectionChanged(sel));
        }
    }

    fn set_view(&mut self, view: View) {
        if let Some(r) = self.current_mut().research.as_mut() {
            r.world.apply(AgentEvent::ViewChanged(view.clone()));
        }
        self.current_mut().view = view;
    }

    fn sorted_agents(&self) -> Vec<AgentId> {
        match self.current().research.as_ref() {
            Some(r) => {
                let mut ids: Vec<AgentId> = r.world.agents.keys().cloned().collect();
                ids.sort();
                ids
            }
            None => Vec::new(),
        }
    }

    fn first_worker_of(&self, agent_id: &AgentId) -> Option<WorkerId> {
        self.current()
            .research
            .as_ref()
            .and_then(|r| r.world.agents.get(agent_id))
            .and_then(|a| a.workers.first().cloned())
    }

    fn open_first_agent(&mut self) {
        let first = self.sorted_agents().into_iter().next();
        if let Some(id) = first {
            self.open_agent(&id);
        }
    }

    fn open_first_worker(&mut self, agent_id: &AgentId) {
        let first = self.first_worker_of(agent_id);
        if let Some(id) = first {
            self.open_worker(&id);
        }
    }
}

impl Default for App {
    fn default() -> Self {
        Self::new()
    }
}

/// 67/33 vertical split of the workspace area: research TOP, chat BOTTOM.
/// Tall screens use `Percentage(67)`/`Percentage(33)` (already top Min(5) /
/// bottom Min(3) at height >= 8); short screens clamp to top Min(5) / bottom
/// Min(3), tiny screens split half. Hit-tests reuse this so mouse and render
/// never drift apart.
pub fn research_split(area: Rect) -> (Rect, Rect) {
    if area.is_empty() {
        return (area, area);
    }
    let h = area.height;
    // Tall: Percentage(67)/Percentage(33) clears top Min(5)/bottom Min(3).
    // Short: hold bottom Min(3) so chat stays usable. Tiny: even split.
    let chunks = if h >= 8 {
        Layout::vertical([Constraint::Percentage(67), Constraint::Percentage(33)]).split(area)
    } else if h >= 4 {
        Layout::vertical([Constraint::Min(0), Constraint::Length(3)]).split(area)
    } else {
        Layout::vertical([Constraint::Percentage(50), Constraint::Percentage(50)]).split(area)
    };
    (chunks[0], chunks[1])
}

fn in_rect(area: Rect, col: u16, row: u16) -> bool {
    !area.is_empty()
        && col >= area.x
        && row >= area.y
        && col < area.x.saturating_add(area.width)
        && row < area.y.saturating_add(area.height)
}

fn typing_mods(mods: KeyModifiers) -> bool {
    !mods.contains(KeyModifiers::CONTROL) && !mods.contains(KeyModifiers::ALT)
}

/// True when applying `event` would change the world. Navigation events never
/// do (the pump must not steal the view); push-type events skip exact
/// duplicates so cycling the fixture stays bounded.
fn event_adds_progress(world: &WorldState, event: &AgentEvent) -> bool {
    match event {
        AgentEvent::SelectionChanged(_) | AgentEvent::ViewChanged(_) => false,
        AgentEvent::AgentAdded { id, .. } => !world.agents.contains_key(id),
        AgentEvent::AgentStatus { id, status } => {
            world.agents.get(id).is_some_and(|a| a.status != *status)
        }
        AgentEvent::WorkerAdded { id, .. } => !world.workers.contains_key(id),
        AgentEvent::WorkerStatus { id, status } => {
            world.workers.get(id).is_some_and(|w| w.status != *status)
        }
        AgentEvent::WorkerProgress { id, done, total } => world
            .workers
            .get(id)
            .is_some_and(|w| w.progress.done != *done || w.progress.total != *total),
        AgentEvent::WorkerDetails { id, started_at, runtime_secs, model, objective } => {
            world.workers.get(id).is_some_and(|w| {
                w.started_at != *started_at
                    || w.runtime_secs != *runtime_secs
                    || w.model != *model
                    || w.objective != *objective
            })
        }
        AgentEvent::WorkerLog { id, line } => {
            world.workers.get(id).is_some_and(|w| !w.logs.contains(line))
        }
        AgentEvent::WorkerStageChanged { worker_id, stage } => world
            .workers
            .get(worker_id)
            .is_some_and(|w| w.current_stage != *stage),
        AgentEvent::FindingAdded { worker_id, finding } => world
            .workers
            .get(worker_id)
            .is_some_and(|w| !w.findings.contains(finding)),
        AgentEvent::ArtifactAdded { worker_id, artifact } => world
            .workers
            .get(worker_id)
            .is_some_and(|w| !w.artifacts.iter().any(|a| a.id == artifact.id)),
        AgentEvent::EvidenceAdded { worker_id, evidence } => world
            .workers
            .get(worker_id)
            .is_some_and(|w| !w.evidence.contains(evidence)),
        AgentEvent::AuditAdded { summary } => world
            .events
            .last()
            .map(|e| e.summary != *summary)
            .unwrap_or(true),
    }
}

#[cfg(test)]
mod shell_tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::layout::Rect;

    fn key(code: KeyCode) -> (KeyCode, KeyModifiers) {
        (code, KeyModifiers::empty())
    }

    #[test]
    fn plus_creates_and_switch_preserves_state() {
        let mut app = App::new();
        app.startup.skip();
        app.new_workspace();
        assert_eq!(app.workspaces().len(), 2);
        assert_eq!(app.selected(), 1);
        assert_eq!(app.current().title, "stockbot-1");
        app.current_mut().chat.input = "hello".to_owned();
        app.submit_chat();
        app.select(0);
        app.new_workspace();
        app.current_mut().chat.input = "/research investigate NVDA".to_owned();
        app.submit_chat();
        assert_eq!(app.current().mode(), WorkspaceMode::Research);
        app.select(1);
        assert_eq!(app.current().mode(), WorkspaceMode::Chat);
        assert!(app.current().chat.messages.iter().any(|m| m.from_user));
        app.select(2);
        assert_eq!(app.current().mode(), WorkspaceMode::Research);
        assert!(app.current().research.is_some());
    }

    #[test]
    fn research_split_drills_with_chat_below() {
        let mut app = App::new();
        app.startup.skip();
        app.current_mut().chat.input = "/research investigate NVDA".to_owned();
        app.submit_chat();
        assert_eq!(app.current().mode(), WorkspaceMode::Research);
        // STARTING: empty world with ticker-derived title.
        let r = app.current().research.as_ref().unwrap();
        assert_eq!(r.title, "NVDA RESEARCH");
        assert!(r.world.agents.is_empty());
        // Pump: agents appear progressively, background too.
        for _ in 0..3 {
            app.tick();
        }
        assert!(!app.current().research.as_ref().unwrap().world.agents.is_empty());
        let area = Rect::new(20, 1, 100, 30);
        let (top, bottom) = research_split(area);
        assert!(top.height > bottom.height);
        assert!(bottom.height >= 3);
        assert!(app.current().chat.messages.iter().any(|m| m.from_user));
        app.focus = Focus::Research;
        // Drill needs a selected agent; seed selection like the old shell did.
        app.current_mut().research.as_mut().unwrap().world.apply(
            AgentEvent::SelectionChanged(Some(Selected::Agent(AgentId::new("agent-sec")))),
        );
        let (code, mods) = key(KeyCode::Enter);
        app.on_key(code, mods);
        assert_ne!(app.current().view, View::World);
        let (code, mods) = key(KeyCode::Esc);
        app.on_key(code, mods);
        assert_eq!(app.current().view, View::World);
        assert_eq!(app.current().mode(), WorkspaceMode::Research);
    }

    #[test]
    fn background_research_ticks_while_unfocused() {
        let mut app = App::new();
        app.startup.skip();
        app.current_mut().chat.input = "/research investigate NVDA".to_owned();
        app.submit_chat();
        app.new_workspace();
        assert_eq!(app.current().mode(), WorkspaceMode::Chat);
        for _ in 0..3 {
            app.tick();
        }
        app.select(0);
        assert!(!app.current().research.as_ref().unwrap().world.agents.is_empty());
    }
}
