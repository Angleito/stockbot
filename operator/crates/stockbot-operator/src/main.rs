mod adapter;
mod app;
mod live;

use agent_ui::{AgentEvent, Selected, View, WorldState, ids::AgentId};
use agent_ui::views::{agent, worker, world};
use crate::app::App;
use crossterm::event::{self, Event, KeyCode, KeyModifiers, MouseButton, MouseEventKind};
use ratatui::{
    Frame,
    layout::{Constraint, Direction, Layout, Rect},
    widgets::Paragraph,
};

fn main() -> std::io::Result<()> {
    let mut terminal = ratatui::init();
    crossterm::execute!(
        std::io::stdout(),
        crossterm::event::EnableMouseCapture
    )?;
    let result = run(&mut terminal);
    let _ = crossterm::execute!(
        std::io::stdout(),
        crossterm::event::DisableMouseCapture
    );
    ratatui::restore();
    result
}

fn run(terminal: &mut ratatui::DefaultTerminal) -> std::io::Result<()> {
    // Live world: starts empty, fills from the typed runtime feed without
    // manual refresh (adapt -> apply per tick, no text parsing).
    let mut app = App::new(WorldState::new());
    // Keep the SEC highlight for keyboard drill-down once the feed lands it.
    app.apply(AgentEvent::SelectionChanged(Some(Selected::Agent(
        AgentId::new("agent-sec"),
    ))));
    let mut feed = live::LiveFeed::from_fake();
    loop {
        // One live event per tick; every tick redraws so updates land now.
        feed.pump_one(&mut app);
        terminal.draw(|frame: &mut Frame| render(frame, &app))?;
        if crossterm::event::poll(std::time::Duration::from_millis(50))? {
            match event::read()? {
                Event::Key(key) => match key.code {
                    KeyCode::Char('q') => break,
                    KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => break,
                    code => app.on_key(code),
                },
                Event::Mouse(mouse) => {
                    if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                        mouse_click(&mut app, mouse.column, mouse.row);
                    }
                }
                _ => {}
            }
        }
    }
    Ok(())
}
/// Left-click dispatch: hit-test the current view's area and funnel through
/// the same `clicked_*` intent API as the widgets wave. Unknown/empty clicks
/// are ignored; other buttons never reach here. Keyboard is untouched.
fn mouse_click(app: &mut App, column: u16, row: u16) {
    let Ok((cols, rows)) = crossterm::terminal::size() else {
        return;
    };
    // Same shell split as `render`: header 1, view fill, footer 1.
    let view_area = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ])
        .split(Rect::new(0, 0, cols, rows))[1];
    match app.view().clone() {
        View::World => {
            let hit = world::agent_at(view_area, app.world(), column, row);
            if let Some(id) = hit {
                app.clicked_agent(&id);
            }
        }
        View::Agent(id) => {
            let hit = agent::worker_at(view_area, app.world(), &id, column, row);
            if let Some(wid) = hit {
                app.clicked_worker(&wid);
            }
        }
        View::Worker(id) => {
            if worker::raw_logs_hit(view_area, column, row) {
                app.clicked_raw_logs(&id);
            }
        }
        View::Logs(_) => {}
    }
}

fn render(frame: &mut Frame, app: &App) {
    let world = app.world();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(0),
            Constraint::Length(1),
        ])
        .split(frame.area());
    let header = Paragraph::new(format!(
        "stockbot-operator -- {:?} (agents: {}, workers: {})",
        app.view(),
        world.agents.len(),
        world.workers.len()
    ));
    match app.view() {
        View::World => world::render_world(frame, chunks[1], world),
        View::Agent(id) => agent::render(frame, chunks[1], world, &id),
        View::Worker(id) => worker::render(frame, chunks[1], world, &id),
        View::Logs(id) => {
            let lines = live::raw_logs(world, &id);
            frame.render_widget(Paragraph::new(lines.join("\n")), chunks[1]);
        }
    }
    let footer =
        Paragraph::new("Enter drill-in - Esc back - Up/Down select - click select/open - q quit");
    frame.render_widget(header, chunks[0]);
    frame.render_widget(footer, chunks[2]);
}
