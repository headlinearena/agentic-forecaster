FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/agent

WORKDIR /app

COPY requirements.txt ./

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && python3 -m pip install --no-cache-dir -r requirements.txt \
    && mkdir -p "$HOME" \
    && chown -R 1000:1000 "$HOME" \
    && rm -rf /var/lib/apt/lists/*

COPY market_challenge_agent.py ./
COPY configs ./configs
COPY skills ./skills
COPY scripts ./scripts
COPY storage ./storage
COPY agentic ./agentic
COPY alembic ./alembic
COPY alembic.ini ./

RUN chmod +x /app/scripts/agentic_live_entrypoint.sh \
    && chmod +x /app/scripts/worker_entrypoint.sh

ENTRYPOINT ["/bin/bash", "/app/scripts/agentic_live_entrypoint.sh"]
