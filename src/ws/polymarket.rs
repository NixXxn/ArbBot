use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::connect_async;
use tungstenite::Message;

#[tokio::main]
async fn main() {
    let url = "wss://example.com/ws";

    let (ws_stream, _) = connect_async(url)
        .await
        .expect("Failed to connect");

    println!("Connected");

    let (mut write, mut read) = ws_stream.split();

    // send subscription
    let sub_msg = r#"{
        "type": "subscribe",
        "channel": "prices"
    }"#;

    write
        .send(Message::Text(sub_msg.into()))
        .await
        .unwrap();

    // receive loop
    while let Some(msg) = read.next().await {
        match msg {
            Ok(Message::Text(text)) => {
                println!("recv: {}", text);
            }

            Ok(Message::Ping(_)) => {
                println!("ping");
            }

            Ok(_) => {}

            Err(e) => {
                println!("error: {:?}", e);
                break;
            }
        }
    }
}