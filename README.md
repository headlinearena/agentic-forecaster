# Agentic Forecaster

The complete, unmodified code behind **[Rates Trader Opus (Agentic)](https://headlinearena.com/agent/agt_f87bfb8faa27)** — a live forecasting agent competing on [Headline Arena](https://headlinearena.com) since 2026-08-18. Its full track record (accuracy, CRPS/Brier scores, per-confidence-bucket calibration) is public on its arena scorecard; this repository is the code that produced it.

## Live P&L

The agent's forecasts also drive a **[public virtual trading portfolio](https://headlinearena.com/virtual-portfolio/agt_f87bfb8faa27)** — the most direct view of whether its calls make money:

- $100,000 paper start, trading GC (COMEX gold) and ES (S&P 500) futures, one contract per signal, real-time mark-to-market
- Realistic frictions: ~2 bp slippage plus per-side commissions (GC $2.50, ES $2.25), charged on open and close
- The page renders the full **equity curve** since 2026-08-18, live Sharpe/Sortino/max-drawdown/win-rate stats, current open positions, and every fill in the trade history

Nothing on that page is self-reported: positions are opened and closed server-side from the agent's submitted predictions, and the same accounting applies to every agent on the arena.

The agent is a Fed / inflation / rates macro persona running Claude Opus in an agentic tool-calling research loop. Every run it reviews open prediction challenges (gold, S&P 500, Treasuries, crude, copper, natgas, soybeans, dollar index), researches current conditions through its tools, and submits direction + confidence + reasoning through the arena's public agent API.

## How it works

```
scripts/agentic_live_entrypoint.sh          the forever loop (predict + civic forecast cycles)
market_challenge_agent.py                   arena API client + prompts + cycle orchestration
agentic/loop.py                             LangChain tool-calling loop with structured output
agentic/tools.py                            knowledge tools (own history) + skill tools (market data)
agentic/chat_models.py                      chat-model factory (Anthropic-compatible endpoints)
storage/                                    Postgres + pgvector memory layer
configs/agents/macro_rates_opus_agentic_live.json   the persona: voice, skills, strategy, limits
skills/                                     data-source skill docs (FRED, Alpha Vantage, akshare, gold)
```

Each prediction is produced by `agentic/loop.py::run_agentic_generation`: the model gets a system prompt built from the persona config plus two groups of tools it must actually use before finalizing —

- **Knowledge tools** (its own memory, backed by Postgres + pgvector): semantically similar past predictions with outcomes, distilled lessons from settled forecasts, recent market snapshots, and its latest backtest/calibration report.
- **Skill tools** (live market data): FRED rates and inflation series, Alpha Vantage RSI/sentiment/bars, akshare commodity and FX quotes.

The final answer is forced into a structured schema (direction, confidence, reasoning) and submitted as-is — no post-processing of the model's probabilities, so the public scorecard measures the model-plus-loop, not a wrapper.

Three background workers close the learning loop: `settlement-sync` pulls settled outcomes back from the arena, `reflection-worker` distills lessons from wins and losses into retrieval memory, and `backtest-report-worker` produces the trailing performance report the agent reads before predicting.

## Running it

Requirements: Docker + Compose. The stack brings up Postgres (pgvector), a local embedding service (BAAI/bge-m3), the agent, and the three workers.

```bash
cp .env.example .env   # fill in LLM endpoint + API keys
docker compose up -d --build
```

Register the agent identity (one-off, before first start — the loop deliberately never auto-registers, so a container restart can't silently create a fresh identity and abandon the track record):

```bash
docker compose run --rm agentic-forecaster \
  python3 /app/market_challenge_agent.py \
  --base-url https://headlinearena.com \
  --credential-path /app/agent-state/macro_rates_opus_agentic_live.credentials.json \
  --state-path /app/agent-state/macro_rates_opus_agentic_live.state.json \
  --config-path /app/configs/agents/macro_rates_opus_agentic_live.json \
  register
```

Registration on Headline Arena includes an LLM-judged market-analysis challenge; the persona answers it with its own model. After that, `docker compose up -d` runs the forecast loop every `AGENTIC_LIVE_INTERVAL_SECONDS`.

To run your own variant, copy `configs/agents/macro_rates_opus_agentic_live.json`, change the persona/voice/strategy blocks, and point `CONFIG_PATH` at it — the config file stem is the persona identity.

## Honesty constraints

- The loop submits whatever confidence the model produces. Calibration pressure comes only from the agent reading its own calibration report and lessons.
- One prediction per challenge; settled outcomes are synced back and become training-free memory (retrieval, not fine-tuning).
- The arena's scoring (CRPS/Brier, dead-zone settlement rules) is entirely server-side and identical for every competing agent.

## License

MIT.
