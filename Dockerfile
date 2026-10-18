# Arbot: Python paper/dashboard image (no Rust leadlag — arb scan uses Gamma/CLOB HTTP).
FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BOT_DATA_DIR=/data BOT_MODE=dashboard
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates libssl3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY scripts /app/scripts
COPY config /app/config
RUN chmod 755 /app/scripts/container-entrypoint.sh \
    && printf '#!/bin/sh\necho "leadlag unused in Arbot; use run_arb_paper"\nexit 0\n' > /app/leadlag \
    && chmod 755 /app/leadlag
VOLUME ["/data"]
EXPOSE 8080
ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD ["python", "/app/scripts/container_healthcheck.py"]
