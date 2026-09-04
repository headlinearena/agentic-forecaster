#!/usr/bin/env bash
set -euo pipefail

cd /app

: "${BASE_URL:?BASE_URL is required}"
: "${WORKER_SUBCOMMAND:?WORKER_SUBCOMMAND is required}"

FINANCIAL_PERSONAS_FILE="${FINANCIAL_PERSONAS_FILE:-/app/configs/financial_personas/financial_personas.txt}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-3600}"

mkdir -p /app/agent-state

while true; do
  if [ ! -f "$FINANCIAL_PERSONAS_FILE" ]; then
    echo "Missing $FINANCIAL_PERSONAS_FILE, skipping this cycle" >&2
    sleep "$INTERVAL_SECONDS"
    continue
  fi

  # Re-read the file every cycle (not just at container start) so pause/resume
  # takes effect on the next cycle without needing any docker command.
  while IFS= read -r line || [ -n "$line" ]; do
    persona="$(echo "$line" | sed 's/#.*//' | xargs)"
    [ -z "$persona" ] && continue

    config_path="/app/configs/agents/${persona}.json"
    if [ ! -f "$config_path" ]; then
      echo "Skipping unknown persona config: $config_path" >&2
      continue
    fi
    python3 /app/market_challenge_agent.py \
      --base-url "$BASE_URL" \
      --credential-path "/app/agent-state/${persona}.${WORKER_SUBCOMMAND}.credentials.json" \
      --state-path "/app/agent-state/${persona}.${WORKER_SUBCOMMAND}.state.json" \
      --config-path "$config_path" \
      "$WORKER_SUBCOMMAND" || echo "worker cycle failed for persona=$persona subcommand=$WORKER_SUBCOMMAND" >&2
  done < "$FINANCIAL_PERSONAS_FILE"

  sleep "$INTERVAL_SECONDS"
done
