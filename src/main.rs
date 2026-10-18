use std::fs::OpenOptions;
use std::io::{self, Write};
use std::path::Path;
use std::{
    sync::Arc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use futures_util::{SinkExt, StreamExt};
use reqwest::Client;
use serde::Deserialize;
use tokio::sync::Mutex;
use tokio::sync::broadcast;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

mod ui;
use ui::ui::ui;
mod app;
use app::App;
use app::modules::Market;
use app::modules::PriceUpdate;

use crossterm::{
    event::{self, Event, KeyCode},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{Terminal, backend::CrosstermBackend};

impl App {
    fn new() -> App {
        App {
            market_question: "Waiting for market...".to_string(),
            price_history: Vec::new(),
            momentum: 0.0,
            secs_left: 0,
            bid: 0.0,
            price: 0.0,
            size: 0.0,
            logs: Vec::new(),
            should_quit: false,
            status: "Initializing...".to_string(),
        }
    }

    fn add_log(&mut self, message: String) {
        self.logs.push(message);
        if self.logs.len() > 20 {
            self.logs.remove(0);
        }
    }
}

fn get_next_log_file() -> String {
    let mut i = 1;
    loop {
        let filename = format!("market_log{}.txt", i);
        if !Path::new(&filename).exists() {
            return filename;
        }
        i += 1;
    }
}

fn log_to_file(filename: &str, message: &str) {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    if let Ok(mut file) = OpenOptions::new().create(true).append(true).open(filename) {
        let _ = writeln!(file, "[{}] {}", now, message);
    }
}

fn get_current_btc_slug() -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    let rounded = (now / 300) * 300;
    format!("btc-updown-5m-{}", rounded)
}

fn get_market_end_time(slug: &str) -> u64 {
    let parts: Vec<&str> = slug.split('-').collect();
    if let Some(ts_str) = parts.last() {
        if let Ok(ts) = ts_str.parse::<u64>() {
            return ts + 300;
        }
    }
    0
}

fn seconds_remaining(end_time: u64) -> i64 {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs();
    end_time as i64 - now as i64
}

async fn monitor_market(app: Arc<Mutex<App>>, log_file: String) {
    let client = Client::new();
    let mut internal_history: Vec<f64> = Vec::new();

    loop {
        {
            let app = app.lock().await;
            if app.should_quit {
                break;
            }
        }

        let slug = get_current_btc_slug();
        {
            let mut app = app.lock().await;
            app.status = format!("Searching for slug: {}", slug);
        }

        let url = format!("https://gamma-api.polymarket.com/markets?slug={}", slug);
        let res = match client.get(url).send().await {
            Ok(r) => r.json::<Vec<Market>>().await.unwrap_or_default(),
            Err(_) => {
                tokio::time::sleep(Duration::from_secs(2)).await;
                continue;
            }
        };

        if res.is_empty() {
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        }

        let market = &res[0];
        let bid = market.best_bid.unwrap_or(0.0);
        let question = market.question.clone().unwrap_or_default();

        {
            let mut app = app.lock().await;
            app.market_question = question;
            app.bid = bid;
            app.status = "Market found, checking activity...".to_string();
        }

        if bid > 0.0 {
            let end_time = get_market_end_time(&slug);
            let ids_str = market.clob_token_ids.as_ref().unwrap();
            let ids: Vec<String> = serde_json::from_str(ids_str).unwrap();
            let yes_token = &ids[0];

            let ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
            let (ws_stream, _) = match connect_async(ws_url).await {
                Ok(s) => s,
                Err(e) => {
                    log_to_file(&log_file, &format!("WS Connect Error: {:?}", e));
                    tokio::time::sleep(Duration::from_secs(2)).await;
                    continue;
                }
            };

            let (mut write, mut read) = ws_stream.split();
            let sub_msg = serde_json::json!({
                "type": "market",
                "assets_ids": [yes_token]
            });

            if write
                .send(Message::Text(sub_msg.to_string().into()))
                .await
                .is_err()
            {
                continue;
            }

            {
                let mut app = app.lock().await;
                app.status = "Subscribed to live feed".to_string();
            }

            while let Some(msg) = read.next().await {
                if app.lock().await.should_quit {
                    break;
                }

                match msg {
                    Ok(Message::Text(text)) => {
                        if let Ok(ws_msg) = serde_json::from_str::<serde_json::Value>(&text) {
                            if ws_msg["event_type"] == "price_change" {
                                if let Some(changes) = ws_msg["price_changes"].as_array() {
                                    if let Some(change) = changes.first() {
                                        let asset_id = change["asset_id"].as_str().unwrap_or("");
                                        if asset_id != yes_token {
                                            continue;
                                        }

                                        let bid: f64 = change["best_bid"]
                                            .as_str()
                                            .unwrap_or("0")
                                            .parse()
                                            .unwrap_or(0.0);
                                        let price: f64 = change["price"]
                                            .as_str()
                                            .unwrap_or("0")
                                            .parse()
                                            .unwrap_or(0.0);
                                        let size: f64 = change["size"]
                                            .as_str()
                                            .unwrap_or("0")
                                            .parse()
                                            .unwrap_or(0.0);

                                        if internal_history.last() != Some(&bid) {
                                            internal_history.push(bid);
                                            if internal_history.len() > 50 {
                                                internal_history.remove(0);
                                            }
                                        }

                                        let momentum = if internal_history.len() >= 2 {
                                            let oldest = internal_history[0];
                                            let newest =
                                                internal_history[internal_history.len() - 1];
                                            if oldest > 0.0 {
                                                ((newest - oldest) / oldest) * 100.0
                                            } else {
                                                0.0
                                            }
                                        } else {
                                            0.0
                                        };

                                        let secs_left = seconds_remaining(end_time);

                                        {
                                            let mut app = app.lock().await;
                                            app.bid = bid;
                                            app.price = price;
                                            app.size = size;
                                            app.momentum = momentum;
                                            app.secs_left = secs_left;
                                            app.price_history = internal_history
                                                .iter()
                                                .map(|v| (v * 100.0) as u64)
                                                .collect();

                                            if secs_left > 0 && secs_left < 30 {
                                                if bid > 0.70 && momentum > 5.0 {
                                                    let msg = format!(
                                                        "🎯 SNIPER! BUY UP @ {}¢ | Mom: {:+.2}%",
                                                        (bid * 100.0) as u32,
                                                        momentum
                                                    );
                                                    app.add_log(msg.clone());
                                                    log_to_file(&log_file, &msg);
                                                } else if bid < 0.30 && momentum < -5.0 {
                                                    let msg = format!(
                                                        "🎯 SNIPER! BUY DOWN @ {}¢ | Mom: {:+.2}%",
                                                        ((1.0 - bid) * 100.0) as u32,
                                                        momentum
                                                    );
                                                    app.add_log(msg.clone());
                                                    log_to_file(&log_file, &msg);
                                                }
                                            }
                                        }

                                        if secs_left <= 0 {
                                            break;
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Ok(Message::Ping(data)) => {
                        let _ = write.send(Message::Pong(data)).await;
                    }
                    _ => {}
                }
            }
            internal_history.clear();
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

#[tokio::main]
async fn main() -> Result<(), io::Error> {
    let log_file = get_next_log_file();
    let app = Arc::new(Mutex::new(App::new()));

    // Setup terminal
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    // Start monitor task
    let monitor_app = app.clone();
    tokio::spawn(async move {
        monitor_market(monitor_app, log_file).await;
    });

    loop {
        terminal.draw(|f| {
            // Try to acquire the async mutex without blocking the sync UI thread.
            // If the lock is not available, render a minimal temporary UI snapshot.
            if let Ok(app_guard) = app.try_lock() {
                ui(f, &*app_guard);
            } else {
                let tmp = App::new();
                ui(f, &tmp);
            }
        })?;

        if event::poll(Duration::from_millis(100))? {
            if let Event::Key(key) = event::read()? {
                if key.code == KeyCode::Char('q') {
                    app.lock().await.should_quit = true;
                    break;
                }
            }
        }
    }

    // Restore terminal
    disable_raw_mode()?;
    execute!(terminal.backend_mut(), LeaveAlternateScreen)?;
    terminal.show_cursor()?;

    Ok(())
}
