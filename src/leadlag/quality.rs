use super::types::*;
use serde::Deserialize;
use std::collections::{HashMap, VecDeque};

#[derive(Clone, Deserialize)]
pub struct Bucket {
    pub min_z: f64,
    pub max_z: Option<f64>,
    pub samples: usize,
    pub probability_lower: f64,
}
#[derive(Clone, Deserialize)]
pub struct MoveBucket {
    pub min_move_bp: f64,
    pub max_move_bp: Option<f64>,
    pub samples: usize,
    pub probability_lower: f64,
}
#[derive(Clone, Deserialize)]
pub struct PositionTier {
    pub min_probability: f64,
    pub budget: f64,
}
#[derive(Clone, Deserialize)]
pub struct Config {
    pub version: u64,
    #[serde(default = "default_asset")]
    pub asset: String,
    pub interval_seconds: u64,
    pub remaining_seconds: u64,
    #[serde(default = "default_entry_window")]
    pub entry_window_seconds: u64,
    pub min_z: f64,
    pub min_move_bp: f64,
    pub volatility_minutes: usize,
    pub volatility_floor: f64,
    pub min_probability: f64,
    pub min_edge: f64,
    #[serde(default = "default_min_ask")]
    pub min_ask: f64,
    pub max_ask: f64,
    pub fee_rate_ceiling: f64,
    pub probability_haircut: f64,
    #[serde(default)]
    pub ema_fast: usize,
    #[serde(default)]
    pub ema_slow: usize,
    #[serde(default)]
    pub rsi_period: usize,
    pub probability_buckets: Vec<Bucket>,
    #[serde(default)]
    pub move_probability_buckets: Vec<MoveBucket>,
    #[serde(default)]
    pub position_tiers: Vec<PositionTier>,
    #[serde(default = "default_order_notional")]
    pub order_notional: f64,
    pub proxy_validation_passed: bool,
}
fn default_min_ask() -> f64 {0.1}
fn default_order_notional() -> f64 {3.2}
fn default_asset() -> String {"btc".into()}
fn default_entry_window() -> u64 {5}
fn ema(values: &[f64], period: usize) -> f64 {
    let alpha = 2.0/(period as f64+1.0);
    values.iter().skip(1).fold(values[0], |prior, value| alpha*value+(1.0-alpha)*prior)
}
fn rsi(values: &[f64], period: usize) -> f64 {
    let changes = values[values.len()-period-1..].windows(2).map(|pair| pair[1]-pair[0]);
    let (gain, loss) = changes.fold((0.0,0.0), |(gain,loss), change|
        (gain+change.max(0.0),loss+(-change).max(0.0)));
    if loss == 0.0 {100.0} else {100.0-100.0/(1.0+gain/loss)}
}
impl Config {
    pub fn validate(&self) -> Result<(), &'static str> {
        let numbers = [self.min_z, self.min_move_bp, self.volatility_floor,
            self.min_probability, self.min_edge, self.max_ask, self.fee_rate_ceiling,self.probability_haircut];
        if !matches!(self.asset.as_str(), "btc" | "eth")
            || !matches!(self.version, 1 | 2 | 3)
            || self.volatility_minutes != 60 || !self.proxy_validation_passed
            || numbers.iter().any(|x| !x.is_finite())
            || self.volatility_floor < 0.0001
            || !(0.07..=0.10).contains(&self.fee_rate_ceiling)
            || !self.min_ask.is_finite() || !self.order_notional.is_finite()
            || self.min_ask < 0.1 || self.max_ask < self.min_ask || self.max_ask > 0.9
            || self.order_notional <= 0.0 {
            return Err("Invalid or unvalidated Kraken quality configuration");
        }
        if self.version == 3 {
            if self.interval_seconds != 900 || self.remaining_seconds != 720
                || self.entry_window_seconds != 30 || self.ema_fast != 9
                || self.ema_slow != 21 || self.rsi_period != 14
                || self.min_z != 0.0 || self.min_move_bp < 1.0
                || !(0.55..=0.80).contains(&self.min_probability)
                || !(0.005..=0.10).contains(&self.min_edge)
                || !(0.02..=0.10).contains(&self.probability_haircut)
                || self.min_ask < 0.20 || self.max_ask > 0.80
                || !(3.9..=4.0).contains(&self.order_notional)
                || self.move_probability_buckets.is_empty()
                || self.position_tiers.is_empty() {
                return Err("Invalid 15-minute trend configuration");
            }
            let mut prior_probability = 0.0;
            let mut prior_budget = 0.0;
            for tier in &self.position_tiers {
                if !tier.min_probability.is_finite() || !tier.budget.is_finite()
                    || tier.min_probability < self.min_probability
                    || tier.min_probability <= prior_probability
                    || tier.budget < 4.0 || tier.budget > 5.0
                    || tier.budget <= prior_budget {
                    return Err("Invalid 15-minute position tiers");
                }
                prior_probability = tier.min_probability;
                prior_budget = tier.budget;
            }
            self.validate_move_buckets()?;
            return Ok(());
        }
        if self.interval_seconds != 300 || self.remaining_seconds != 60
            || self.entry_window_seconds != 5 {
            return Err("Invalid quality timing configuration");
        }
        if self.version == 2 {
            if self.min_z < 0.0 || self.min_move_bp < 5.0
                || !(0.80..=0.99).contains(&self.min_probability)
                || !(0.02..=0.2).contains(&self.min_edge)
                || !(0.02..=0.15).contains(&self.probability_haircut)
                || self.min_ask < 0.65 || self.max_ask > 0.77
                || !(3.9..=4.0).contains(&self.order_notional)
                || self.move_probability_buckets.is_empty() {
                return Err("Invalid wallet-replica configuration");
            }
            self.validate_move_buckets()?;
            return Ok(());
        }
        if self.min_z < 0.5 || self.min_move_bp < 2.0
            || !(0.65..=0.99).contains(&self.min_probability)
            || !(0.03..=0.2).contains(&self.min_edge)
            || self.max_ask > 0.59
            || !(0.10..=0.25).contains(&self.probability_haircut) {
            return Err("Invalid or unvalidated Kraken quality configuration");
        }
        let mut boundary = self.min_z;
        if self.probability_buckets.is_empty() {return Err("Missing probability calibration");}
        for (index, bucket) in self.probability_buckets.iter().enumerate() {
            if !bucket.min_z.is_finite() || bucket.min_z != boundary
                || !bucket.probability_lower.is_finite()
                || !(0.0..=0.99).contains(&bucket.probability_lower)
                || (bucket.samples < 100 && bucket.probability_lower > 0.0) {
                return Err("Invalid probability calibration bucket");
            }
            match bucket.max_z {
                Some(upper) if upper.is_finite() && upper > boundary => boundary = upper,
                None if index == self.probability_buckets.len()-1 => {},
                _ => return Err("Invalid calibration bounds"),
            }
        }
        Ok(())
    }
    fn validate_move_buckets(&self) -> Result<(), &'static str> {
        let mut boundary = self.min_move_bp;
        for (index, bucket) in self.move_probability_buckets.iter().enumerate() {
            if !bucket.min_move_bp.is_finite() || bucket.min_move_bp != boundary
                || !bucket.probability_lower.is_finite()
                || !(0.0..=0.99).contains(&bucket.probability_lower)
                || bucket.samples < 100 {
                return Err("Invalid move calibration bucket");
            }
            match bucket.max_move_bp {
                Some(upper) if upper.is_finite() && upper > boundary => boundary = upper,
                None if index == self.move_probability_buckets.len()-1 => {},
                _ => return Err("Invalid move calibration bounds"),
            }
        }
        Ok(())
    }
    pub fn probability(&self, z: f64, move_bp: f64) -> f64 {
        let raw = if matches!(self.version, 2 | 3) {
            self.move_probability_buckets.iter()
                .find(|b| move_bp >= b.min_move_bp && b.max_move_bp.is_none_or(|u| move_bp < u))
                .map(|b| b.probability_lower)
        } else {
            self.probability_buckets.iter()
                .find(|b| z >= b.min_z && b.max_z.is_none_or(|u| z < u))
                .map(|b| b.probability_lower)
        };
        raw.map(|p| (p-self.probability_haircut).max(0.0)).unwrap_or(0.0)
    }
    pub fn budget(&self, probability: f64) -> f64 {
        self.position_tiers.iter().rev()
            .find(|tier| probability >= tier.min_probability)
            .map(|tier| tier.budget).unwrap_or(self.order_notional)
    }
}

pub struct QualityEngine {
    pub config: Config,
    closes: VecDeque<(u64, f64)>,
    last: Option<Tick>,
    interval_start: Option<u64>,
    open: Option<f64>,
    emitted_for: Option<u64>,
    pub reason: &'static str,
    pub opportunities: usize,
    counted_for: Option<u64>,
    last_decision: Option<(u64, &'static str)>,
    history_source: &'static str,
    decision_counts: HashMap<&'static str, usize>,
    decision_counted_for: Option<u64>,
    last_score: Option<serde_json::Value>,
}
impl QualityEngine {
    pub fn new(config: Config) -> Self {
        Self {config, closes: VecDeque::new(), last: None, interval_start: None,
            open: None, emitted_for: None, reason: "warming_up", opportunities: 0, counted_for: None,
            last_decision: None, history_source: "warming_up", decision_counts:HashMap::new(),
            decision_counted_for:None,last_score:None}
    }
    pub fn load_history(&mut self, closes: Vec<(u64, f64)>) {
        if closes.len() == 61
            && closes.windows(2).all(|rows| rows[1].0 == rows[0].0 + 60)
            && closes.iter().all(|row| row.1.is_finite() && row.1 > 0.0) {
            self.closes = closes.into();
            self.reason = "history_ready";
            self.history_source = "kraken_rest_1m";
        }
    }
    pub fn reset(&mut self) {
        // Brief Kraken reconnects were wiping 61 minutes of closes and silencing
        // the trend feed for another hour; keep the ring and only drop live state.
        self.last = None; self.interval_start = None;
        self.open = None; self.reason = "kraken_reset_warming_up";
        self.history_source = if self.closes.len() >= 61 {"kept_closes_after_reset"} else {"reset_warming_up"};
        // Keep market deduplication across reconnects.
    }
    pub fn health(&self) -> serde_json::Value {
        serde_json::json!({"reason":self.reason,"closed_minutes":self.closes.len(),
            "required_closed_minutes":61,"quality_opportunities":self.opportunities,
            "has_interval_open":self.open.is_some(),"last_entry_window_decision":self.last_decision,
            "history_source":self.history_source,"decision_counts":self.decision_counts,
            "last_score":self.last_score})
    }
    pub fn tick(&mut self, tick: &Tick, quotes: &HashMap<String, Quote>) -> Option<Signal> {
        let result = self.evaluate(tick, quotes);
        let seconds=tick.received_ms/1000;
        let start=seconds/self.config.interval_seconds*self.config.interval_seconds;
        let remaining=self.config.interval_seconds-seconds%self.config.interval_seconds;
        if tick.venue == Venue::Kraken
            && self.decision_counted_for != Some(start)
            && (result.is_some() || remaining == self.config.remaining_seconds-self.config.entry_window_seconds) {
            self.last_decision = Some((tick.received_ms,self.reason));
            *self.decision_counts.entry(self.reason).or_default() += 1;
            self.decision_counted_for = Some(start);
        }
        result
    }
    fn evaluate(&mut self, tick: &Tick, quotes: &HashMap<String, Quote>) -> Option<Signal> {
        if tick.venue != Venue::Kraken {return None;}
        if (tick.ask-tick.bid)/tick.mid()*10000.0 > 2.0 {
            self.reason="kraken_spread_too_wide";return None;
        }
        let seconds = tick.received_ms / 1000;
        let minute = seconds/60*60;
        if let Some(last) = &self.last {
            let previous_minute = last.received_ms/60000*60;
            if previous_minute != minute {
                // The two adjacent observations prove feed continuity directly.
                // A quiet final couple of seconds is not a reason to erase an hour.
                if minute == previous_minute+60
                    && tick.received_ms.saturating_sub(last.received_ms) <= 5_000 {
                    self.closes.push_back((previous_minute, last.mid()));
                    while self.closes.len() > 61 {self.closes.pop_front();}
                    self.history_source = "live_orderbook_mid";
                } else {
                    self.closes.clear(); self.open = None;
                    self.history_source = "gap_warming_up";
                }
            }
        }
        self.last = Some(tick.clone());
        let start = seconds/self.config.interval_seconds*self.config.interval_seconds;
        if self.interval_start != Some(start) {
            self.interval_start = Some(start);
            self.open = (seconds-start <= 2).then_some(tick.mid());
        }
        if self.emitted_for == Some(start) {self.reason = "already_signalled"; return None;}
        if self.closes.len() < 61 {self.reason = "warming_up"; return None;}
        let Some(open) = self.open else {self.reason = "missing_interval_open";return None;};
        let end = start+self.config.interval_seconds;
        let remaining = end.saturating_sub(seconds);
        // Reproduce the historical observation time, allowing at most 5s for a quote.
        if remaining > self.config.remaining_seconds
            || remaining < self.config.remaining_seconds-self.config.entry_window_seconds {
            self.reason = "outside_entry_window"; return None;
        }
        let returns: Vec<f64> = self.closes.iter().zip(self.closes.iter().skip(1))
            .map(|(a,b)| (b.1/a.1).ln()).collect();
        let mean = returns.iter().sum::<f64>()/returns.len() as f64;
        let sigma = (returns.iter().map(|r| (r-mean).powi(2)).sum::<f64>()/(returns.len()-1) as f64)
            .sqrt().max(self.config.volatility_floor);
        let movement = (tick.mid()/open).ln();
        let z = movement.abs()/(sigma*(self.config.remaining_seconds as f64/60.0).sqrt());
        let move_bp = movement.abs()*10000.0;
        let probability = self.config.probability(z, move_bp);
        if self.config.version == 3 {
            let values: Vec<f64> = self.closes.iter().rev().take(30).map(|row| row.1).collect::<Vec<_>>()
                .into_iter().rev().collect();
            let ema_fast = ema(&values, self.config.ema_fast);
            let ema_slow = ema(&values, self.config.ema_slow);
            let rsi_value = rsi(&values, self.config.rsi_period);
            let direction = movement.signum();
            self.last_score = Some(serde_json::json!({"move_bp":movement*10000.0,
                "ema_spread_bp":(ema_fast/ema_slow).ln()*10000.0,"rsi":rsi_value,
                "probability":probability,"suggested_budget":self.config.budget(probability)}));
            if direction == 0.0 {self.reason = "flat_market";return None;}
        } else {
            let previous = (self.closes[self.closes.len()-2].1/open).ln();
            if movement*previous <= 0.0 {self.reason = "unstable_direction";return None;}
            self.last_score = Some(serde_json::json!({"move_bp":movement*10000.0,
                "z":z,"probability":probability}));
        }
        if move_bp < self.config.min_move_bp || z < self.config.min_z
            || probability < self.config.min_probability {
            self.reason = "insufficient_confidence"; return None;
        }
        if self.counted_for != Some(start) {self.opportunities += 1;self.counted_for = Some(start);}
        let outcome = if movement > 0.0 {"Up"} else {"Down"};
        let slug = format!("{}-updown-{}m-{start}", self.config.asset,
            self.config.interval_seconds/60);
        let Some(quote) = quotes.values().find(|q| q.slug == slug && q.outcome == outcome
            && q.end_ms == end*1000 && q.liquid_and_fresh(tick.mono_ms)) else {
            self.reason = "no_fresh_quote"; return None;
        };
        if quote.ask < self.config.min_ask {self.reason = "price_below_consensus_floor"; return None;}
        if quote.ask > self.config.max_ask {self.reason = "price_above_budget_cap"; return None;}
        let limit = quote.ask+0.01;
        let fee = self.config.fee_rate_ceiling*limit*(1.0-limit);
        if probability < limit+fee+self.config.min_edge {
            self.reason = "insufficient_fee_adjusted_edge"; return None;
        }
        // The live adapter spends at most 80% of the all-in reservation so that
        // settlement and fee headroom remain inside the selected tier.
        let notional = self.config.budget(probability)
            * if self.config.version == 3 {0.8} else {1.0};
        if notional/limit < quote.min_size || quote.ask_size*quote.ask < notional {
            self.reason = "insufficient_order_size_or_depth"; return None;
        }
        self.emitted_for = Some(start); self.reason = "signal";
        Some(Signal {token:quote.token.clone(),slug,outcome:outcome.into(),ask:quote.ask,
            mono_ms:tick.mono_ms,trigger_ms:tick.mono_ms,received_ms:tick.received_ms,end_ms:quote.end_ms,
            execution_delay_ms:20,
            quality:Some(SignalQuality {probability_lower:probability,move_bp:movement*10000.0,z,
                fee_rate_ceiling:self.config.fee_rate_ceiling,min_edge:self.config.min_edge,
                suggested_budget:(self.config.version == 3).then(|| self.config.budget(probability))}),
            scenario:None,legs:None})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn config() -> Config {serde_json::from_str(include_str!("../../kraken-quality.json")).unwrap()}
    fn replica_config() -> Config {serde_json::from_str(include_str!("../../kraken-replica.json")).unwrap()}
    fn trend_config() -> Config {serde_json::from_str(include_str!("../../kraken-trend-btc.json")).unwrap()}
    fn ready() -> QualityEngine {
        let mut e = QualityEngine::new(config());
        let start = 6000;
        e.interval_start = Some(start); e.open = Some(100000.0);
        for minute in (2400..=6000).step_by(60) {e.closes.push_back((minute,100100.0));}
        e
    }
    fn tick() -> Tick {Tick {venue:Venue::Kraken,bid:100100.0,ask:100100.0,
        exchange_ms:6240000,received_ms:6240000,mono_ms:1000}}
    fn quotes(ask: f64) -> HashMap<String,Quote> {
        HashMap::from([("up".into(),Quote {slug:"btc-updown-5m-6000".into(),token:"up".into(),
            outcome:"Up".into(),end_ms:6300000,bid:ask-0.01,ask,ask_size:100.0,min_size:5.0,
            exchange_ms:6240000,received_ms:6240000,mono_ms:1000})])
    }
    fn trend_ready() -> QualityEngine {
        let mut e=QualityEngine::new(trend_config());
        e.interval_start=Some(6300);e.open=Some(100000.0);
        for index in 0..61 {e.closes.push_back((2640+index*60,99900.0+index as f64*3.0));}
        e
    }
    fn trend_tick() -> Tick {Tick {venue:Venue::Kraken,bid:100100.0,ask:100100.0,
        exchange_ms:6480000,received_ms:6480000,mono_ms:1000}}
    fn trend_quotes(ask:f64) -> HashMap<String,Quote> {
        HashMap::from([("up".into(),Quote {slug:"btc-updown-15m-6300".into(),token:"up".into(),
            outcome:"Up".into(),end_ms:7200000,bid:ask-0.01,ask,ask_size:100.0,min_size:5.0,
            exchange_ms:6480000,received_ms:6480000,mono_ms:1000})])
    }
    #[test] fn config_rejects_bad_calibration_and_nan() {
        config().validate().unwrap();
        replica_config().validate().unwrap();
        trend_config().validate().unwrap();
        let mut c = config();c.min_edge=f64::NAN;assert!(c.validate().is_err());
        let mut c=config();c.probability_buckets[0].samples=2;assert!(c.validate().is_err());
    }
    #[test] fn trend_15m_emits_score_and_dynamic_budget() {
        let mut e=trend_ready();
        let signal=e.tick(&trend_tick(),&trend_quotes(0.55)).unwrap();
        assert_eq!(signal.slug,"btc-updown-15m-6300");
        let quality=signal.quality.unwrap();
        assert!(quality.probability_lower>=0.65);
        assert_eq!(quality.suggested_budget,Some(4.5));
        assert!(e.health()["last_score"]["rsi"].as_f64().unwrap()>50.0);
    }
    #[test] fn trend_15m_keeps_high_coverage_when_indicators_disagree() {
        let mut e=trend_ready();
        for (index,row) in e.closes.iter_mut().enumerate() {row.1=100200.0-index as f64*3.0;}
        assert!(e.tick(&trend_tick(),&trend_quotes(0.55)).is_some());
        assert_eq!(e.reason,"signal");
    }
    #[test] fn wallet_replica_requires_market_consensus_and_emits_once() {
        let mut e=ready();e.config=replica_config();
        assert!(e.tick(&tick(),&quotes(0.67)).is_none());
        assert_eq!(e.reason,"price_below_consensus_floor");
        let mut e=ready();e.config=replica_config();
        let signal=e.tick(&tick(),&quotes(0.72)).unwrap();
        assert_eq!(signal.outcome,"Up");
        assert!(signal.quality.unwrap().probability_lower>=0.80);
        assert!(e.tick(&tick(),&quotes(0.72)).is_none());
    }
    #[test] fn eth_replica_emits_only_for_the_eth_slug() {
        let mut e=ready();
        e.config=serde_json::from_str(include_str!("../../kraken-eth-replica.json")).unwrap();
        let mut eth_quotes=quotes(0.72);
        eth_quotes.get_mut("up").unwrap().slug="eth-updown-5m-6000".into();
        let signal=e.tick(&tick(),&eth_quotes).unwrap();
        assert_eq!(signal.slug,"eth-updown-5m-6000");
    }
    #[test] fn quality_emits_at_observation_and_deduplicates() {
        let mut e=ready();let signal=e.tick(&tick(),&quotes(0.55)).unwrap();
        assert_eq!(signal.outcome,"Up");assert!(signal.quality.unwrap().probability_lower>=0.65);
        assert_eq!(e.health()["decision_counts"]["signal"],1);
        assert!(e.tick(&tick(),&quotes(0.55)).is_none());
    }
    #[test] fn expensive_or_shallow_book_never_emits() {
        let mut e=ready();assert!(e.tick(&tick(),&quotes(0.80)).is_none());
        assert_eq!(e.reason,"price_above_budget_cap");
        let mut q=quotes(0.55);q.get_mut("up").unwrap().ask_size=1.0;
        assert!(e.tick(&tick(),&q).is_none());
    }
    #[test] fn reset_requires_fresh_warmup_and_open() {
        let mut e=ready();e.reset();assert!(e.tick(&tick(),&quotes(0.55)).is_none());
        assert_eq!(e.reason,"warming_up");
    }
    #[test] fn history_bootstrap_avoids_hour_long_warmup() {
        let mut e=QualityEngine::new(config());
        e.load_history((0..61).map(|index|(index*60,100000.0+index as f64)).collect());
        assert_eq!(e.closes.len(),61);assert_eq!(e.reason,"history_ready");
        assert_eq!(e.health()["history_source"],"kraken_rest_1m");
    }
    #[test] fn one_wide_tick_does_not_erase_history() {
        let mut e=ready();let mut t=tick();t.ask=100200.0;
        assert!(e.tick(&t,&quotes(0.55)).is_none());
        assert_eq!(e.closes.len(),61);assert_eq!(e.reason,"kraken_spread_too_wide");
    }
    #[test] fn window_or_reversal_blocks_entry() {
        let mut e=ready();let mut t=tick();t.received_ms+=6000;t.exchange_ms+=6000;
        assert!(e.tick(&t,&quotes(0.55)).is_none());
        let mut e=ready();e.closes[59].1=99900.0;
        assert!(e.tick(&tick(),&quotes(0.55)).is_none());assert_eq!(e.reason,"unstable_direction");
    }
    #[test] fn final_skip_reason_is_counted_once_per_market() {
        let mut e=ready();let mut t=tick();t.received_ms=6245000;t.exchange_ms=6245000;
        assert!(e.tick(&t,&quotes(0.80)).is_none());
        assert!(e.tick(&t,&quotes(0.80)).is_none());
        assert_eq!(e.health()["decision_counts"]["price_above_budget_cap"],1);
    }
}
