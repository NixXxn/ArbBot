use serde::Deserialize;

#[derive(Clone, Debug)]
pub struct MarketUpdate {
    pub market_name: String,
    pub bid: f64,
    pub price: f64,
    pub size: f64,
    pub momentum: f64,
    pub sec_left: i64,
    pub price_history: Vec<u64>,
    pub question: String,
}

#[derive(Deserialize, Debug)]
pub struct Market {
    pub question: Option<String>,
    #[serde(rename = "ConditionId")]
    pub condition_id: Option<String>,
    #[serde(rename = "endDateIso")]
    pub end_date_iso: Option<String>,
    #[serde(rename = "bestBid")]
    pub best_bid: Option<f64>,
    #[serde(rename = "bestAsk")]
    pub best_ask: Option<f64>,
    pub spread: Option<f64>,
    #[serde(rename = "lastTradePrice")]
    pub last_trade_price: Option<f64>,
    #[serde(rename = "volume24hr")]
    pub volume_24hr: Option<f64>,
    #[serde(rename = "acceptingOrders")]
    pub accepting_orders: Option<bool>,
    #[serde(rename = "clobTokenIds")]
    pub clob_token_ids: Option<String>,
}

pub struct App {
    pub market_question: String,
    pub price_history: Vec<u64>,
    pub momentum: f64,
    pub secs_left: i64,
    pub bid: f64,
    pub price: f64,
    pub size: f64,
    pub logs: Vec<String>,
    pub should_quit: bool,
    pub status: String,
}

#[derive(Debug, Clone)]
pub struct PriceUpdate {
    pub assest: String,
    pub bid: f64,
    pub momentum: f64,
}
