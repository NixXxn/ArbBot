use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum Venue {
    Kraken,
    Deribit,
    Binance,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Tick {
    pub venue: Venue,
    pub bid: f64,
    pub ask: f64,
    pub exchange_ms: u64,
    pub received_ms: u64,
    pub mono_ms: u64,
}
impl Tick {
    pub fn mid(&self) -> f64 {
        (self.bid + self.ask) / 2.0
    }
    pub fn valid(&self) -> bool {
        self.bid.is_finite()
            && self.ask.is_finite()
            && self.bid > 0.0
            && self.ask >= self.bid
            && self.exchange_ms <= self.received_ms + 250
            && self.received_ms.saturating_sub(self.exchange_ms) <= 500
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Quote {
    pub slug: String,
    pub token: String,
    pub outcome: String,
    pub end_ms: u64,
    pub bid: f64,
    pub ask: f64,
    pub ask_size: f64,
    pub min_size: f64,
    pub received_ms: u64,
    pub exchange_ms: u64,
    pub mono_ms: u64,
}
impl Quote {
    pub fn liquid_and_fresh(&self, now: u64) -> bool {
        [self.bid, self.ask, self.ask_size, self.min_size]
            .iter()
            .all(|v| v.is_finite())
            && self.min_size > 0.0
            && self.bid > 0.0
            && self.ask >= self.bid
            && self.ask < 1.0
            && self.ask - self.bid <= 0.02 + 1e-9
            && self.ask >= 0.1
            && self.ask <= 0.9
            && self.mono_ms <= now
            && now - self.mono_ms <= 500
            && self.received_ms.saturating_sub(self.exchange_ms) <= 500
            && self.exchange_ms <= self.received_ms + 250
            && self.end_ms > self.received_ms
    }
    pub fn usable(&self, now: u64) -> bool {
        self.liquid_and_fresh(now) && self.end_ms - self.received_ms <= 30000
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type")]
pub enum Event {
    Tick(Tick),
    KrakenHistory { closes: Vec<(u64, f64)> },
    Quote(Quote),
    Status { venue: String, state: String },
    Reset { venue: Venue },
}
impl Event {
    pub fn kind(&self) -> String {
        match self {
            Self::Tick(t) => format!("{:?}", t.venue),
            Self::KrakenHistory { .. } => "KrakenHistory".into(),
            Self::Quote(_) => "Polymarket".into(),
            Self::Status { venue, state } => format!("status:{venue}:{state}"),
            Self::Reset { venue } => format!("reset:{venue:?}"),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignalLeg {
    pub token: String,
    pub outcome: String,
    pub ask: f64,
    /// `primary` is bought first (Kraken-riskier side); `hedge` is the second leg.
    pub role: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Signal {
    pub token: String,
    pub slug: String,
    pub outcome: String,
    pub ask: f64,
    pub mono_ms: u64,
    pub trigger_ms: u64,
    pub received_ms: u64,
    pub end_ms: u64,
    #[serde(default = "default_execution_delay_ms")]
    pub execution_delay_ms: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub quality: Option<SignalQuality>,
    /// `directional` = Scenario A; `hedge_merge` = Scenario B (both legs + early merge).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scenario: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub legs: Option<Vec<SignalLeg>>,
}
fn default_execution_delay_ms() -> u64 {20}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignalQuality {
    pub probability_lower: f64,
    pub move_bp: f64,
    pub z: f64,
    pub fee_rate_ceiling: f64,
    pub min_edge: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub suggested_budget: Option<f64>,
}
