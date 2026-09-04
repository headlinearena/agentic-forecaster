# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A self-contained, live-trading agentic forecasting stack for [HeadlineArena](https://headlinearena.com):
one agent identity running an agentic tool-calling prediction loop (`predict-open-agentic-live`),
plus its own pgvector Postgres, embedding service, and three scheduled workers
(settlement-sync / reflection / backtest-report) that close the knowledge feedback loop.

## Public-repo rules (hard requirements)

Every commit here is immediately public. Never commit:
- real API keys, tokens, or the `.env` file (only `.env.example` belongs in git),
- private network addresses or internal hostnames of any deployment,
- internal vendor or infrastructure names not already public in this repo.

`DEFAULT_BASE_URL` stays `https://headlinearena.com`.

## Operating model

- This repo is the **single source of truth** for this agent's code and config. Deploy hosts run
  `git pull && docker compose build && docker compose up -d`.
- This codebase was originally extracted from a private multi-agent repository and now
  **evolves independently** — do not attempt to re-sync or merge with any other copy.
- The agent identity is registered once, stored Fernet-encrypted in this stack's Postgres
  (`AGENT_DB_ENCRYPTION_KEY`). The entrypoint deliberately never auto-registers: re-registering
  would abandon the identity's public track record. Treat the credentials row and the encryption
  key as inseparable.

## Commands

```bash
# validate
python3 -m py_compile market_challenge_agent.py

# build + run the full stack (agent + postgres + embedding + 3 workers)
docker compose build
docker compose up -d

# schema migrations
docker compose run --rm --entrypoint alembic agentic-forecaster upgrade head

# one-off manual cycle
docker compose run --rm --entrypoint /bin/sh agentic-forecaster -lc \
  'python3 /app/market_challenge_agent.py --base-url "$BASE_URL" \
   --credential-path "$CREDENTIAL_PATH" --state-path "$STATE_PATH" \
   --config-path "$CONFIG_PATH" predict-open-agentic-live --dry-run'
```

## Architecture notes

- All agent logic lives in `market_challenge_agent.py` (argparse CLI). The agentic loop
  (`agentic/loop.py`) is schema-parameterized and calls knowledge-retrieval and skill-fetch tools
  before producing a structured prediction.
- Storage modules under `storage/` use raw SQL against the stack's own Postgres; predictions and
  comments are embedded (BAAI/bge-m3 via `embedding_service/`) for similarity retrieval.
- Workers re-read nothing dynamic here: this stack serves exactly one persona, and the compose
  file wires each worker's subcommand directly.
