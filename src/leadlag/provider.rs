use super::types::*;
use std::collections::{HashMap, HashSet};

pub struct PaperProvider {
    pending: Option<Signal>,
    used: HashSet<String>,
    pub fills: usize,
    pub spent: f64,
    pub reserve: f64,
    max_spend: f64,
}
impl Default for PaperProvider {
    fn default() -> Self {
        Self::new(10.0)
    }
}
impl PaperProvider {
    pub fn new(max_spend: f64) -> Self {
        Self {
            pending: None,
            used: HashSet::new(),
            fills: 0,
            spent: 0.0,
            reserve: 0.0,
            max_spend,
        }
    }
    pub fn submit(&mut self, s: Signal) {
        if self.pending.is_none() && !self.used.contains(&s.slug) && self.spent < self.max_spend {
            self.pending = Some(s);
        }
    }
    pub fn advance(
        &mut self,
        now: u64,
        quotes: &HashMap<String, Quote>,
    ) -> Option<serde_json::Value> {
        let s = self.pending.as_ref()?;
        // Strategy-specific simulated order delay; never claim a real fill.
        if now.saturating_sub(s.mono_ms) < s.execution_delay_ms {
            return None;
        }
        let s = self.pending.take()?;
        let q = quotes.get(&s.token)?;
        let usable = if s.quality.is_some() || s.slug.starts_with("btc-updown-15m-") {
            q.liquid_and_fresh(now)
        } else {
            q.usable(now)
        };
        if q.slug != s.slug || !usable || q.ask > s.ask + 0.01 + 1e-9 {
            return None;
        }
        // Recheck expiry against elapsed monotonic time, even on a silent socket.
        if q.end_ms <= q.received_ms + now.saturating_sub(q.mono_ms) {
            return None;
        }
        let suggested = s.quality.as_ref().and_then(|quality| quality.suggested_budget);
        let requested = suggested.unwrap_or(5.0);
        // Match live_executor.buy(), which uses 80% of the all-in reservation.
        let budget = (requested*if suggested.is_some() {0.8} else {1.0})
            .min(self.max_spend - self.spent);
        let shares = (budget / q.ask).min(q.ask_size);
        if shares < q.min_size {
            return None;
        }
        let cost = shares * q.ask;
        self.spent += cost;
        self.reserve += cost * 0.02;
        self.fills += 1;
        self.used.insert(s.slug.clone());
        Some(
            serde_json::json!({"type":"paper_fill","slug":s.slug,"outcome":s.outcome,"token":s.token,
            "price":q.ask,"shares":shares,"cost":cost,"fee_reserve":cost*0.02,
            "trigger_to_paper_fill_ms":now.saturating_sub(s.trigger_ms),"mono_ms":now,
            "execution":"hypothetical_top_of_book","settled":false}),
        )
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn setup() -> (PaperProvider, HashMap<String, Quote>, Signal) {
        let q = Quote {
            slug: "m".into(),
            token: "up".into(),
            outcome: "Up".into(),
            end_ms: 20000,
            bid: 0.49,
            ask: 0.5,
            ask_size: 100.0,
            min_size: 5.0,
            received_ms: 1000,
            exchange_ms: 1000,
            mono_ms: 1000,
        };
        let s = Signal {
            token: "up".into(),
            slug: "m".into(),
            outcome: "Up".into(),
            ask: 0.5,
            mono_ms: 1000,
            trigger_ms: 990,
            received_ms: 1000,
            end_ms: 20000,
            execution_delay_ms: 20,
            quality: None,
            scenario: None,
            legs: None,
        };
        (
            PaperProvider::default(),
            HashMap::from([("up".into(), q)]),
            s,
        )
    }
    #[test]
    fn delays_and_deduplicates() {
        let (mut p, q, s) = setup();
        p.submit(s.clone());
        assert!(p.advance(1010, &q).is_none());
        assert!(p.advance(1020, &q).is_some());
        p.submit(s);
        assert!(p.advance(1040, &q).is_none());
        assert_eq!(p.spent, 5.0);
    }
    #[test]
    fn slippage_and_depth_guards() {
        let (mut p, mut q, s) = setup();
        q.get_mut("up").unwrap().ask = 0.52;
        p.submit(s.clone());
        assert!(p.advance(1020, &q).is_none());
        q.get_mut("up").unwrap().ask = 0.5;
        q.get_mut("up").unwrap().ask_size = 1.0;
        p.submit(s);
        assert!(p.advance(1020, &q).is_none());
    }
    #[test]
    fn never_fill_after_expiry_on_silent_socket() {
        let (mut p, mut q, s) = setup();
        q.get_mut("up").unwrap().end_ms = 1010;
        p.submit(s);
        assert!(p.advance(1020, &q).is_none());
    }
    #[test]
    fn clone_can_paper_fill_before_15m_market_start() {
        let (mut provider, mut quotes, mut signal) = setup();
        signal.slug = "btc-updown-15m-1800".into();
        signal.end_ms = 2_700_000;
        let quote = quotes.get_mut("up").unwrap();
        quote.slug = signal.slug.clone();
        quote.end_ms = signal.end_ms;
        provider.submit(signal);
        assert!(provider.advance(1020, &quotes).is_some());
    }
}
