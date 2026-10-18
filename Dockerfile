FROM rust:1.90-bookworm AS rust-builder
WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src ./src
COPY kraken-quality.json kraken-replica.json kraken-eth-replica.json kraken-trend-btc.json kraken-trend-eth.json ./
RUN cargo build --locked --release --bin leadlag

FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOT_DATA_DIR=/data BOT_MODE=dashboard
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --from=rust-builder /build/target/release/leadlag /app/leadlag
COPY scripts /app/scripts
COPY config /app/config
COPY kraken-quality.json kraken-replica.json kraken-eth-replica.json kraken-trend-btc.json kraken-trend-eth.json ./
RUN chmod 755 /app/leadlag /app/scripts/container-entrypoint.sh
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "/app/scripts/container_healthcheck.py"]
