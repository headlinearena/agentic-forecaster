---
name: akshare-market-data
description: Fetch and normalize token-free real-time and recent market data with AkShare for China and global markets. Use when Codex needs live or recent FX quotes, international commodity and futures snapshots, or a compact market snapshot to support trading commentary, macro analysis, watchlists, or agent prompts. Prefer this skill over ad hoc AkShare calls when repeatability, constrained endpoints, or normalized JSON output matters.
---

# AkShare Market Data

## Overview

Use AkShare through a constrained wrapper, not by inventing arbitrary library calls. Favor deterministic JSON snapshots that can be cached, summarized, and injected into prompts.

AkShare covers both China and global market data. In this skill, the production whitelist is intentionally narrower: it currently focuses on verified FX and international commodity / futures snapshots. China and equity endpoints should be re-added only after in-container probes pass reliably.

## Workflow

1. Choose the business intent first.
2. Map the intent to an allowed dataset in [references/endpoint-map.md](references/endpoint-map.md).
3. Build a constrained request spec using [references/request-schema.md](references/request-schema.md).
4. Run `scripts/akshare_snapshot.py` to fetch and normalize the data.
5. Use the normalized output for analysis, prompt context, or agent state.

## Rules

- Use the wrapper scripts for repeatable tasks instead of direct one-off Python snippets.
- Keep requests narrow and explicit. Prefer `market_breadth`, `top_movers`, `watchlist_quote`, `futures_quote`, or `fx_quote`.
- Return compact JSON, not full DataFrame dumps.
- Treat AkShare field names as unstable across endpoints. Apply the normalization rules in [references/normalization-rules.md](references/normalization-rules.md).
- Use AkShare for token-free market data; use other providers when the user needs a source AkShare does not reliably expose.

## Quick Start

Install the dependency if needed:

```bash
uv pip install akshare pandas
```

Probe a dataset during development:

```bash
python3 scripts/akshare_probe.py --dataset stock_zh_a_spot_em
```

Fetch normalized A-share breadth:

```bash
python3 scripts/akshare_snapshot.py --spec '{
    "key": "fx_watchlist",
    "dataset": "fx_spot_quote",
    "mode": "fx_quote",
    "symbols": ["USD/CNY", "EUR/CNY", "100JPY/CNY"]
}'
```

Fetch normalized global commodity / futures watchlist quotes:

```bash
python3 scripts/akshare_snapshot.py --spec '{
  "key": "global_commodity_watchlist",
  "dataset": "futures_foreign_commodity_realtime",
  "mode": "watchlist_quote",
  "params": {"symbol": ["HG", "NG", "CAD"]},
  "symbols": ["HG", "NG", "CAD"]
}'
```

## References

- Use [references/endpoint-map.md](references/endpoint-map.md) to pick the allowed AkShare dataset.
- Use [references/request-schema.md](references/request-schema.md) to shape requests.
- Use [references/normalization-rules.md](references/normalization-rules.md) when extending the wrapper.

## Scripts

- `scripts/akshare_snapshot.py`: constrained wrapper that fetches data and emits normalized JSON.
- `scripts/akshare_probe.py`: small inspection tool for verifying dataset availability and column names before extending the wrapper.

Extend the wrapper by adding a new allowed dataset and normalization path instead of exposing arbitrary AkShare calls. Before enabling a new dataset in agent configs, probe it inside the packaged runtime and verify both availability and field stability.
