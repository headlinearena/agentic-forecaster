# Endpoint Map

Use a small curated subset of AkShare functions. Extend this file only when a new use case is recurring and a stable normalization path exists.

## Production Whitelist

These endpoints have been verified against the packaged runtime and should be preferred for automated agents.

## Global Commodity / International Futures

- `futures_foreign_commodity_realtime`
  - Use for: internationally relevant commodity and futures spot checks.
  - Good modes: `futures_quote`, `watchlist_quote`.
  - Notes: requires `params.symbol`; use `futures_foreign_commodity_subscribe_exchange_symbol()` to discover valid codes.

## FX

- `fx_spot_quote`
  - Use for: FX spot watchlists and broad currency context.
  - Good modes: `fx_quote`, `watchlist_quote`.

## CN Futures Intraday

- `futures_zh_minute_sina`
  - Use for: CN futures 60min K-line, open interest, and volume for AU/AG/IF/IC.
  - Good modes: `kline_summary`.
  - Notes: `params.symbol` uses continuous contract codes (AU0, AG0, IF0, IC0);
    `params.period` is a string ("60" for 60-minute bars).
    Returns ~1000 historical bars; wrapper takes `tail(limit)` internally.

## Experimental / Probe Before Use

- `stock_zh_a_spot_em`
- `stock_hk_spot_em`
- `stock_us_spot_em`
- `stock_zh_index_spot_em`
- `index_global_spot_em`
- `futures_zh_spot`

These may exist in AkShare, but should not be enabled for automated agents until they pass in-container probes consistently.

## Not In Scope For Current Agent V1

- Arbitrary AkShare function execution.
- Raw financial statement or fund-holdings workflows.
- Large historical batch downloads.
- Endpoints that require captcha workarounds, browser emulation, or unstable scraping paths.
