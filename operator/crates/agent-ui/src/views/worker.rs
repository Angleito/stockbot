//! Full worker detail page.
//!
//! Renders every [`WorkerState`] field from the generic model: identity,
//! status, runtime details (started, runtime, model, objective), progress,
//! findings, evidence, artifacts, plus a `RAW LOGS` link hint. Labels and
//! bodies never share a line: each finding/artifact/evidence label sits on
//! its own row with its body/detail indented below, so stage-like metadata
//! never merges into finding text. No domain language.

use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
    widgets::{Block, Borders, Paragraph},
};

use crate::{ids::WorkerId, state::WorldState};

use super::agent::{status_glyph, status_label};

/// Pure page lines. Every [`WorkerState`] field appears; `RAW LOGS` is always
/// the last line so tests and the app shell can find the link target.
pub fn worker_lines(worker: &crate::state::WorkerState) -> Vec<String> {
    let mut out = vec![
        format!("{} {}", worker.name, worker.id.as_str()),
        format!("agent: {}", worker.agent_id.as_str()),
        format!(
            "status: {} {}",
            status_glyph(worker.status),
            status_label(worker.status)
        ),
        format!("started: {}", worker.started_at),
        format!("runtime: {}s", worker.runtime_secs),
        format!("model: {}", worker.model),
        format!("objective: {}", worker.objective),
        format!("stage: {}", worker.current_stage),
        format!(
            "progress: {}/{} ({}%)",
            worker.progress.done,
            worker.progress.total,
            worker.progress.percent()
        ),
    ];
    out.push(format!("findings: {}", worker.findings.len()));
    for finding in &worker.findings {
        out.push(format!("  - {}", finding));
    }
    out.push(format!("evidence: {}", worker.evidence.len()));
    for ev in &worker.evidence {
        out.push(format!("  - {}", ev.label));
        out.push(format!("    {}", ev.detail));
    }
    out.push(format!("artifacts: {}", worker.artifacts.len()));
    for a in &worker.artifacts {
        out.push(format!("  - {}", a.label));
        out.push(format!("    {}", a.body));
    }
    out.push(format!("log lines: {}", worker.logs.len()));
    out.push("RAW LOGS".to_owned());
    out
}

/// Resize-safe render: scrollable body + one-line `RAW LOGS` footer hint.
/// Unknown id renders a placeholder; navigation itself stays in the app
/// shell, which maps the hint key to the logs view.
pub fn render(frame: &mut Frame, area: Rect, world: &WorldState, id: &WorkerId) {
    let Some(worker) = world.workers.get(id) else {
        frame.render_widget(
            Paragraph::new("unknown worker").block(Block::default().borders(Borders::ALL).title("worker")),
            area,
        );
        return;
    };
    let chunks = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(area);
    let body = worker_lines(worker).join("\n");
    let title = format!(
        "{} {} [{}]",
        status_glyph(worker.status),
        worker.name,
        status_label(worker.status)
    );
    frame.render_widget(
        Paragraph::new(body).block(Block::default().borders(Borders::ALL).title(title)),
        chunks[0],
    );
    frame.render_widget(Paragraph::new("enter: RAW LOGS · q: back"), chunks[1]);
}
/// Hit-test a click at terminal cell `(x, y)` against the `RAW LOGS` targets.
/// Mirrors [`render`]: the same body / footer-1 vertical split. The footer
/// hint row always hits; the last content row of the bordered body (where
/// `RAW LOGS` sits when the body fits) hits too. Anything else — including
/// empty areas, clicks outside, and body borders — returns `false`, never
/// panics.
pub fn raw_logs_hit(area: Rect, x: u16, y: u16) -> bool {
    if area.is_empty() {
        return false;
    }
    if x < area.x
        || y < area.y
        || x >= area.x.saturating_add(area.width)
        || y >= area.y.saturating_add(area.height)
    {
        return false;
    }
    // Same split as `render`: scrollable body plus one-line footer hint.
    let chunks = Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(area);
    let body = chunks[0];
    let foot = chunks[1];
    // Footer hint row always opens the logs.
    if !foot.is_empty()
        && y >= foot.y
        && y < foot.y.saturating_add(foot.height)
        && x >= foot.x
        && x < foot.x.saturating_add(foot.width)
    {
        return true;
    }
    // Last content row inside the bordered body: one cell past the top
    // border, one row above the bottom border, inside the side borders.
    if body.is_empty() || body.height < 3 || body.width < 3 {
        return false;
    }
    let last_row = body.y.saturating_add(body.height).saturating_sub(2);
    if y != last_row {
        return false;
    }
    x > body.x && x < body.x.saturating_add(body.width).saturating_sub(1)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::seed_world;
    use crate::ids::WorkerId;

    fn view() -> Rect {
        Rect::new(0, 0, 80, 24)
    }

    fn split(view: Rect) -> (Rect, Rect) {
        let chunks =
            Layout::vertical([Constraint::Min(1), Constraint::Length(1)]).split(view);
        (chunks[0], chunks[1])
    }

    #[test]
    fn raw_logs_line_and_footer_hit() {
        let world = seed_world();
        assert!(world.workers.contains_key(&WorkerId::new("worker-8k")));
        let view = view();
        let (body, foot) = split(view);
        // Footer hint row opens the logs.
        assert!(raw_logs_hit(view, foot.x + 2, foot.y));
        // Last body line (the RAW LOGS row when the body fits) opens logs.
        let last_row = body.y.saturating_add(body.height).saturating_sub(2);
        assert!(raw_logs_hit(view, body.x + 2, last_row));
    }

    #[test]
    fn other_body_rows_and_outside_miss() {
        let view = view();
        let (body, _) = split(view);
        // First content row is detail text, not the logs link.
        assert!(!raw_logs_hit(view, body.x + 2, body.y + 1));
        // Body borders never hit.
        assert!(!raw_logs_hit(view, body.x, body.y + 1));
        assert!(!raw_logs_hit(view, body.x + 2, body.y));
        // Outside the view misses, never panics.
        assert!(!raw_logs_hit(view, 90, 2));
        assert!(!raw_logs_hit(view, 2, 30));
    }

    #[test]
    fn empty_and_tiny_screens_never_panic() {
        assert!(!raw_logs_hit(Rect::new(0, 0, 0, 0), 0, 0));
        for (w, h) in [(80, 24), (40, 10), (20, 6), (8, 4), (1, 1)] {
            let tiny = Rect::new(0, 0, w, h);
            let _ = raw_logs_hit(tiny, 1, 1);
            let _ = raw_logs_hit(tiny, 0, 0);
        }
        // Unknown worker ids still render the same geometry: footer hits.
        let view = view();
        let (_, foot) = split(view);
        assert!(raw_logs_hit(view, foot.x + 1, foot.y));
    }
}
