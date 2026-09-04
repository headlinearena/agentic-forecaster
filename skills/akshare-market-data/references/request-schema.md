# Request Schema

Pass one request spec at a time to `scripts/akshare_snapshot.py`.

## Minimal Schema

```json
{
  "key": "fx_watchlist",
  "dataset": "fx_spot_quote",
  "mode": "fx_quote",
  "symbols": ["USD/CNY", "EUR/CNY", "100JPY/CNY"]
}
```

## Supported Fields

- `key`: stable metric identifier used by the caller.
- `dataset`: allowed AkShare function name from `endpoint-map.md`.
- `mode`: one of:
- `top_movers`
- `watchlist_quote`
- `futures_quote`
- `fx_quote`
- `params`: optional dict passed to the AkShare function.
- `symbols`: optional list of symbols or names for watchlist filtering.
- `limit`: optional integer for top-mover output size.
- `label`: optional human-readable label.
- `unit`: optional default unit.

## Output Shape

```json
{
  "provider": "akshare",
  "status": "ok",
  "metrics": {
    "a_share_breadth": {
      "label": "A-share market breadth",
      "value": 1.37,
      "unit": "ratio",
      "timestamp": "2026-03-25T03:30:00Z",
      "source": "stock_zh_a_spot_em",
      "components": {
        "up_count": 3000,
        "down_count": 2190,
        "flat_count": 95
      }
    }
  },
  "fetched_at": "2026-03-25T03:30:01Z"
}
```

## Design Notes

- Use compact scalar metrics plus a small `components` object.
- Avoid embedding full tables in the output.
- Prefer one request per call for deterministic behavior and easier caching.
