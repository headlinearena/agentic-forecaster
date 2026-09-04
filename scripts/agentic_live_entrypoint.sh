#!/usr/bin/env bash
set -euo pipefail

cd /app

: "${BASE_URL:?BASE_URL is required}"
: "${CREDENTIAL_PATH:?CREDENTIAL_PATH is required}"
: "${STATE_PATH:?STATE_PATH is required}"
: "${CONFIG_PATH:?CONFIG_PATH is required}"

INTERVAL_SECONDS="${INTERVAL_SECONDS:-1800}"

mkdir -p "$(dirname "$CREDENTIAL_PATH")" "$(dirname "$STATE_PATH")"

# Unlike agent_entrypoint.sh, this does NOT auto-register on first run: credentials are
# Postgres-backed (storage/credentials.py), not written to CREDENTIAL_PATH, so an
# "if file missing, register" check here would be true on every container start/restart --
# silently re-registering a brand-new agent identity each time and abandoning any prior
# claim/track record. Registration for this persona is a deliberate one-off manual step
# (see CLAUDE.md), run once before this service is ever started.
base_args=(
  python3 /app/market_challenge_agent.py
  --base-url "$BASE_URL"
  --credential-path "$CREDENTIAL_PATH"
  --state-path "$STATE_PATH"
  --config-path "$CONFIG_PATH"
)

while true; do
  "${base_args[@]}" predict-open-agentic-live || echo "predict-open-agentic-live cycle failed" >&2
  # Config-gated: personas without a civic_forecast.enabled block skip this instantly.
  "${base_args[@]}" civic-forecast-agentic-live || echo "civic-forecast-agentic-live cycle failed" >&2
  sleep "$INTERVAL_SECONDS"
done
