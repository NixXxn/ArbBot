#[path = "../leadlag/market_worker.rs"]
mod market_worker;
#[path = "../leadlag/provider.rs"]
mod provider;
#[path = "../leadlag/quality.rs"]
mod quality;
#[path = "../leadlag/strategies.rs"]
mod strategies;
#[path = "../leadlag/types.rs"]
mod types;

use std::{
    collections::HashMap,
    fs::File,
    io::Write,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tokio::sync::mpsc;
use types::*;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<_> = std::env::args().collect();
    if args.iter().any(|a| a == "--live") {
        return Err("This research executable supports paper execution only".into());
    }
    let value = |key: &str, default: &str| {
        args.windows(2)
            .find(|p| p[0] == key)
            .map(|p| p[1].clone())
            .unwrap_or(default.into())
    };
    let duration: u64 = value("--seconds", "300").parse()?;
    let asset = value("--asset", "btc").to_lowercase();
    let paper_max_spend: f64 = value("--paper-max-spend", "10").parse()?;
    if !paper_max_spend.is_finite() || paper_max_spend <= 0.0 || paper_max_spend > 1_000_000.0 {
        return Err("--paper-max-spend must be between 0 and 1000000".into());
    }
    let signals_only = args.iter().any(|a| a == "--signals-only");
    let kraken_only = args.iter().any(|a| a == "--kraken-only");
    let trader_clone = args.iter().any(|a| a == "--trader-clone");
    let quality_mode = args.iter().any(|a| a == "--kraken-quality");
    let wallet_replica = args.iter().any(|a| a == "--wallet-replica");
    let trend_mode = args.iter().any(|a| a == "--kraken-trend");
    let up60 = args.iter().any(|a| a == "--up60");
    let down35 = args.iter().any(|a| a == "--down35");
    let clip5 = args.iter().any(|a| a == "--clip5");
    let clip15 = args.iter().any(|a| a == "--clip15");
    let early3m = args.iter().any(|a| a == "--early3m");
    let early3m5 = args.iter().any(|a| a == "--early3m5");
    if down35 && asset != "sol" {
        return Err("--down35 requires --asset sol".into());
    }
    if !matches!(asset.as_str(), "btc" | "eth") && !(down35 && asset == "sol") {
        return Err("--asset must be btc or eth; --down35 requires sol".into());
    }
    let calibrated_mode = quality_mode || wallet_replica || trend_mode;
    if asset != "btc" && !calibrated_mode && !up60 && !down35 {
        return Err("Non-BTC assets require a calibrated quality strategy".into());
    }
    if [kraken_only, trader_clone, quality_mode, wallet_replica, trend_mode, up60, down35, clip5, clip15, early3m, early3m5].iter().filter(|x| **x).count() > 1 {
        return Err("Choose one strategy flag".into());
    }
    let quality_config = if calibrated_mode {
        let config: quality::Config = serde_json::from_reader(File::open(value("--quality-config", "kraken-quality.json"))?)?;
        config.validate()?;
        if config.asset != asset {
            return Err("--asset and quality configuration asset do not match".into());
        }
        if (wallet_replica && config.version != 2) || (quality_mode && config.version != 1)
            || (trend_mode && config.version != 3) {
            return Err("Strategy and calibration version do not match".into());
        }
        Some(config)
    } else {None};
    let log_all_events = args.iter().any(|a| a == "--log-all-events");
    let journal = value("--journal", "leadlag.jsonl");
    let mut file = File::create(&journal)?;
    let (tx, mut rx) = mpsc::channel(65536);
    let mut tasks = Vec::new();
    let venues: Vec<Venue> = if kraken_only || trader_clone || calibrated_mode || up60 || down35 || clip5 || clip15 || early3m || early3m5 {
        vec![Venue::Kraken]
    } else {
        vec![Venue::Kraken, Venue::Deribit, Venue::Binance]
    };
    for venue in venues {
        tasks.push(tokio::spawn(market_worker::exchange(
            venue,
            tx.clone(),
            calibrated_mode || up60 || down35 || clip5 || clip15 || early3m || early3m5,
            asset.clone(),
        )));
    }
    let market_interval = quality_config.as_ref().map(|config| config.interval_seconds)
        .unwrap_or(if trader_clone || up60 || clip15 || early3m {900} else {300});
    tasks.push(tokio::spawn(market_worker::polymarket(
        tx.clone(),
        market_interval,
        trader_clone,
        asset.clone(),
        down35 || clip5 || clip15 || early3m || early3m5,
    )));
    drop(tx);
    let mut engine = if let Some(config) = quality_config {
        strategies::Engine::quality(config)
    } else if up60 {
        strategies::Engine::up60()
    } else if down35 {
        strategies::Engine::down35()
    } else if clip5 {
        strategies::Engine::clip5()
    } else if clip15 {
        strategies::Engine::clip15()
    } else if early3m {
        strategies::Engine::early3m()
    } else if early3m5 {
        strategies::Engine::early3m5()
    } else if trader_clone {
        strategies::Engine::trader_clone()
    } else if kraken_only {
        strategies::Engine::kraken_only()
    } else {
        strategies::Engine::default()
    };
    let mut provider = provider::PaperProvider::new(paper_max_spend);
    let mut counts: HashMap<String, usize> = HashMap::new();
    let started = Instant::now();
    let end = started + Duration::from_secs(duration);
    let mut timer = tokio::time::interval(Duration::from_millis(5));
    let mut health_timer = tokio::time::interval(Duration::from_secs(60));
    // Tokio intervals tick immediately once; consume that tick so the first
    // health record reflects completed startup/bootstrap work.
    health_timer.tick().await;
    println!(
        "Paper research session: {duration}s; strategy={}",
        if down35 {"sol_down35_5m"} else if clip5 {"clip_btc_5m"} else if clip15 {"clip_btc_15m"} else if early3m5 {"early3m_btc_5m"} else if early3m {"early3m_btc_15m"} else if up60 {"up60_15m"} else if trend_mode {"kraken_trend_15m"} else if wallet_replica { "kraken_wallet_replica" } else if quality_mode { "kraken_quality" } else if trader_clone { "preopen_btc_15m" } else if kraken_only { "kraken_only" } else { "three_exchange_confirmation" }
    );
    loop {
        tokio::select! {
            _ = tokio::signal::ctrl_c() => break,
            _ = tokio::time::sleep_until(end.into()) => break,
            _ = health_timer.tick() => {
                if file.metadata()?.len() >= 8*1024*1024 {
                    let previous = format!("{journal}.1");
                    if std::path::Path::new(&previous).exists() {
                        std::fs::rename(&previous, format!("{journal}.2"))?;
                    }
                    std::fs::rename(&journal, &previous)?;
                    file = File::create(&journal)?;
                }
                let health = serde_json::json!({"type":"health","received_ms":clock_ms(),
                    "asset":asset,
                    "events":counts,"signals":engine.confirmed,"paper_fills":provider.fills,
                    "paper_spend":provider.spent,"quality":engine.quality_health()});
                writeln!(file,"{health}")?;
                if signals_only || calibrated_mode || up60 || down35 || clip5 || clip15 || early3m || early3m5 || trader_clone {println!("{health}");}
            }
            _ = timer.tick() => {
                let now = mono_ms();
                engine.expire(now);
                if let Some(fill) = provider.advance(now, &engine.quotes) {
                    writeln!(file, "{}", serde_json::to_string(&fill)?)?;
                    println!("{fill}");
                }
            }
            event = rx.recv() => {
                let Some(event) = event else { break; };
                *counts.entry(event.kind()).or_default() += 1;
                if (!signals_only && !calibrated_mode) || log_all_events || matches!(&event, Event::Status {..} | Event::Reset {..}) {
                    writeln!(file, "{}", serde_json::to_string(&event)?)?;
                }
                if let Some(signal) = engine.on_event(event) {
                    writeln!(file, "{}", serde_json::to_string(&signal)?)?;
                    if signals_only {
                        println!("{}", serde_json::json!({"type":"signal", "signal":signal}));
                    } else {
                        provider.submit(signal);
                    }
                }
            }
        }
    }
    for task in tasks {
        task.abort();
    }
    let summary = serde_json::json!({"type":"summary", "events":counts,
        "confirmed_moves":engine.confirmed, "expired_candidates":engine.expired,
        "paper_fills":provider.fills, "paper_spend":provider.spent,
        "quality":engine.quality_health(),
        "fee_reserve":provider.reserve, "requested_seconds":duration, "elapsed_seconds":started.elapsed().as_secs_f64(),
        "mode":if signals_only {"signal_monitor"} else {"paper"},
        "asset":asset,
        "strategy":if down35 {"sol_down35_5m"} else if clip5 {"clip_btc_5m"} else if clip15 {"clip_btc_15m"} else if early3m5 {"early3m_btc_5m"} else if early3m {"early3m_btc_15m"} else if up60 {"up60_15m"} else if trend_mode {"kraken_trend_15m"} else if wallet_replica {"kraken_wallet_replica"} else if quality_mode {"kraken_quality"} else if trader_clone {"preopen_btc_15m"} else if kraken_only {"kraken_only"} else {"three_exchange_confirmation"},
        "live_orders":0, "profitability_established":false});
    writeln!(file, "{summary}")?;
    println!("{summary}");
    Ok(())
}

pub fn clock_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
}
pub fn mono_ms() -> u64 {
    static START: std::sync::OnceLock<Instant> = std::sync::OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_millis() as u64
}
