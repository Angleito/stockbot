//! Fake multi-workspace state: Pi chat below, optional research above.
//! Demo/fake only. No Herdr imports, no render code here.

/// Workspace identifier.
pub type WorkspaceId = u64;

/// Derived from `Workspace.research.is_none()`.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum WorkspaceMode {
    Chat,
    Research,
}

/// One chat line. `from_user=true` is the operator, else Pi.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ChatMsg {
    pub from_user: bool,
    pub text: String,
}

/// Pi chat: history plus the current input line.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ChatState {
    pub messages: Vec<ChatMsg>,
    pub input: String,
}

impl ChatState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push_user(&mut self, text: impl Into<String>) {
        self.messages.push(ChatMsg { from_user: true, text: text.into() });
    }

    pub fn push_bot(&mut self, text: impl Into<String>) {
        self.messages.push(ChatMsg { from_user: false, text: text.into() });
    }
}

/// Fake Pi responder. Real backend replaces this, same trait.
pub trait ChatBackend {
    fn reply(&self, input: &str) -> String;
}

/// Demo responder: `/research` yields a research-intent marker, `hello`
/// yields a fake stream line, everything else echoes.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct DemoPiBackend;

impl ChatBackend for DemoPiBackend {
    fn reply(&self, input: &str) -> String {
        let trimmed = input.trim();
        if trimmed.starts_with("/research") {
            let query = trimmed.strip_prefix("/research").unwrap_or("").trim();
            return format!("RESEARCH_START:{query}");
        }
        if trimmed.to_ascii_lowercase().contains("hello") {
            return "Pi: streaming fake SEC 8-K scan...".to_owned();
        }
        format!("Pi: {trimmed}")
    }
}

/// Fake research pane: empty world at STARTING, filled per-tick from the
/// fixture timeline. `cursor` indexes `fixtures::fake_sequence()`.
#[derive(Clone, Debug)]
pub struct ResearchState {
    pub world: agent_ui::WorldState,
    pub tick: u64,
    pub title: String,
    pub query: String,
    pub cursor: usize,
}

/// One sidebar entry: always has chat, optionally has research.
#[derive(Clone, Debug)]
pub struct Workspace {
    pub id: WorkspaceId,
    pub title: String,
    pub chat: ChatState,
    pub research: Option<ResearchState>,
    pub view: agent_ui::View,
}

impl Workspace {
    /// New workspace starts in Chat mode: empty chat, `View::World`.
    pub fn new(id: WorkspaceId, title: impl Into<String>) -> Self {
        Self {
            id,
            title: title.into(),
            chat: ChatState::new(),
            research: None,
            view: agent_ui::View::World,
        }
    }

    /// Derived from `research.is_none()`; no stored flag.
    pub fn mode(&self) -> WorkspaceMode {
        if self.research.is_none() { WorkspaceMode::Chat } else { WorkspaceMode::Research }
    }

    /// Enter Research mode empty at STARTING: title from the query's ticker
    /// (`NVDA RESEARCH`), timeline pumped per-tick by `App::tick`.
    pub fn start_research(&mut self, query: &str) {
        let query = query.trim().to_owned();
        self.research = Some(ResearchState {
            world: agent_ui::WorldState::new(),
            tick: 0,
            title: research_title(&query),
            query,
            cursor: 0,
        });
    }
}

/// Query -> `NVDA RESEARCH`: first ALL-CAPS/ticker-like token, else last
/// token, else `STOCKBOT`. `NVDA's` counts as `NVDA`.
pub fn research_title(query: &str) -> String {
    let tokens: Vec<&str> = query.split_whitespace().collect();
    for token in &tokens {
        let edge = token.trim_matches(|c: char| !c.is_alphanumeric());
        let head = edge.split(|c: char| !c.is_alphanumeric()).next().unwrap_or("");
        if (1..=5).contains(&head.len()) && head.chars().all(|c| c.is_ascii_uppercase()) {
            return format!("{head} RESEARCH");
        }
    }
    let fallback = tokens
        .last()
        .map(|t| t.trim_matches(|c: char| !c.is_alphanumeric()))
        .filter(|t| !t.is_empty())
        .unwrap_or("STOCKBOT");
    format!("{} RESEARCH", fallback.to_ascii_uppercase())
}

#[cfg(test)]
mod title_tests {
    use super::*;

    #[test]
    fn picks_ticker_not_first_word() {
        assert_eq!(research_title("investigate NVDA's latest filings"), "NVDA RESEARCH");
        assert_eq!(research_title("/research investigate NVDA"), "NVDA RESEARCH");
        assert_eq!(research_title("explain what VICI owns"), "VICI RESEARCH");
        assert_eq!(research_title("compare it to Realty Income"), "INCOME RESEARCH");
        assert_eq!(research_title(""), "STOCKBOT RESEARCH");
    }
}
