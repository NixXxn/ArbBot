use crossterm::{
    event::{self, Event, KeyCode},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::symbols::{self, Marker};
use ratatui::{
    Terminal,
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{
        Axis, Block, Borders, Chart, Dataset, Gauge, GraphType, List, ListItem, Paragraph,Bar,BarChart, Widget,
        Sparkline,
    },
};

use crate::app::App;

pub fn ui(f: &mut ratatui::Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .margin(1)
        .constraints(
            [
                Constraint::Length(3),
                Constraint::Length(3),
                Constraint::Min(10),
                Constraint::Length(10),
                Constraint::Length(1),
            ]
            .as_ref(),
        )
        .split(f.size());

    // Title
    let title = Paragraph::new(format!(" 🎯 Polymarket Sniper | {}", app.market_question))
        .block(Block::default().borders(Borders::ALL).title(" Market "));
    f.render_widget(title, chunks[0]);

    // Timer Gauge
    let timer_color = if app.secs_left < 30 {
        Color::Red
    } else if app.secs_left < 60 {
        Color::Yellow
    } else {
        Color::Green
    };
    let progress = if app.secs_left > 0 {
        (app.secs_left as f64 / 300.0).min(1.0)
    } else {
        0.0
    };
    let label = format!("{}s remaining", app.secs_left);
    let gauge = Gauge::default()
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Expiry Countdown "),
        )
        .gauge_style(
            Style::default()
                .fg(timer_color)
                .add_modifier(Modifier::BOLD),
        )
        .ratio(progress)
        .label(label);
    f.render_widget(gauge, chunks[1]);

    // Middle row: Sparkline and Momentum
    let mid_chunks = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(70), Constraint::Percentage(30)].as_ref())
        .split(chunks[2]);

    let points: Vec<(f64, f64)> = app
        .price_history
        .iter()
        .enumerate()
        .map(|(i, p)| (i as f64, *p as f64))
        .collect();

    let dataset = Dataset::default()
        .name("Price")
        .marker(Marker::Braille)
        .graph_type(GraphType::Line)
        .style(if app.momentum > 0.0 {
            Style::default().fg(Color::Green)
        } else {
            Style::default().fg(Color::Red)
        })
        .data(&points);

    let min_price = app.price_history.iter().min().copied().unwrap_or(0) as f64;
    let max_price = app.price_history.iter().max().copied().unwrap_or(100) as f64;

    let (min_price, max_price) = if min_price == max_price {
        (min_price - 1.0, max_price + 1.0)
    } else {
        (min_price - 1.0, max_price + 1.0)
    };

    let x_max = if points.is_empty() {
        1.0
    } else {
        points.len() as f64
    };

    let chart = Chart::new(vec![dataset])
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Live Price Chart "),
        )
        .x_axis(Axis::default().bounds([0.0, x_max]))
        .y_axis(Axis::default().bounds([min_price - 1.0, max_price + 1.0]));

    f.render_widget(chart, mid_chunks[0]);

    let mom_color = if app.momentum > 5.0 {
        Color::Green
    } else if app.momentum < -5.0 {
        Color::Red
    } else {
        Color::Gray
    };
    let momentum_para = Paragraph::new(vec![
        Line::from(vec![
            Span::raw("Price: "),
            Span::styled(
                format!("{:.0}¢", app.price * 100.0),
                Style::default().add_modifier(Modifier::BOLD),
            ),
        ]),
        Line::from(vec![
            Span::raw("Bid:   "),
            Span::styled(
                format!("{:.0}¢", app.bid * 100.0),
                Style::default().add_modifier(Modifier::BOLD),
            ),
        ]),
        Line::from(vec![
            Span::raw("Size:  "),
            Span::raw(format!("{:.0}", app.size)),
        ]),
        Line::from(vec![
            Span::raw("Mom:   "),
            Span::styled(
                format!("{:+.2}%", app.momentum),
                Style::default().fg(mom_color).add_modifier(Modifier::BOLD),
            ),
        ]),
    ])
    .block(Block::default().borders(Borders::ALL).title(" Stats "));
    f.render_widget(momentum_para, mid_chunks[1]);

    // Logs
    let log_items: Vec<ListItem> = app
        .logs
        .iter()
        .rev()
        .map(|l| ListItem::new(l.as_str()))
        .collect();
    let logs_list = List::new(log_items).block(
        Block::default()
            .borders(Borders::ALL)
            .title(" Sniper Signals "),
    );
    f.render_widget(logs_list, chunks[3]);

    // Status bar
    let status = Paragraph::new(format!(" Status: {} | Press 'q' to quit", app.status));
    f.render_widget(status, chunks[4]);
}
