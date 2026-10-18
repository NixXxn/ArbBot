use super::types::*;
use std::collections::{HashMap, VecDeque};

struct Candidate {
    direction: f64,
    start: u64,
    baselines: HashMap<Venue, f64>,
    followers: HashMap<Venue, u64>,
}
#[derive(Default)]
pub struct Engine {
    history: HashMap<Venue, VecDeque<Tick>>,
    candidate: Option<Candidate>,
    kraken_only: bool,
    trader_clone: bool,
    up60: bool,
    down35: bool,
    clip5: bool,
    clip15: bool,
    early3m: bool,
    early3m5: bool,
    quality: Option<super::quality::QualityEngine>,
    trader_interval_start: Option<u64>,
    trader_interval_open: Option<f64>,
    trader_emitted_for: Option<u64>,
    up60_emitted_for: Option<String>,
    down35_emitted_for: Option<String>,
    clip_emitted: Vec<String>,
    early3m_emitted: Vec<String>,
    kraken_history: Vec<(u64, f64)>,
    edge_interval_start: Option<u64>,
    edge_interval_open: Option<f64>,
    edge_last_tick: Option<Tick>,
    edge_reason: &'static str,
    edge_last_score: Option<serde_json::Value>,
    pub quotes: HashMap<String, Quote>,
    pub confirmed: usize,
    pub expired: usize,
}
impl Engine {
    pub fn quality(config: super::quality::Config) -> Self {
        Self {quality: Some(super::quality::QualityEngine::new(config)), ..Self::default()}
    }
    pub fn quality_health(&self) -> Option<serde_json::Value> {
        self.quality.as_ref().map(|quality| quality.health()).or_else(|| {
            (self.up60 || self.down35 || self.trader_clone || self.clip5 || self.clip15 || self.early3m || self.early3m5).then(|| serde_json::json!({
                "reason": self.edge_reason,
                "history_source": if self.trader_clone {
                    if self.trader_interval_open.is_some() {"live_interval_open"} else {"waiting_for_interval_open"}
                } else if self.kraken_history.is_empty() {"warming_up"} else {"kraken_rest_1m"},
                "has_interval_open": if self.trader_clone {self.trader_interval_open.is_some()} else {self.edge_interval_open.is_some()},
                "last_score": self.edge_last_score,
                "confirmation": if self.up60 {"Kraken +7.5bp; BTC ask 0.58-0.62"}
                    else if self.trader_clone {"Follow BTC ≥15bp; ask 0.49-0.51; T-200s"}
                    else if self.clip5 {"BTC 5m ask 0.62-0.65 + Kraken ≥5bp"}
                    else if self.clip15 {"BTC 15m ask 0.55-0.65 + Kraken ≥7.5bp; after 5m"}
                    else if self.early3m5 {"BTC 5m: 90s Kraken observe; ≥6bp ask≤0.62 or dual-leg sum<0.99"}
                    else if self.early3m {"BTC 15m: 3m Kraken observe; ≥8bp ask≤0.62 or dual-leg sum<0.99"}
                    else {"Kraken -10bp; ask <=0.32"}
            }))
        })
    }
    pub fn kraken_only() -> Self {
        Self {
            kraken_only: true,
            ..Self::default()
        }
    }

    pub fn trader_clone() -> Self {
        Self {
            trader_clone: true,
            ..Self::default()
        }
    }

    pub fn up60() -> Self {
        Self {up60:true,..Self::default()}
    }

    pub fn down35() -> Self {
        Self {down35:true,..Self::default()}
    }

    pub fn clip5() -> Self {
        Self {clip5:true,..Self::default()}
    }

    pub fn clip15() -> Self {
        Self {clip15:true,..Self::default()}
    }

    pub fn early3m() -> Self {
        Self {early3m:true,..Self::default()}
    }

    pub fn early3m5() -> Self {
        Self {early3m5:true,..Self::default()}
    }

    fn down35_quote(&mut self, quote: &Quote) -> Option<Signal> {
        let Some(tick)=self.edge_last_tick.as_ref() else {self.edge_reason="waiting_for_kraken";return None;};
        let Some(open)=self.edge_interval_open else {self.edge_reason="missing_interval_open";return None;};
        let move_bp=(tick.mid()/open-1.0)*10000.0;
        self.edge_last_score=Some(serde_json::json!({"move_bp":move_bp,"probability":0.50,
            "ask":quote.ask,"required_move_bp":-10.0,"max_ask":0.32}));
        if tick.mono_ms.abs_diff(quote.mono_ms)>500 {self.edge_reason="stale_kraken_confirmation";return None;}
        if move_bp > -10.0 {self.edge_reason="insufficient_down_momentum";return None;}
        if quote.outcome != "Down" || quote.ask > 0.32 || quote.ask <= 0.0
            || !quote.slug.starts_with("sol-updown-5m-")
            || ![quote.bid,quote.ask,quote.ask_size,quote.min_size].iter().all(|v|v.is_finite())
            || quote.bid <= 0.0 || quote.ask < quote.bid || quote.ask_size < quote.min_size
            || quote.received_ms.saturating_sub(quote.exchange_ms)>500
            || quote.end_ms <= quote.received_ms
            || self.down35_emitted_for.as_deref()==Some(&quote.slug) {
            return None;
        }
        self.edge_reason="signal";
        self.down35_emitted_for=Some(quote.slug.clone());self.confirmed+=1;
        Some(Signal {token:quote.token.clone(),slug:quote.slug.clone(),outcome:"Down".into(),
            ask:quote.ask,mono_ms:quote.mono_ms,trigger_ms:quote.mono_ms,
            received_ms:quote.received_ms,end_ms:quote.end_ms,execution_delay_ms:0,
            quality:Some(SignalQuality {probability_lower:0.50,move_bp,z:0.0,
                fee_rate_ceiling:0.0,min_edge:0.10,suggested_budget:None}),
            scenario:None,legs:None})
    }

    fn up60_quote(&mut self, quote: &Quote) -> Option<Signal> {
        // Live book: exact ask 0.60 + 10bp almost never cleared one-entry-per-market
        // against clip15. Widen to 0.58-0.62 with +7.5bp so momentum can print first.
        let Some(tick)=self.edge_last_tick.as_ref() else {self.edge_reason="waiting_for_kraken";return None;};
        let Some(open)=self.edge_interval_open else {self.edge_reason="missing_interval_open";return None;};
        let move_bp=(tick.mid()/open-1.0)*10000.0;
        self.edge_last_score=Some(serde_json::json!({"move_bp":move_bp,"probability":0.69,
            "ask":quote.ask,"required_move_bp":7.5,"min_ask":0.58,"max_ask":0.62}));
        if tick.mono_ms.abs_diff(quote.mono_ms)>500 {self.edge_reason="stale_kraken_confirmation";return None;}
        if move_bp < 7.5 {self.edge_reason="insufficient_up_momentum";return None;}
        if quote.outcome != "Up" || quote.ask < 0.58 || quote.ask > 0.62
            || !quote.slug.starts_with("btc-updown-15m-")
            || !quote.liquid_and_fresh(quote.mono_ms)
            || self.up60_emitted_for.as_deref() == Some(&quote.slug) {
            return None;
        }
        self.edge_reason="signal";
        self.up60_emitted_for=Some(quote.slug.clone());
        self.confirmed+=1;
        Some(Signal {token:quote.token.clone(),slug:quote.slug.clone(),outcome:"Up".into(),
            ask:quote.ask,mono_ms:quote.mono_ms,trigger_ms:quote.mono_ms,
            received_ms:quote.received_ms,end_ms:quote.end_ms,execution_delay_ms:200,
            quality:Some(SignalQuality {probability_lower:0.69,move_bp,z:0.0,
                fee_rate_ceiling:0.10,min_edge:0.015,suggested_budget:None}),
            scenario:None,legs:None})
    }

    fn track_kraken_interval(&mut self, tick: &Tick, interval: u64) -> bool {
        if tick.venue != Venue::Kraken || !tick.valid() {self.edge_reason="invalid_kraken_tick";return false;}
        if (tick.ask-tick.bid)/tick.mid()*10000.0 > 2.0 {self.edge_reason="kraken_spread_too_wide";return false;}
        let seconds=tick.received_ms/1000;
        let start=seconds/interval*interval;
        if self.edge_interval_start!=Some(start) {
            self.edge_interval_start=Some(start);
            self.edge_interval_open=(seconds-start<=2).then_some(tick.mid()).or_else(|| {
                self.kraken_history.iter().find(|row| row.0+60==start).map(|row| row.1)
            });
        }
        self.edge_last_tick=Some(tick.clone());
        if self.edge_interval_open.is_none() {self.edge_reason="missing_interval_open";return false;}
        true
    }

    fn edge_tick(&mut self, tick: Tick) -> Option<Signal> {
        let interval=if self.up60 || self.clip15 || self.early3m {900} else {300};
        if !self.track_kraken_interval(&tick, interval) {return None;}
        if self.clip5 || self.clip15 {
            self.edge_reason="waiting_for_clip_quote";
            return None;
        }
        if self.early3m || self.early3m5 {
            self.edge_reason="waiting_for_early3m_quote";
            return None;
        }
        self.edge_reason="waiting_for_price_and_momentum";
        let start=tick.received_ms/1000/interval*interval;
        let slug=format!("{}-updown-{}m-{start}",if self.down35 {"sol"} else {
            self.quotes.values().next().and_then(|q| q.slug.split('-').next()).unwrap_or("btc")
        },interval/60);
        let outcome=if self.up60 {"Up"} else {"Down"};
        let quote=self.quotes.values().find(|q| q.slug==slug && q.outcome==outcome).cloned()?;
        if self.up60 {self.up60_quote(&quote)} else {self.down35_quote(&quote)}
    }

    fn clip_quote(&mut self, quote: &Quote) -> Option<Signal> {
        let interval = if self.clip5 { 300 } else { 900 };
        let prefix = if self.clip5 { "btc-updown-5m-" } else { "btc-updown-15m-" };
        if !quote.slug.starts_with(prefix) {
            self.edge_reason = "wrong_market";
            return None;
        }
        let Ok(start) = quote.slug.rsplit('-').next().unwrap_or("").parse::<u64>() else {
            self.edge_reason = "invalid_slug";
            return None;
        };
        let seconds = quote.received_ms / 1000;
        let elapsed = seconds.saturating_sub(start);
        let remaining = (start + interval).saturating_sub(seconds);
        let Some(tick) = self.edge_last_tick.as_ref() else {
            self.edge_reason = "waiting_for_kraken";
            return None;
        };
        let Some(open) = self.edge_interval_open else {
            self.edge_reason = "missing_interval_open";
            return None;
        };
        let move_bp = (tick.mid() / open - 1.0) * 10000.0;
        let required_move = if self.clip5 { 5.0 } else { 7.5 };
        self.edge_last_score = Some(serde_json::json!({
            "ask": quote.ask, "elapsed_s": elapsed, "remaining_s": remaining,
            "move_bp": move_bp, "required_move_bp": required_move,
            "min_ask": if self.clip5 {0.62} else {0.55},
            "max_ask": 0.65
        }));
        if quote.end_ms != (start + interval) * 1000 {
            self.edge_reason = "end_mismatch";
            return None;
        }
        // 5m favorites need directional confirmation; bare price bands lost after fees.
        // 15m waits 5 minutes so up60/preopen/kraken can claim the market first.
        let min_elapsed = if self.clip5 { 30 } else { 300 };
        let min_remaining = if self.clip5 { 60 } else { 90 };
        if elapsed < min_elapsed || remaining < min_remaining {
            self.edge_reason = "outside_clip_window";
            return None;
        }
        let in_band = if self.clip5 {
            quote.ask >= 0.62 && quote.ask <= 0.65
        } else {
            quote.ask >= 0.55 && quote.ask <= 0.65
        };
        if !in_band {
            self.edge_reason = "outside_price_band";
            return None;
        }
        let confirmed = if quote.outcome == "Up" { move_bp >= required_move } else { move_bp <= -required_move };
        if !confirmed {
            self.edge_reason = "insufficient_clip_momentum";
            return None;
        }
        if tick.mono_ms.abs_diff(quote.mono_ms) > 500 {
            self.edge_reason = "stale_kraken_confirmation";
            return None;
        }
        if !quote.liquid_and_fresh(quote.mono_ms) {
            self.edge_reason = "quote_not_tradable";
            return None;
        }
        if self.clip_emitted.iter().any(|slug| slug == &quote.slug) {
            self.edge_reason = "already_signalled";
            return None;
        }
        self.clip_emitted.push(quote.slug.clone());
        if self.clip_emitted.len() > 64 {
            self.clip_emitted.remove(0);
        }
        self.edge_reason = "signal";
        self.confirmed += 1;
        Some(Signal {
            token: quote.token.clone(),
            slug: quote.slug.clone(),
            outcome: quote.outcome.clone(),
            ask: quote.ask,
            mono_ms: quote.mono_ms,
            trigger_ms: quote.mono_ms,
            received_ms: quote.received_ms,
            end_ms: quote.end_ms,
            execution_delay_ms: 0,
            quality: Some(SignalQuality {
                probability_lower: if quote.ask >= 0.55 { 0.70 } else { 0.58 },
                move_bp,
                z: 0.0,
                fee_rate_ceiling: 0.02,
                min_edge: 0.0,
                suggested_budget: Some(5.0),
            }),
            scenario: None,
            legs: None,
        })
    }

    fn early3m_quote(&mut self, quote: &Quote) -> Option<Signal> {
        let interval = if self.early3m5 { 300 } else { 900 };
        let prefix = if self.early3m5 { "btc-updown-5m-" } else { "btc-updown-15m-" };
        // 5m cannot afford a full 3-minute observe; use 90s so ~2.5m remain to trade.
        let observe_s = if self.early3m5 { 90 } else { 180 };
        let min_remaining = if self.early3m5 { 60 } else { 120 };
        let directional_bp = if self.early3m5 { 6.0 } else { 8.0 };
        // Logs: directional asks ≥0.73 were almost all losers; cap favorites.
        const MAX_DIRECTIONAL_ASK: f64 = 0.62;
        const MAX_PAIR_ASK: f64 = 0.99;
        const FEE_RATE: f64 = 0.02;
        if !quote.slug.starts_with(prefix) {
            self.edge_reason = "wrong_market";
            return None;
        }
        let Ok(start) = quote.slug.rsplit('-').next().unwrap_or("").parse::<u64>() else {
            self.edge_reason = "invalid_slug";
            return None;
        };
        let seconds = quote.received_ms / 1000;
        let elapsed = seconds.saturating_sub(start);
        let remaining = (start + interval).saturating_sub(seconds);
        let Some(tick) = self.edge_last_tick.as_ref() else {
            self.edge_reason = "waiting_for_kraken";
            return None;
        };
        let Some(open) = self.edge_interval_open else {
            self.edge_reason = "missing_interval_open";
            return None;
        };
        let move_bp = (tick.mid() / open - 1.0) * 10000.0;
        self.edge_last_score = Some(serde_json::json!({
            "ask": quote.ask, "elapsed_s": elapsed, "remaining_s": remaining,
            "move_bp": move_bp, "directional_bp": directional_bp,
            "max_pair_ask": MAX_PAIR_ASK, "max_directional_ask": MAX_DIRECTIONAL_ASK,
            "observe_s": observe_s
        }));
        if quote.end_ms != (start + interval) * 1000 {
            self.edge_reason = "end_mismatch";
            return None;
        }
        if elapsed < observe_s || remaining < min_remaining {
            self.edge_reason = "outside_early3m_window";
            return None;
        }
        if tick.mono_ms.abs_diff(quote.mono_ms) > 500 {
            self.edge_reason = "stale_kraken_confirmation";
            return None;
        }
        if self.early3m_emitted.iter().any(|slug| slug == &quote.slug) {
            self.edge_reason = "already_signalled";
            return None;
        }
        let up = self.quotes.values().find(|q| q.slug == quote.slug && q.outcome == "Up").cloned();
        let down = self.quotes.values().find(|q| q.slug == quote.slug && q.outcome == "Down").cloned();
        let tradable = |q: &Quote| {
            [q.bid, q.ask, q.ask_size, q.min_size].iter().all(|v| v.is_finite())
                && q.min_size > 0.0
                && q.bid > 0.0
                && q.ask >= q.bid
                && q.ask - q.bid <= 0.02 + 1e-9
                && q.ask >= 0.1
                && q.ask <= 0.9
                && q.ask_size >= q.min_size
                && q.received_ms.saturating_sub(q.exchange_ms) <= 500
                && tick.mono_ms.abs_diff(q.mono_ms) <= 500
        };

        // Scenario A: clear Kraken direction at a still-cheap ask. Expensive favorites
        // fall through to Scenario B when a dual-leg arb is available.
        if move_bp.abs() >= directional_bp {
            let side = if move_bp > 0.0 { up.as_ref() } else { down.as_ref() };
            if let Some(side) = side.filter(|q| tradable(q) && q.ask <= MAX_DIRECTIONAL_ASK) {
                self.early3m_emitted.push(quote.slug.clone());
                if self.early3m_emitted.len() > 64 {
                    self.early3m_emitted.remove(0);
                }
                self.edge_reason = "signal";
                self.confirmed += 1;
                return Some(Signal {
                    token: side.token.clone(),
                    slug: side.slug.clone(),
                    outcome: side.outcome.clone(),
                    ask: side.ask,
                    mono_ms: side.mono_ms,
                    trigger_ms: side.mono_ms,
                    received_ms: side.received_ms,
                    end_ms: side.end_ms,
                    execution_delay_ms: 0,
                    quality: Some(SignalQuality {
                        probability_lower: 0.72,
                        move_bp,
                        z: 0.0,
                        fee_rate_ceiling: FEE_RATE,
                        min_edge: 0.0,
                        suggested_budget: Some(5.0),
                    }),
                    scenario: Some("directional".into()),
                    legs: None,
                });
            }
        }

        // Scenario B: dual-leg arb when ask_up + ask_down leave fee room under $1.
        let (Some(up), Some(down)) = (up, down) else {
            self.edge_reason = "waiting_for_both_quotes";
            return None;
        };
        if !tradable(&up) || !tradable(&down) {
            self.edge_reason = "pair_not_tradable";
            return None;
        }
        let pair = up.ask + down.ask;
        if pair >= MAX_PAIR_ASK || pair * (1.0 + FEE_RATE) >= 1.0 {
            self.edge_reason = "pair_not_arbable";
            return None;
        }
        // Buy the Kraken-riskier side first (more likely to reprice higher).
        let (primary, hedge) = if move_bp >= 0.0 { (up, down) } else { (down, up) };
        let legs = vec![
            SignalLeg {
                token: primary.token.clone(),
                outcome: primary.outcome.clone(),
                ask: primary.ask,
                role: "primary".into(),
            },
            SignalLeg {
                token: hedge.token.clone(),
                outcome: hedge.outcome.clone(),
                ask: hedge.ask,
                role: "hedge".into(),
            },
        ];
        self.early3m_emitted.push(quote.slug.clone());
        if self.early3m_emitted.len() > 64 {
            self.early3m_emitted.remove(0);
        }
        self.edge_reason = "signal";
        self.confirmed += 1;
        Some(Signal {
            token: primary.token.clone(),
            slug: primary.slug.clone(),
            outcome: primary.outcome.clone(),
            ask: primary.ask,
            mono_ms: primary.mono_ms.max(hedge.mono_ms),
            trigger_ms: primary.mono_ms.max(hedge.mono_ms),
            received_ms: primary.received_ms.max(hedge.received_ms),
            end_ms: primary.end_ms,
            execution_delay_ms: 0,
            quality: Some(SignalQuality {
                probability_lower: 0.55,
                move_bp,
                z: 0.0,
                fee_rate_ceiling: FEE_RATE,
                min_edge: (1.0 - pair * (1.0 + FEE_RATE)).max(0.0),
                suggested_budget: Some(5.0),
            }),
            scenario: Some("hedge_merge".into()),
            legs: Some(legs),
        })
    }

    fn trader_clone_tick(&mut self, tick: &Tick) -> Option<Signal> {
        let wall_seconds = tick.received_ms / 1000;
        let interval_start = wall_seconds / 900 * 900;
        if self.trader_interval_start != Some(interval_start) {
            self.trader_interval_start = Some(interval_start);
            // Do not invent the interval open after a restart in the middle of a candle.
            self.trader_interval_open = (wall_seconds - interval_start <= 2).then_some(tick.mid());
            self.trader_emitted_for = None;
        }
        let Some(open) = self.trader_interval_open else {
            self.edge_reason = "missing_interval_open";
            return None;
        };
        let next_start = interval_start + 900;
        let move_bp = (tick.mid() / open - 1.0) * 10000.0;
        self.edge_last_score = Some(serde_json::json!({
            "move_bp": move_bp, "probability": 0.58, "min_ask": 0.49, "max_ask": 0.51
        }));
        // Pre-open still enters ~200s before the UTC half-hour BTC 15m window.
        if next_start % 1800 != 0 {
            self.edge_reason = "not_half_hour_window";
            return None;
        }
        if self.trader_emitted_for == Some(next_start) {
            self.edge_reason = "already_signalled";
            return None;
        }
        let seconds_to_start = next_start.saturating_sub(wall_seconds);
        if !(190..=235).contains(&seconds_to_start) {
            self.edge_reason = "outside_preopen_window";
            return None;
        }
        // Follow stays green post-deploy; demand a clearer print and keep asks near 0.50.
        if move_bp.abs() < 15.0 {
            self.edge_reason = "insufficient_trend_move";
            return None;
        }
        let outcome = if move_bp > 0.0 { "Up" } else { "Down" };
        let expected_slug = format!("btc-updown-15m-{next_start}");
        let Some(quote) = self.quotes.values().find(|quote| {
            quote.slug == expected_slug
                && quote.outcome == outcome
                && quote.liquid_and_fresh(tick.mono_ms)
                && quote.end_ms == (next_start + 900) * 1000
        }) else {
            self.edge_reason = "waiting_for_quote";
            return None;
        };
        if quote.ask < 0.49 || quote.ask > 0.51 {
            self.edge_reason = "outside_preopen_price";
            return None;
        }
        let signal = Signal {
            token: quote.token.clone(),
            slug: quote.slug.clone(),
            outcome: outcome.into(),
            ask: quote.ask,
            mono_ms: tick.mono_ms,
            trigger_ms: tick.mono_ms,
            received_ms: tick.received_ms,
            end_ms: quote.end_ms,
            execution_delay_ms: 20,
            quality: Some(SignalQuality {
                probability_lower: 0.58,
                move_bp,
                z: 0.0,
                fee_rate_ceiling: 0.02,
                min_edge: 0.0,
                suggested_budget: Some(5.0),
            }),
            scenario: None,
            legs: None,
        };
        self.trader_emitted_for = Some(next_start);
        self.edge_reason = "signal";
        self.confirmed += 1;
        Some(signal)
    }

    fn signal(&mut self, direction: f64, trigger_ms: u64, tick: &Tick) -> Option<Signal> {
        self.confirmed += 1;
        let outcome = if direction > 0.0 { "Up" } else { "Down" };
        let q = self
            .quotes
            .values()
            .find(|q| q.outcome == outcome && q.usable(tick.mono_ms))?;
        Some(Signal {
            token: q.token.clone(),
            slug: q.slug.clone(),
            outcome: outcome.into(),
            ask: q.ask,
            mono_ms: tick.mono_ms,
            trigger_ms,
            received_ms: tick.received_ms,
            end_ms: q.end_ms,
            execution_delay_ms: 20,
            quality: None,
            scenario: None,
            legs: None,
        })
    }

    pub fn expire(&mut self, now: u64) {
        if self
            .candidate
            .as_ref()
            .is_some_and(|c| now.saturating_sub(c.start) > 250)
        {
            self.candidate = None;
            self.expired += 1;
        }
    }
    pub fn on_event(&mut self, event: Event) -> Option<Signal> {
        match event {
            Event::Reset { venue } => {
                if venue == Venue::Kraken {
                    if let Some(quality) = &mut self.quality {
                        quality.reset();
                        if !self.kraken_history.is_empty() {
                            quality.load_history(self.kraken_history.clone());
                        }
                    }
                    self.edge_interval_start=None;self.edge_interval_open=None;
                    self.edge_last_tick=None;self.edge_reason="kraken_reset_warming_up";
                }
                self.history.remove(&venue);
                self.candidate = None;
                None
            }
            Event::KrakenHistory { closes } => {
                self.kraken_history=closes.clone();
                if let Some(quality) = &mut self.quality {
                    quality.load_history(closes);
                }
                None
            }
            Event::Status { venue, state } => {
                if venue == "Polymarket" && (state == "connected" || state == "disconnected") {
                    self.quotes.clear();
                }
                None
            }
            Event::Quote(q) => {
                self.quotes.retain(|_, old| old.slug == q.slug);
                let token=q.token.clone();
                self.quotes.insert(token.clone(), q);
                if self.up60 {
                    let quote=self.quotes.get(&token).cloned().unwrap();
                    self.up60_quote(&quote)
                } else if self.down35 {
                    let quote=self.quotes.get(&token).cloned().unwrap();
                    self.down35_quote(&quote)
                } else if self.clip5 || self.clip15 {
                    let quote=self.quotes.get(&token).cloned().unwrap();
                    self.clip_quote(&quote)
                } else if self.early3m || self.early3m5 {
                    let quote=self.quotes.get(&token).cloned().unwrap();
                    self.early3m_quote(&quote)
                } else {None}
            }
            Event::Tick(t) => if self.up60 || self.down35 || self.clip5 || self.clip15 || self.early3m || self.early3m5 {
                self.edge_tick(t)
            } else {
                self.tick(t)
            },
        }
    }
    fn tick(&mut self, t: Tick) -> Option<Signal> {
        self.expire(t.mono_ms);
        if !t.valid() {
            if t.venue == Venue::Kraken {
                if let Some(quality) = &mut self.quality {quality.reset();}
            }
            self.history.remove(&t.venue);
            self.candidate = None;
            return None;
        }
        if let Some(quality) = &mut self.quality {
            let signal = quality.tick(&t, &self.quotes);
            if signal.is_some() {self.confirmed += 1;}
            return signal;
        }
        if self.trader_clone {
            return (t.venue == Venue::Kraken)
                .then(|| self.trader_clone_tick(&t))
                .flatten();
        }
        let history = self.history.entry(t.venue).or_default();
        // The article's event threshold: >=1bp within 5ms, measured on local arrival time.
        let baseline = history
            .iter()
            .rev()
            .take_while(|p| t.mono_ms.saturating_sub(p.mono_ms) <= 5)
            .last()
            .map(Tick::mid);
        let change = baseline
            .map(|b| (t.mid() / b - 1.0) * 10000.0)
            .unwrap_or(0.0);
        let kraken_trigger = t.venue == Venue::Kraken
            && change.abs() >= 1.0
            && self.candidate.is_none();
        if kraken_trigger && !self.kraken_only {
            let mut baselines = HashMap::new();
            for venue in [Venue::Deribit, Venue::Binance] {
                if let Some(last) = self.history.get(&venue).and_then(|h| h.back()) {
                    if t.mono_ms.saturating_sub(last.mono_ms) <= 500 {
                        baselines.insert(venue, last.mid());
                    }
                }
            }
            if baselines.len() == 2 {
                self.candidate = Some(Candidate {
                    direction: change.signum(),
                    start: t.mono_ms,
                    baselines,
                    followers: HashMap::new(),
                });
            }
        }
        let history = self.history.entry(t.venue).or_default();
        history.push_back(t.clone());
        while history
            .front()
            .is_some_and(|p| t.mono_ms.saturating_sub(p.mono_ms) > 1000)
        {
            history.pop_front();
        }
        // Bound memory even when many updates share a timestamp.
        while history.len() > 20000 {
            history.pop_front();
        }
        if self.kraken_only {
            return kraken_trigger
                .then_some(change.signum())
                .and_then(|direction| self.signal(direction, t.mono_ms, &t));
        }
        let c = self.candidate.as_mut()?;
        if let Some(base) = c.baselines.get(&t.venue) {
            let bp = (t.mid() / base - 1.0) * 10000.0 * c.direction;
            if bp >= 0.8 && t.mono_ms > c.start {
                c.followers.insert(t.venue, t.mono_ms);
            } else {
                c.followers.remove(&t.venue);
            }
        }
        if c.followers.len() != 2 {
            return None;
        }
        // Both confirmations must still be fresh; Kraken must not have reversed.
        for venue in [Venue::Kraken, Venue::Deribit, Venue::Binance] {
            let last = self.history.get(&venue)?.back()?;
            if t.mono_ms.saturating_sub(last.mono_ms) > 250 {
                return None;
            }
        }
        let kraken = self.history.get(&Venue::Kraken)?.back()?.mid();
        if let Some(start) = self
            .history
            .get(&Venue::Kraken)?
            .iter()
            .find(|k| k.mono_ms == c.start)
        {
            if (kraken / start.mid() - 1.0) * c.direction < -0.00008 {
                self.candidate = None;
                return None;
            }
        }
        let direction = c.direction;
        let trigger_ms = c.start;
        self.candidate = None;
        self.signal(direction, trigger_ms, &t)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn tick(v: Venue, p: f64, ms: u64) -> Event {
        Event::Tick(Tick {
            venue: v,
            bid: p,
            ask: p,
            exchange_ms: ms,
            received_ms: ms,
            mono_ms: ms,
        })
    }
    fn ready() -> Engine {
        let mut e = Engine::default();
        for v in [Venue::Kraken, Venue::Deribit, Venue::Binance] {
            e.on_event(tick(v, 100000.0, 1000));
        }
        e.on_event(Event::Quote(Quote {
            slug: "market".into(),
            token: "up".into(),
            outcome: "Up".into(),
            end_ms: 30000,
            bid: 0.49,
            ask: 0.5,
            ask_size: 100.0,
            min_size: 5.0,
            received_ms: 1000,
            exchange_ms: 1000,
            mono_ms: 1000,
        }));
        e
    }
    #[test]
    fn needs_both_confirmations() {
        let mut e = ready();
        assert!(e.on_event(tick(Venue::Kraken, 100020.0, 1005)).is_none());
        assert!(e.on_event(tick(Venue::Deribit, 100020.0, 1010)).is_none());
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1020)).is_some());
    }
    #[test]
    fn kraken_only_emits_without_other_venues() {
        let mut e = Engine::kraken_only();
        e.on_event(tick(Venue::Kraken, 100000.0, 1000));
        e.on_event(Event::Quote(Quote {
            slug: "market".into(), token: "up".into(), outcome: "Up".into(),
            end_ms: 30000, bid: 0.49, ask: 0.5, ask_size: 100.0, min_size: 5.0,
            received_ms: 1000, exchange_ms: 1000, mono_ms: 1000,
        }));
        let signal = e.on_event(tick(Venue::Kraken, 100020.0, 1005)).unwrap();
        assert_eq!(signal.token, "up");
        assert_eq!(e.confirmed, 1);
    }
    #[test]
    fn trader_clone_uses_next_15m_market_and_mean_reversion() {
        let mut e = Engine::trader_clone();
        let interval_start = 1_799_100u64;
        let next_start = interval_start + 900;
        let tick_at = |price: f64, seconds: u64, mono: u64| Event::Tick(Tick {
            venue: Venue::Kraken, bid: price, ask: price,
            exchange_ms: seconds * 1000, received_ms: seconds * 1000, mono_ms: mono,
        });
        e.on_event(tick_at(100_000.0, interval_start, 1));
        let quote = |ask: f64, outcome: &str, token: &str| Event::Quote(Quote {
            slug: format!("btc-updown-15m-{next_start}"), token: token.into(),
            outcome: outcome.into(), end_ms: (next_start + 900) * 1000,
            bid: ask - 0.01, ask, ask_size: 100.0, min_size: 5.0,
            received_ms: (next_start - 205) * 1000,
            exchange_ms: (next_start - 205) * 1000, mono_ms: 10,
        });
        e.on_event(quote(0.51, "Up", "up"));
        assert!(e.on_event(tick_at(100_020.0, next_start - 205, 10)).is_none());
        e.on_event(quote(0.56, "Up", "up"));
        assert!(e.on_event(tick_at(100_160.0, next_start - 205, 10)).is_none());
        e.on_event(quote(0.51, "Up", "up"));
        let signal = e.on_event(tick_at(100_160.0, next_start - 205, 10)).unwrap();
        assert_eq!(signal.outcome, "Up");
        assert_eq!(signal.slug, format!("btc-updown-15m-{next_start}"));
        assert!(signal.quality.unwrap().move_bp >= 15.0);
        assert!(e.on_event(tick_at(100_170.0, next_start - 204, 11)).is_none());
    }

    #[test]
    fn early3m_directional_after_observe_or_hedges_when_uncertain() {
        let mut engine = Engine::early3m();
        let start = 1_800_000u64;
        let quote = |ask: f64, outcome: &str, elapsed: u64| Event::Quote(Quote {
            slug: format!("btc-updown-15m-{start}"),
            token: outcome.to_lowercase(),
            outcome: outcome.into(),
            end_ms: (start + 900) * 1000,
            bid: ask - 0.01, ask, ask_size: 100.0, min_size: 5.0,
            received_ms: (start + elapsed) * 1000,
            exchange_ms: (start + elapsed) * 1000,
            mono_ms: elapsed,
        });
        let tick = |mid: f64, elapsed: u64| Event::Tick(Tick {
            venue: Venue::Kraken, bid: mid, ask: mid,
            exchange_ms: (start + elapsed) * 1000,
            received_ms: (start + elapsed) * 1000,
            mono_ms: elapsed,
        });
        engine.on_event(Event::KrakenHistory { closes: vec![(start - 60, 100.0)] });
        engine.on_event(tick(100.0, 1));
        engine.on_event(tick(100.05, 100));
        assert!(engine.on_event(quote(0.48, "Up", 100)).is_none());
        assert!(engine.on_event(quote(0.49, "Down", 100)).is_none());
        // Uncertain move + pair under 0.98 → hedge, riskier (Up) first.
        engine.on_event(tick(100.05, 190));
        engine.on_event(quote(0.48, "Up", 190));
        let hedge = engine.on_event(quote(0.49, "Down", 190)).unwrap();
        assert_eq!(hedge.scenario.as_deref(), Some("hedge_merge"));
        assert_eq!(hedge.outcome, "Up");
        let legs = hedge.legs.unwrap();
        assert_eq!(legs.len(), 2);
        assert_eq!(legs[0].role, "primary");
        assert_eq!(legs[0].outcome, "Up");
        assert_eq!(legs[1].role, "hedge");
        assert_eq!(legs[1].outcome, "Down");
        assert!(engine.on_event(quote(0.47, "Up", 200)).is_none());

        let mut strong = Engine::early3m();
        strong.on_event(Event::KrakenHistory { closes: vec![(start - 60, 100.0)] });
        strong.on_event(tick(100.0, 1));
        strong.on_event(tick(100.15, 190));
        assert!(strong.on_event(quote(0.55, "Down", 190)).is_none());
        let directional = strong.on_event(quote(0.58, "Up", 190)).unwrap();
        assert_eq!(directional.scenario.as_deref(), Some("directional"));
        assert_eq!(directional.outcome, "Up");
        assert!(directional.quality.unwrap().move_bp >= 12.0);
        assert!(directional.legs.is_none());

        let mut five = Engine::early3m5();
        let quote5 = |ask: f64, outcome: &str, elapsed: u64| Event::Quote(Quote {
            slug: format!("btc-updown-5m-{start}"),
            token: outcome.to_lowercase(),
            outcome: outcome.into(),
            end_ms: (start + 300) * 1000,
            bid: ask - 0.01, ask, ask_size: 100.0, min_size: 5.0,
            received_ms: (start + elapsed) * 1000,
            exchange_ms: (start + elapsed) * 1000,
            mono_ms: elapsed,
        });
        five.on_event(Event::KrakenHistory { closes: vec![(start - 60, 100.0)] });
        five.on_event(tick(100.0, 1));
        five.on_event(tick(100.08, 50));
        assert!(five.on_event(quote5(0.57, "Up", 50)).is_none()); // still observing
        five.on_event(tick(100.08, 100));
        let five_dir = five.on_event(quote5(0.57, "Up", 100)).unwrap();
        assert_eq!(five_dir.slug, format!("btc-updown-5m-{start}"));
        assert_eq!(five_dir.scenario.as_deref(), Some("directional"));
        assert_eq!(five_dir.outcome, "Up");
        // Expensive directional asks are rejected; hedge can still fire.
        let mut five_hedge = Engine::early3m5();
        five_hedge.on_event(Event::KrakenHistory { closes: vec![(start - 60, 100.0)] });
        five_hedge.on_event(tick(100.0, 1));
        five_hedge.on_event(tick(100.03, 100));
        five_hedge.on_event(quote5(0.48, "Up", 100));
        let hedged = five_hedge.on_event(quote5(0.49, "Down", 100)).unwrap();
        assert_eq!(hedged.scenario.as_deref(), Some("hedge_merge"));
    }

    #[test]
    fn clip_buys_only_the_price_bands_that_cleared_fees() {
        let mut five = Engine::clip5();
        let start = 1_800_000u64;
        let quote = |ask, slug: &str, outcome: &str, elapsed: u64| {
            let market_start: u64 = slug.rsplit('-').next().unwrap().parse().unwrap();
            let interval = if slug.contains("-5m-") { 300 } else { 900 };
            Event::Quote(Quote {
                slug: slug.into(), token: outcome.into(), outcome: outcome.into(),
                end_ms: (market_start + interval) * 1000,
                bid: ask - 0.01, ask, ask_size: 100.0, min_size: 5.0,
                received_ms: (market_start + elapsed) * 1000,
                exchange_ms: (market_start + elapsed) * 1000, mono_ms: elapsed,
            })
        };
        let tick = |mid: f64, seconds: u64, mono: u64| Event::Tick(Tick {
            venue: Venue::Kraken, bid: mid, ask: mid,
            exchange_ms: seconds * 1000, received_ms: seconds * 1000, mono_ms: mono,
        });
        five.on_event(Event::KrakenHistory { closes: vec![(start - 60, 100.0)] });
        five.on_event(tick(100.0, start + 1, 1));
        five.on_event(tick(100.06, start + 100, 100));
        assert!(five.on_event(quote(0.50, "btc-updown-5m-1800000", "Up", 100)).is_none());
        assert!(five.on_event(quote(0.58, "btc-updown-5m-1800000", "Up", 100)).is_none());
        assert!(five.on_event(quote(0.63, "btc-updown-5m-1800000", "Down", 100)).is_none());
        let favorite = five.on_event(quote(0.63, "btc-updown-5m-1800000", "Up", 100)).unwrap();
        assert_eq!(favorite.outcome, "Up");
        assert!(favorite.quality.unwrap().move_bp >= 5.0);
        assert!(five.on_event(quote(0.64, "btc-updown-5m-1800000", "Down", 100)).is_none());
        let mut fifteen = Engine::clip15();
        fifteen.on_event(Event::KrakenHistory { closes: vec![(start - 60, 100.0)] });
        fifteen.on_event(tick(100.0, start + 1, 1));
        fifteen.on_event(tick(100.09, start + 320, 320));
        assert!(fifteen.on_event(quote(0.60, "btc-updown-15m-1800000", "Up", 30)).is_none());
        assert!(fifteen.on_event(quote(0.40, "btc-updown-15m-1800000", "Down", 320)).is_none());
        let favorite15 = fifteen.on_event(quote(0.60, "btc-updown-15m-1800000", "Up", 320)).unwrap();
        assert_eq!(favorite15.ask, 0.60);
        assert!(favorite15.quality.unwrap().move_bp >= 7.5);
        assert!(fifteen.on_event(quote(0.62, "btc-updown-15m-1800000", "Down", 320)).is_none());
    }

    #[test]
    fn trader_clone_waits_after_mid_interval_restart() {
        let mut e = Engine::trader_clone();
        let seconds = 1_800_100u64;
        let event = Event::Tick(Tick {venue:Venue::Kraken,bid:100_000.0,ask:100_000.0,
            exchange_ms:seconds*1000,received_ms:seconds*1000,mono_ms:1});
        assert!(e.on_event(event).is_none());
        assert!(e.trader_interval_open.is_none());
    }
    #[test]
    fn up60_requires_strong_fresh_kraken_confirmation_and_price_cap() {
        let mut e=Engine::up60();
        let quote=|ask,slug:&str| Event::Quote(Quote {slug:slug.into(),
            token:"up".into(),outcome:"Up".into(),end_ms:1800900000,
            bid:ask-0.01,ask,ask_size:100.0,min_size:5.0,received_ms:1800000100,
            exchange_ms:1800000100,mono_ms:100});
        e.on_event(Event::KrakenHistory {closes:vec![(1_799_940,100.0)]});
        assert!(e.on_event(Event::Tick(Tick {venue:Venue::Kraken,bid:100.05,ask:100.05,
            exchange_ms:1_800_100_000,received_ms:1_800_100_000,mono_ms:100})).is_none());
        assert!(e.on_event(quote(0.60,"btc-updown-15m-1800000")).is_none());
        assert!(e.on_event(quote(0.60,"eth-updown-15m-1800000")).is_none());
        assert!(e.on_event(quote(0.57,"btc-updown-15m-1800000")).is_none());
        assert!(e.on_event(quote(0.63,"btc-updown-15m-1800000")).is_none());
        // Ask 0.63 stays cached, so a +11bp tick still cannot fire.
        assert!(e.on_event(Event::Tick(Tick {venue:Venue::Kraken,bid:100.11,ask:100.11,
            exchange_ms:1_800_100_000,received_ms:1_800_100_000,mono_ms:100})).is_none());
        let signal=e.on_event(quote(0.59,"btc-updown-15m-1800000")).unwrap();
        assert_eq!(signal.outcome,"Up");
        assert_eq!(signal.execution_delay_ms,200);
        assert!(signal.quality.unwrap().move_bp>=7.5);
        assert!(e.on_event(quote(0.60,"btc-updown-15m-1800000")).is_none());
    }
    #[test]
    fn down35_requires_sol_kraken_drop_and_tighter_price() {
        let mut e=Engine::down35();
        let quote=|ask| Event::Quote(Quote {slug:"sol-updown-5m-1800000".into(),
            token:"down".into(),outcome:"Down".into(),end_ms:1800300000,
            bid:ask-0.02,ask,ask_size:100.0,min_size:5.0,received_ms:1800000100,
            exchange_ms:1800000100,mono_ms:100});
        assert!(e.on_event(quote(0.36)).is_none());
        assert!(e.on_event(quote(0.32)).is_none());
        e.on_event(Event::KrakenHistory {closes:vec![(1_799_940,100.0)]});
        let signal=e.on_event(Event::Tick(Tick {venue:Venue::Kraken,bid:99.89,ask:99.89,
            exchange_ms:1_800_100_000,received_ms:1_800_100_000,mono_ms:100})).unwrap();
        assert_eq!(signal.outcome,"Down");assert_eq!(signal.execution_delay_ms,0);
        assert!(signal.quality.unwrap().move_bp<=-10.0);
        assert!(e.on_event(quote(0.30)).is_none());
    }
    #[test]
    fn expired_move_cannot_trigger() {
        let mut e = ready();
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        e.on_event(tick(Venue::Deribit, 100020.0, 1010));
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1300)).is_none());
        assert_eq!(e.expired, 1);
    }
    #[test]
    fn stale_exchange_message_invalidates() {
        let mut e = ready();
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        if let Event::Tick(mut t) = tick(Venue::Deribit, 100020.0, 1010) {
            t.exchange_ms = 0;
            e.on_event(Event::Tick(t));
        }
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1020)).is_none());
    }
    #[test]
    fn reset_discards_candidate() {
        let mut e = ready();
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        e.on_event(Event::Reset {
            venue: Venue::Deribit,
        });
        e.on_event(tick(Venue::Deribit, 100020.0, 1010));
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1020)).is_none());
    }
    #[test]
    fn stale_quotes_do_not_trade() {
        let mut e = ready();
        e.quotes.get_mut("up").unwrap().mono_ms = 0;
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        e.on_event(tick(Venue::Deribit, 100020.0, 1010));
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1020)).is_none());
    }
    #[test]
    fn down_uses_actual_down_token() {
        let mut e = ready();
        let mut q = e.quotes["up"].clone();
        q.token = "down".into();
        q.outcome = "Down".into();
        q.ask = 0.6;
        q.bid = 0.59;
        e.quotes.insert("down".into(), q);
        e.on_event(tick(Venue::Kraken, 99980.0, 1005));
        e.on_event(tick(Venue::Deribit, 99980.0, 1010));
        let s = e.on_event(tick(Venue::Binance, 99980.0, 1020)).unwrap();
        assert_eq!(s.token, "down");
        assert_eq!(s.ask, 0.6);
    }
    #[test]
    fn kraken_reversal_cancels_signal() {
        let mut e = ready();
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        e.on_event(tick(Venue::Deribit, 100020.0, 1010));
        e.on_event(tick(Venue::Kraken, 99980.0, 1015));
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1020)).is_none());
    }
    #[test]
    fn tied_arrivals_do_not_confirm() {
        let mut e = ready();
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        e.on_event(tick(Venue::Deribit, 100020.0, 1005));
        assert!(e.on_event(tick(Venue::Binance, 100020.0, 1005)).is_none());
    }
    #[test]
    fn polymarket_disconnect_invalidates_quotes() {
        let mut e = ready();
        e.on_event(Event::Status {
            venue: "Polymarket".into(),
            state: "disconnected".into(),
        });
        assert!(e.quotes.is_empty());
    }
    #[test]
    fn complete_pipeline_produces_only_hypothetical_fill() {
        let mut e = ready();
        let mut provider = crate::provider::PaperProvider::default();
        e.on_event(tick(Venue::Kraken, 100020.0, 1005));
        e.on_event(tick(Venue::Deribit, 100020.0, 1010));
        let signal = e.on_event(tick(Venue::Binance, 100020.0, 1020)).unwrap();
        provider.submit(signal);
        let fill = provider.advance(1040, &e.quotes).unwrap();
        assert_eq!(fill["execution"], "hypothetical_top_of_book");
        assert_eq!(fill["settled"], false);
        assert_eq!(provider.spent, 5.0);
    }
}
