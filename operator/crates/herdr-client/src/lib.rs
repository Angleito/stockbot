//! Minimal Herdr socket client for the trusted stockbot operator.
//!
//! # Trusted-operator-only
//!
//! The Herdr Unix socket lets its holder list, read, focus, and type into the
//! user's live terminals. That power stays inside the operator process the
//! user launched: never expose this client over the network, never forward
//! its handle to an untrusted peer, and never ship socket bytes to a log
//! sink. A missing/unreachable socket is a normal state (Herdr not running),
//! reported as [`Error::NotConnected`], never a panic.
//!
//! # Ground rules
//!
//! - Linux first: plain [`std::os::unix::net::UnixStream`], newline-delimited
//!   JSON (`{"id","method","params"}` → `{"id","result"|"error"}`).
//! - PTY output is RAW LOG TEXT only. This crate returns it verbatim and
//!   never interprets lifecycle fields from it; parse state from typed event
//!   payloads ([`HerdrEvent`]) or [`AgentInfo::agent_status`] instead.
//! - Every fallible call returns [`Error`]; this crate never panics on
//!   malformed server output (unknown fields are ignored).

use std::{
    io::{BufRead, BufReader, Write},
    os::unix::net::UnixStream,
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::Duration,
};

/// Per-RPC socket timeout; [`EventStream`] blocks until the caller sets one.
const RPC_TIMEOUT: Duration = Duration::from_secs(10);

/// Clean failure modes. [`Error::NotConnected`] covers the absent-socket case.
#[derive(Debug)]
pub enum Error {
    /// No Herdr daemon at the socket path, or the stream died.
    NotConnected(String),
    /// Daemon answered with `{code, message}` (e.g. `pane_not_found`).
    Server { code: String, message: String },
    /// I/O or JSON framing went wrong mid-call.
    Protocol(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NotConnected(detail) => write!(f, "herdr not connected: {detail}"),
            Self::Server { code, message } => write!(f, "herdr {code}: {message}"),
            Self::Protocol(detail) => write!(f, "herdr protocol: {detail}"),
        }
    }
}

impl std::error::Error for Error {}

/// Trimmed pane handle. Unknown server fields are ignored.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct PaneInfo {
    #[serde(default)]
    pub pane_id: String,
    #[serde(default)]
    pub workspace_id: String,
    #[serde(default)]
    pub tab_id: String,
    #[serde(default)]
    pub focused: bool,
}

/// Trimmed agent handle. `agent_status` is one of
/// `idle|working|blocked|done|unknown`; match on it, never on PTY text.
#[derive(Debug, Clone, serde::Deserialize)]
pub struct AgentInfo {
    #[serde(default)]
    pub pane_id: String,
    /// Agent kind (e.g. `"omp"`).
    #[serde(default)]
    pub agent: Option<String>,
    /// Unique live agent name, when the pane occupant has one.
    #[serde(default)]
    pub name: Option<String>,
    #[serde(default)]
    pub agent_status: String,
    #[serde(default)]
    pub workspace_id: String,
    #[serde(default)]
    pub tab_id: String,
}

/// One pushed subscription event: the raw `data` object, unparsed.
/// Use the accessors for the fields live wiring needs; match on their
/// presence, not on PTY text.
#[derive(Debug, Clone)]
pub struct HerdrEvent(pub serde_json::Value);

impl HerdrEvent {
    pub fn data(&self) -> &serde_json::Value {
        &self.0
    }
    pub fn pane_id(&self) -> Option<&str> {
        self.0.get("pane_id")?.as_str()
    }
    pub fn agent_status(&self) -> Option<&str> {
        self.0.get("agent_status")?.as_str()
    }
    /// The line that tripped a `pane.output_matched` subscription.
    pub fn matched_line(&self) -> Option<&str> {
        self.0.get("matched_line")?.as_str()
    }
    /// Raw PTY chunk bundled with the event. Verbatim logs, never parsed here.
    pub fn read_text(&self) -> Option<&str> {
        self.0.get("read")?.get("text")?.as_str()
    }
}

/// Blocking event stream from [`Client::events_subscribe`].
pub struct EventStream {
    reader: BufReader<UnixStream>,
}

impl EventStream {
    /// Bound [`next_event`](Self::next_event); `None` blocks indefinitely.
    pub fn set_timeout(&self, timeout: Option<Duration>) -> Result<(), Error> {
        self.reader
            .get_ref()
            .set_read_timeout(timeout)
            .map_err(|e| Error::Protocol(e.to_string()))
    }

    /// Next pushed `{"data": …}` event. Server hangup → [`Error::NotConnected`].
    pub fn next_event(&mut self) -> Result<HerdrEvent, Error> {
        let mut line = String::new();
        let n = self
            .reader
            .read_line(&mut line)
            .map_err(|e| Error::Protocol(e.to_string()))?;
        if n == 0 {
            return Err(Error::NotConnected("event stream closed".into()));
        }
        let value: serde_json::Value =
            serde_json::from_str(&line).map_err(|e| Error::Protocol(e.to_string()))?;
        match value.get("data") {
            Some(data) => Ok(HerdrEvent(data.clone())),
            None => Err(Error::Protocol("event without data".into())),
        }
    }
}

pub struct Client {
    socket_path: PathBuf,
    next_id: AtomicU64,
}

impl Client {
    /// `HERDR_SOCKET_PATH`, else `~/.config/herdr/herdr.sock`. Never fails.
    pub fn new() -> Self {
        Self::with_socket_path(default_socket_path())
    }

    pub fn with_socket_path(path: impl Into<PathBuf>) -> Self {
        Self {
            socket_path: path.into(),
            next_id: AtomicU64::new(1),
        }
    }

    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    /// Raw PTY text for a pane, verbatim. `lines` caps scrollback rows.
    pub fn pane_read(&self, pane_id: &str, lines: u32) -> Result<String, Error> {
        let result = self.rpc(
            "pane.read",
            serde_json::json!({"pane_id": pane_id, "source": "recent_unwrapped", "lines": lines}),
        )?;
        result
            .get("read")
            .and_then(|r| r.get("text"))
            .and_then(|t| t.as_str())
            .map(str::to_owned)
            .ok_or_else(|| Error::Protocol("pane.read without read.text".into()))
    }

    pub fn pane_list(&self, workspace_id: Option<&str>) -> Result<Vec<PaneInfo>, Error> {
        let result = self.rpc("pane.list", serde_json::json!({"workspace_id": workspace_id}))?;
        result
            .get("panes")
            .cloned()
            .ok_or_else(|| Error::Protocol("pane.list without panes".into()))
            .and_then(from_value)
    }

    pub fn pane_focus(&self, pane_id: &str) -> Result<PaneInfo, Error> {
        let result = self.rpc("pane.focus", serde_json::json!({"pane_id": pane_id}))?;
        result.get("pane").cloned().map_or_else(
            || Err(Error::Protocol("pane.focus without pane".into())),
            from_value,
        )
    }

    /// Literal text, no Enter appended. Pass `"cmd\n"` (or `pane.send_input`
    /// keys) when submission is intended.
    pub fn pane_send(&self, pane_id: &str, text: &str) -> Result<(), Error> {
        self.rpc(
            "pane.send_text",
            serde_json::json!({"pane_id": pane_id, "text": text}),
        )?;
        Ok(())
    }

    pub fn agent_list(&self) -> Result<Vec<AgentInfo>, Error> {
        let result = self.rpc("agent.list", serde_json::json!({}))?;
        result
            .get("agents")
            .cloned()
            .ok_or_else(|| Error::Protocol("agent.list without agents".into()))
            .and_then(from_value)
    }

    /// `target` is a unique live agent name or a pane id (`wB:p1`).
    pub fn agent_get(&self, target: &str) -> Result<AgentInfo, Error> {
        let result = self.rpc("agent.get", serde_json::json!({"target": target}))?;
        result.get("agent").cloned().map_or_else(
            || Err(Error::Protocol("agent.get without agent".into())),
            from_value,
        )
    }

    /// Subscribe, e.g. `json!({"type":"pane.exited"})` or
    /// `json!({"type":"pane.output_matched","pane_id":id,"source":"recent_unwrapped",
    /// "match":{"type":"substring","value":"…"}})`. Returns the stream after
    /// the server acks `subscription_started`; pushes arrive via `next_event`.
    pub fn events_subscribe(
        &self,
        subscriptions: &[serde_json::Value],
    ) -> Result<EventStream, Error> {
        let id = self.claim_id();
        let stream =
            UnixStream::connect(&self.socket_path).map_err(|e| not_connected(&self.socket_path, e))?;
        stream
            .set_write_timeout(Some(RPC_TIMEOUT))
            .map_err(|e| Error::Protocol(e.to_string()))?;
        let mut stream = stream;
        let req = serde_json::json!({"id": id, "method": "events.subscribe",
            "params": {"subscriptions": subscriptions}});
        stream
            .write_all(req.to_string().as_bytes())
            .and_then(|()| stream.write_all(b"\n"))
            .map_err(|e| Error::Protocol(e.to_string()))?;
        stream
            .set_read_timeout(Some(RPC_TIMEOUT))
            .map_err(|e| Error::Protocol(e.to_string()))?;
        let mut reader = BufReader::new(stream);
        let mut line = String::new();
        reader
            .read_line(&mut line)
            .map_err(|e| Error::Protocol(e.to_string()))?;
        if line.is_empty() {
            return Err(Error::NotConnected("event stream closed".into()));
        }
        let ack: serde_json::Value =
            serde_json::from_str(&line).map_err(|e| Error::Protocol(e.to_string()))?;
        if let Some(err) = ack.get("error") {
            return Err(server_error(err));
        }
        Ok(EventStream { reader })
    }

    fn rpc(&self, method: &str, params: serde_json::Value) -> Result<serde_json::Value, Error> {
        let id = self.claim_id();
        let stream =
            UnixStream::connect(&self.socket_path).map_err(|e| not_connected(&self.socket_path, e))?;
        stream
            .set_read_timeout(Some(RPC_TIMEOUT))
            .and_then(|()| stream.set_write_timeout(Some(RPC_TIMEOUT)))
            .map_err(|e| Error::Protocol(e.to_string()))?;
        let mut stream = stream;
        let req = serde_json::json!({"id": id, "method": method, "params": params});
        stream
            .write_all(req.to_string().as_bytes())
            .and_then(|()| stream.write_all(b"\n"))
            .map_err(|e| Error::Protocol(e.to_string()))?;
        let mut line = String::new();
        BufReader::new(&stream)
            .read_line(&mut line)
            .map_err(|e| Error::Protocol(e.to_string()))?;
        if line.is_empty() {
            return Err(Error::NotConnected("server closed connection".into()));
        }
        let reply: serde_json::Value =
            serde_json::from_str(&line).map_err(|e| Error::Protocol(e.to_string()))?;
        if let Some(err) = reply.get("error") {
            return Err(server_error(err));
        }
        reply
            .get("result")
            .cloned()
            .ok_or_else(|| Error::Protocol("reply without result".into()))
    }

    fn claim_id(&self) -> String {
        format!("herdr-client-{}", self.next_id.fetch_add(1, Ordering::Relaxed))
    }
}

impl Default for Client {
    fn default() -> Self {
        Self::new()
    }
}

fn default_socket_path() -> PathBuf {
    if let Ok(path) = std::env::var("HERDR_SOCKET_PATH") {
        if !path.is_empty() {
            return PathBuf::from(path);
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| String::from("/root"));
    Path::new(&home).join(".config/herdr/herdr.sock")
}

fn not_connected(path: &Path, source: std::io::Error) -> Error {
    Error::NotConnected(format!("{}: {source}", path.display()))
}

fn server_error(body: &serde_json::Value) -> Error {
    Error::Server {
        code: body
            .get("code")
            .and_then(|c| c.as_str())
            .unwrap_or("unknown")
            .to_owned(),
        message: body
            .get("message")
            .and_then(|m| m.as_str())
            .unwrap_or("unknown error")
            .to_owned(),
    }
}

fn from_value<T: serde::de::DeserializeOwned>(value: serde_json::Value) -> Result<T, Error> {
    serde_json::from_value(value).map_err(|e| Error::Protocol(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn absent_socket_is_not_connected() {
        let client =
            Client::with_socket_path("/nonexistent/herdr-client-test/herdr.sock");
        assert!(matches!(
            client.pane_list(None),
            Err(Error::NotConnected(_))
        ));
        assert!(matches!(
            client.pane_read("wB:p1", 5),
            Err(Error::NotConnected(_))
        ));
        assert!(matches!(
            client.events_subscribe(&[]),
            Err(Error::NotConnected(_))
        ));
    }
}
