mod adapter;
mod app;
mod live;
mod ui_market;
mod ui_research;
mod ui_shell;
mod workspace;

use agent_ui_ttfx::Animation;

use crate::app::{App, Focus, research_split};
use crate::ui_market::{TransientTitle, status_pulse_glyph};
use crate::ui_research::render_research;
use crate::ui_shell::{render_chat, render_sidebar, root_split};
use crate::workspace::WorkspaceMode;
use crossterm::event::{self, Event, MouseButton, MouseEventKind};
use ratatui::{
    Frame,
    layout::{Constraint, Layout, Rect},
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
    let mut app = App::new();
    loop {
        // One deterministic pump per 50ms frame; every tick redraws.
        app.tick();
        terminal.draw(|frame: &mut Frame| render(frame, &app))?;
        if crossterm::event::poll(std::time::Duration::from_millis(50))? {
            match event::read()? {
                Event::Key(key) => {
                    if app.on_key(key.code, key.modifiers) {
                        break;
                    }
                }
                Event::Mouse(mouse) => {
                    if mouse.kind == MouseEventKind::Down(MouseButton::Left) {
                        if let Ok((cols, rows)) = crossterm::terminal::size() {
                            app.clicked(mouse.column, mouse.row, cols, rows);
                        }
                    }
                }
                _ => {}
            }
        }
    }
    Ok(())
}

fn render(frame: &mut Frame, app: &App) {
    // Splash owns the whole screen until done or skipped.
    if !app.startup.done() {
        app.startup.render(frame, frame.area());
        return;
    }
    let chunks = Layout::vertical([
        Constraint::Length(1),
        Constraint::Min(0),
        Constraint::Length(1),
    ])
    .split(frame.area());
    let (header_area, body, footer_area) = (chunks[0], chunks[1], chunks[2]);
    let (sidebar_area, workspace_area) = root_split(body);

    let ws = app.current();
    let mode = ws.mode();
    let titles: Vec<(String, bool, bool)> = app
        .workspaces()
        .iter()
        .map(|w| (w.title.clone(), w.research.is_some(), false))
        .collect();
    render_sidebar(frame, sidebar_area, &titles, app.selected(), app.sidebar_scroll);

    match mode {
        WorkspaceMode::Chat => {
            render_chat(frame, workspace_area, &ws.chat);
        }
        WorkspaceMode::Research => {
            let (top, bottom) = research_split(workspace_area);
            if let Some(research) = ws.research.as_ref() {
                render_research(frame, top, research, &ws.view);
            }
            render_chat(frame, bottom, &ws.chat);
            // Transient decrypt title over the TOP region's first line.
            if app.research_age < 15 && top.height > 0 && top.width > 0 {
                let overlay = Rect::new(top.x, top.y, top.width, 1);
                let mut title = TransientTitle::new("RESEARCH STARTING");
                title.resize(overlay.width.max(1), overlay.height.max(1));
                for _ in 0..app.research_age {
                    title.tick();
                }
                title.render(overlay, frame.buffer_mut());
            }
        }
    }

    let tick = ws
        .research
        .as_ref()
        .map(|r| r.tick as usize)
        .unwrap_or(app.research_age as usize);
    let header = Paragraph::new(format!(
        "{} {} -- {:?}/{:?}",
        status_pulse_glyph(tick),
        ws.title,
        mode,
        app.focus,
    ));
    frame.render_widget(header, header_area);
    let footer = Paragraph::new(match app.focus {
        Focus::Sidebar => "Up/Down select workspace - + new - Tab pane - Enter chat - q quit",
        Focus::Research => {
            "Enter drill-in - Esc back - Up/Down select - Tab pane - / chat - q quit"
        }
        Focus::Chat => {
            "type message - Enter send (/research ...) - Tab pane - Esc sidebar - q quit"
        }
    });
    frame.render_widget(footer, footer_area);
}
