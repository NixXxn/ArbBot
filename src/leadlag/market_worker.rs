use super::types::*;
use crate::{clock_ms, mono_ms};
use futures_util::{future::join_all, SinkExt, StreamExt};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashMap},
    time::Duration,
};
use tokio::sync::mpsc::Sender;
use tokio_tungstenite::{connect_async, tungstenite::Message};

fn number(v: &Value) -> f64 {
    v.as_f64()
        .or_else(|| v.as_str().and_then(|s| s.parse().ok()))
        .unwrap_or(f64::NAN)
}
fn price_key(p: f64) -> u64 {
    (p * 100000000.0).round() as u64
}
#[derive(Default)]
struct Book {
    bids: BTreeMap<u64, f64>,
    asks: BTreeMap<u64, f64>,
}
impl Book {
    fn update(&mut self, side: &str, p: f64, q: f64) {
        if !p.is_finite() || !q.is_finite() || p <= 0.0 || q < 0.0 {
            return;
        }
        let book = match side {
            "buy" | "BUY" => &mut self.bids,
            "sell" | "SELL" => &mut self.asks,
            _ => return,
        };
        if q == 0.0 {
            book.remove(&price_key(p));
        } else {
            book.insert(price_key(p), q);
        }
    }
    fn best(&self) -> Option<(f64, f64, f64)> {
        let (bid, _) = self.bids.last_key_value()?;
        let (ask, size) = self.asks.first_key_value()?;
        Some((*bid as f64 / 1e8, *ask as f64 / 1e8, *size))
    }
    fn snapshot(&mut self, v: &Value) {
        self.bids.clear();
        self.asks.clear();
        for (field, side) in [("bids", "buy"), ("asks", "sell")] {
            if let Some(levels) = v[field].as_array() {
                for l in levels {
                    self.update(
                        side,
                        number(&l["price"]),
                        number(l.get("qty").unwrap_or(&l["size"])),
                    );
                }
            }
        }
    }
}
async fn status(tx: &Sender<Event>, venue: &str, state: &str) {
    let _ = tx
        .send(Event::Status {
            venue: venue.into(),
            state: state.into(),
        })
        .await;
}

fn kraken_product(asset: &str) -> &'static str {
    match asset {
        "eth" => "PF_ETHUSD",
        "sol" => "PF_SOLUSD",
        _ => "PF_XBTUSD",
    }
}

async fn kraken_history(tx: &Sender<Event>, asset: &str) {
    let now = clock_ms() / 1000;
    let current_minute = now / 60 * 60;
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap();
    let response = client
        .get(format!("https://futures.kraken.com/api/charts/v1/trade/{}/1m", kraken_product(asset)))
        .query(&[
            ("from", current_minute.saturating_sub(75 * 60).to_string()),
            ("to", current_minute.saturating_sub(1).to_string()),
            ("count", "75".into()),
        ])
        .send()
        .await;
    let Ok(response) = response else {
        status(tx, "Kraken", "history_fetch_failed").await;
        return;
    };
    let Ok(value) = response.json::<Value>().await else {
        status(tx, "Kraken", "history_parse_failed").await;
        return;
    };
    let mut closes: Vec<(u64, f64)> = value["candles"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let minute = row["time"].as_u64()? / 1000;
            let close = number(&row["close"]);
            (minute < current_minute && close.is_finite() && close > 0.0)
                .then_some((minute, close))
        })
        .collect();
    closes.sort_by_key(|row| row.0);
    closes.dedup_by_key(|row| row.0);
    if closes.len() > 61 {
        closes = closes.split_off(closes.len() - 61);
    }
    let contiguous = closes.len() == 61
        && closes.windows(2).all(|rows| rows[1].0 == rows[0].0 + 60)
        && closes.last().is_some_and(|row| row.0 + 60 == current_minute);
    if !contiguous {
        status(tx, "Kraken", "history_incomplete").await;
        return;
    }
    let _ = tx.send(Event::KrakenHistory { closes }).await;
    status(tx, "Kraken", "history_ready").await;
}

pub async fn exchange(venue: Venue, tx: Sender<Event>, quality_history: bool, asset: String) {
    let name = format!("{venue:?}");
    let url = match venue {
        Venue::Kraken => "wss://futures.kraken.com/ws/v1",
        Venue::Deribit => "wss://www.deribit.com/ws/api/v2",
        Venue::Binance => "wss://fstream.binance.com/ws/btcusdt@bookTicker",
    };
    loop {
        let _ = tx.send(Event::Reset { venue }).await;
        if venue == Venue::Kraken && quality_history {
            kraken_history(&tx, &asset).await;
        }
        let socket = tokio::time::timeout(Duration::from_secs(10), connect_async(url)).await;
        let Ok(Ok((mut ws, _))) = socket else {
            status(&tx, &name, "connect_failed").await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        };
        let subscription = match venue {
            Venue::Kraken => {
                Some(json!({"event":"subscribe","feed":"book","product_ids":[kraken_product(&asset)]}))
            }
            Venue::Deribit => Some(
                json!({"jsonrpc":"2.0","id":1,"method":"public/subscribe","params":{"channels":["quote.BTC-PERPETUAL"]}}),
            ),
            Venue::Binance => None,
        };
        if let Some(sub) = subscription {
            if ws.send(Message::Text(sub.to_string())).await.is_err() {
                continue;
            }
        }
        status(&tx, &name, "connected").await;
        let mut book = Book::default();
        let mut seq = None;
        loop {
            let msg = tokio::time::timeout(Duration::from_secs(15), ws.next()).await;
            let Ok(Some(Ok(msg))) = msg else {
                break;
            };
            let wall = clock_ms();
            let mono = mono_ms();
            let text = match msg {
                Message::Ping(p) => {
                    let _ = ws.send(Message::Pong(p)).await;
                    continue;
                }
                Message::Text(t) => t,
                Message::Close(_) => break,
                _ => continue,
            };
            let Ok(v) = serde_json::from_str::<Value>(&text) else {
                continue;
            };
            if v.get("error").is_some()
                || v["event"] == "error"
                || v["event"] == "subscribed_failed"
            {
                status(&tx, &name, "subscription_error").await;
                break;
            }
            // Kraken acknowledgements carry feed="book" but are not book deltas.
            if v["event"].is_string() {
                continue;
            }
            let parsed = match venue {
                Venue::Kraken => {
                    if v["feed"] == "book_snapshot" {
                        book.snapshot(&v);
                        seq = v["seq"].as_u64();
                    } else if v["feed"] == "book" {
                        let next = v["seq"].as_u64();
                        if seq.is_none() || next != seq.map(|s| s + 1) {
                            status(&tx, &name, &format!("sequence_gap:{seq:?}:{next:?}")).await;
                            break;
                        }
                        seq = next;
                        book.update(
                            v["side"].as_str().unwrap_or(""),
                            number(&v["price"]),
                            number(&v["qty"]),
                        );
                    } else {
                        continue;
                    }
                    book.best()
                        .map(|(bid, ask, _)| (bid, ask, v["timestamp"].as_u64().unwrap_or(0)))
                }
                Venue::Deribit => {
                    let d = &v["params"]["data"];
                    if d.is_null() {
                        continue;
                    }
                    Some((
                        number(&d["best_bid_price"]),
                        number(&d["best_ask_price"]),
                        d["timestamp"].as_u64().unwrap_or(0),
                    ))
                }
                Venue::Binance => Some((
                    number(&v["b"]),
                    number(&v["a"]),
                    v["T"].as_u64().unwrap_or(0),
                )),
            };
            if let Some((bid, ask, exchange_ms)) = parsed {
                if tx
                    .send(Event::Tick(Tick {
                        venue,
                        bid,
                        ask,
                        exchange_ms,
                        received_ms: wall,
                        mono_ms: mono,
                    }))
                    .await
                    .is_err()
                {
                    return;
                }
            }
        }
        status(&tx, &name, "disconnected").await;
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

pub async fn polymarket(tx: Sender<Event>, interval_seconds: u64, upcoming: bool, asset: String,
                        continuous_rest: bool) {
    let client = reqwest::Client::builder()
        .no_proxy()
        .timeout(Duration::from_secs(10))
        .build()
        .unwrap();
    loop {
        let now_seconds = clock_ms() / 1000;
        let start = if upcoming {
            (now_seconds / interval_seconds + 1) * interval_seconds
        } else {
            now_seconds / interval_seconds * interval_seconds
        };
        let slug = format!("{asset}-updown-{}m-{start}", interval_seconds / 60);
        let end_ms = (start + interval_seconds) * 1000;
        let rollover_ms = if upcoming { start * 1000 } else { end_ms };
        let res = client
            .get("https://gamma-api.polymarket.com/markets")
            .query(&[("slug", &slug)])
            .send()
            .await;
        let markets = match res {
            Ok(r) if r.status().is_success() => r.json::<Vec<Value>>().await.unwrap_or_default(),
            _ => Vec::new(),
        };
        let Some(m) = markets.first() else {
            status(&tx, "Polymarket", "market_missing").await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        };
        if m["closed"] == true || m["acceptingOrders"] != true {
            status(&tx, "Polymarket", "not_accepting_orders").await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        }
        let array = |field: &str| -> Vec<String> {
            if let Some(s) = m[field].as_str() {
                serde_json::from_str(s).unwrap_or_default()
            } else {
                serde_json::from_value(m[field].clone()).unwrap_or_default()
            }
        };
        let ids = array("clobTokenIds");
        let outcomes = array("outcomes");
        if ids.len() != 2
            || outcomes.len() != 2
            || !outcomes.iter().any(|o| o == "Up")
            || !outcomes.iter().any(|o| o == "Down")
        {
            status(&tx, "Polymarket", "invalid_outcomes").await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        }
        let socket = tokio::time::timeout(
            Duration::from_secs(10),
            connect_async("wss://ws-subscriptions-clob.polymarket.com/ws/market"),
        )
        .await;
        let Ok(Ok((mut ws, _))) = socket else {
            status(&tx, "Polymarket", "connect_failed").await;
            tokio::time::sleep(Duration::from_secs(2)).await;
            continue;
        };
        if ws
            .send(Message::Text(
                json!({"type":"market","assets_ids":ids}).to_string(),
            ))
            .await
            .is_err()
        {
            continue;
        }
        status(&tx, "Polymarket", "connected").await;
        let mut books: HashMap<String, Book> = HashMap::new();
        let mut minimums: HashMap<String, f64> = HashMap::new();
        let mut heartbeat = tokio::time::interval(Duration::from_secs(10));
        let mut rest_poll = tokio::time::interval(Duration::from_secs(2));
        let mut expiry = tokio::time::interval(Duration::from_millis(100));
        loop {
            tokio::select! {
                _=expiry.tick()=> {if clock_ms()>=rollover_ms {break;}}
                _=heartbeat.tick()=> {if ws.send(Message::Text("PING".into())).await.is_err() {break;}}
                _=rest_poll.tick()=> {
                    // A connected CLOB websocket can occasionally stop delivering
                    // books without closing. During the decision window, refresh both
                    // outcomes from the public REST book so one silent socket does not
                    // erase an otherwise valid signal.
                    let seconds=clock_ms()/1000;
                    let remaining=(start+interval_seconds).saturating_sub(seconds);
                    let (target,window)=if interval_seconds==900 {(720,30)} else {(60,5)};
                    if continuous_rest || (remaining<=target && remaining>=target-window) {
                        let requests=ids.iter().map(|id| {
                            let client=client.clone();
                            async move {
                            let response=client.get("https://clob.polymarket.com/book")
                                .query(&[("token_id",id)]).send().await.ok()?;
                            if !response.status().is_success() {return None;}
                            response.json::<Value>().await.ok()
                        }});
                        let payloads=join_all(requests).await;
                        for (index,payload) in payloads.into_iter().enumerate() {
                            let Some(payload)=payload else {continue;};
                            let mut book=Book::default();book.snapshot(&payload);
                            let Some((bid,ask,ask_size))=book.best() else {continue;};
                            let wall=clock_ms();let mono=mono_ms();
                            let min=number(payload.get("min_order_size").unwrap_or(&m["orderMinSize"]));
                            let q=Quote {slug:slug.clone(),token:ids[index].clone(),outcome:outcomes[index].clone(),
                                end_ms,bid,ask,ask_size,min_size:min,received_ms:wall,exchange_ms:wall,mono_ms:mono};
                            if tx.send(Event::Quote(q)).await.is_err() {return;}
                        }
                    }
                }
                msg=ws.next()=> {
                    let Some(Ok(msg))=msg else {break;};
                    let wall=clock_ms();let mono=mono_ms();
                    let text=match msg {Message::Text(t)=>t,Message::Ping(p)=>{let _=ws.send(Message::Pong(p)).await;continue;},Message::Close(_)=>break,_=>continue};
                    let Ok(v)=serde_json::from_str::<Value>(&text) else {continue;};
                    let events=if let Some(a)=v.as_array() {a.clone()} else {vec![v]};
                    for event in events {
                        let timestamp=event["timestamp"].as_str().and_then(|s|s.parse().ok()).or_else(||event["timestamp"].as_u64()).unwrap_or(0);
                        let mut touched=Vec::new();
                        if event["event_type"]=="book" {
                            if let Some(id)=event["asset_id"].as_str() {
                                books.entry(id.into()).or_default().snapshot(&event);
                                let min=number(event.get("min_order_size").unwrap_or(&m["orderMinSize"]));
                                minimums.insert(id.into(),min);touched.push(id.to_string());
                            }
                        } else if event["event_type"]=="price_change" {
                            if let Some(changes)=event["price_changes"].as_array() {
                                for c in changes {
                                    if let Some(id)=c["asset_id"].as_str() {
                                        if let Some(book)=books.get_mut(id) {book.update(c["side"].as_str().unwrap_or(""),number(&c["price"]),number(&c["size"]));touched.push(id.to_string());}
                                    }
                                }
                            }
                        }
                        touched.sort();touched.dedup();
                        for id in touched {
                            let Some(index)=ids.iter().position(|i|i==&id) else {continue;};
                            if let Some((bid,ask,ask_size))=books.get(&id).and_then(Book::best) {
                                let q=Quote {slug:slug.clone(),token:id.clone(),outcome:outcomes[index].clone(),end_ms,bid,ask,ask_size,
                                    min_size:*minimums.get(&id).unwrap_or(&f64::NAN),received_ms:wall,exchange_ms:timestamp,mono_ms:mono};
                                if tx.send(Event::Quote(q)).await.is_err() {return;}
                            }
                        }
                    }
                }
            }
        }
        status(&tx, "Polymarket", "disconnected").await;
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn delta_deletion_updates_best_prices() {
        let mut b = Book::default();
        b.snapshot(&json!({"bids":[{"price":100,"qty":1},{"price":99,"qty":3}],"asks":[{"price":101,"qty":2}]}));
        assert_eq!(b.best(), Some((100.0, 101.0, 2.0)));
        b.update("buy", 100.0, 0.0);
        assert_eq!(b.best().unwrap().0, 99.0);
    }
    #[test]
    fn snapshot_clears_old_depth() {
        let mut b = Book::default();
        b.update("buy", 100.0, 5.0);
        b.snapshot(&json!({"bids":[],"asks":[]}));
        assert!(b.best().is_none());
    }
    #[test]
    fn kraken_products_are_asset_specific() {
        assert_eq!(kraken_product("btc"), "PF_XBTUSD");
        assert_eq!(kraken_product("eth"), "PF_ETHUSD");
        assert_eq!(kraken_product("sol"), "PF_SOLUSD");
    }
}
