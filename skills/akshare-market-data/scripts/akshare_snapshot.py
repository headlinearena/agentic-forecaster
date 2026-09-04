#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, timezone
from typing import Any


ALLOWED_DATASETS = {
    "fx_spot_quote",
    "futures_foreign_commodity_realtime",
    "futures_zh_minute_sina",
}

SUPPORTED_MODES = {
    "market_breadth",
    "top_movers",
    "watchlist_quote",
    "futures_quote",
    "fx_quote",
    "kline_summary",
}

SYMBOL_COLUMNS = ["symbol", "代码", "编号", "货币对"]
NAME_COLUMNS = ["name", "名称"]
PRICE_COLUMNS = ["latest", "price", "最新价", "现价", "买报价", "卖报价"]
PCT_CHANGE_COLUMNS = ["pct_chg", "change_percent", "涨跌幅", "涨幅"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch one normalized AkShare market snapshot")
    parser.add_argument("--spec", help="JSON request spec")
    parser.add_argument("--spec-file", help="Path to a JSON request spec file")
    return parser.parse_args()


def load_spec(args: argparse.Namespace) -> dict[str, Any]:
    if args.spec:
        return json.loads(args.spec)
    if args.spec_file:
        with open(args.spec_file, "r", encoding="utf-8") as handle:
            return json.load(handle)
    raise SystemExit("Provide --spec or --spec-file")


def import_akshare():
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise RuntimeError("akshare is not installed; run `uv pip install akshare pandas`") from exc
    return ak


def find_column(columns: list[str], candidates: list[str]) -> str | None:
    lowered = {str(column).lower(): str(column) for column in columns}
    for candidate in candidates:
        if candidate in columns:
            return candidate
        resolved = lowered.get(candidate.lower())
        if resolved:
            return resolved
    return None


def numeric_value(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("%", "").strip()
    if not text or text in {"nan", "None", "-"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def filter_watchlist(records: list[dict[str, Any]], symbols: list[str]) -> list[dict[str, Any]]:
    if not symbols:
        return records
    wanted = {item.strip().lower() for item in symbols if item and item.strip()}
    filtered: list[dict[str, Any]] = []
    for row in records:
        haystacks = [
            str(row.get("symbol") or "").strip().lower(),
            str(row.get("name") or "").strip().lower(),
        ]
        if any(item in wanted for item in haystacks):
            filtered.append(row)
    return filtered


def canonicalize_records(df: Any, dataset: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", False):
        return []

    if dataset == "fx_spot_quote":
        records: list[dict[str, Any]] = []
        for raw in df.to_dict(orient="records"):
            row = {str(key): value for key, value in raw.items()}
            pair = str(row.get("货币对") or "").strip() or None
            bid = numeric_value(row.get("买报价"))
            ask = numeric_value(row.get("卖报价"))
            price = None
            if bid is not None and ask is not None:
                price = (bid + ask) / 2
            else:
                price = bid if bid is not None else ask
            records.append(
                {
                    "symbol": pair,
                    "name": pair,
                    "price": price,
                    "pct_change": None,
                    "raw": row,
                }
            )
        return records

    columns = [str(column) for column in df.columns]
    symbol_column = find_column(columns, SYMBOL_COLUMNS)
    name_column = find_column(columns, NAME_COLUMNS)
    price_column = find_column(columns, PRICE_COLUMNS)
    pct_change_column = find_column(columns, PCT_CHANGE_COLUMNS)

    records: list[dict[str, Any]] = []
    for raw in df.to_dict(orient="records"):
        row = {str(key): value for key, value in raw.items()}
        symbol = row.get(symbol_column) if symbol_column else None
        if dataset == "futures_foreign_commodity_realtime" and symbol is None:
            requested = (spec.get("params") or {}).get("symbol")
            if isinstance(requested, list) and len(requested) == len(df.index):
                symbol = requested[len(records)]
            elif isinstance(requested, str):
                symbol = requested
        records.append(
            {
                "symbol": symbol,
                "name": row.get(name_column) if name_column else None,
                "price": numeric_value(row.get(price_column)) if price_column else None,
                "pct_change": numeric_value(row.get(pct_change_column)) if pct_change_column else None,
                "raw": row,
            }
        )
    return records


def market_breadth_metric(key: str, label: str, source: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    changes = [item["pct_change"] for item in records if item.get("pct_change") is not None]
    if not changes:
        raise RuntimeError("No usable percent-change column was found for market_breadth")
    up_count = sum(1 for value in changes if value > 0)
    down_count = sum(1 for value in changes if value < 0)
    flat_count = sum(1 for value in changes if value == 0)
    return {
        key: {
            "label": label,
            "value": up_count / max(down_count, 1),
            "unit": "ratio",
            "timestamp": now_iso(),
            "source": source,
            "components": {
                "up_count": up_count,
                "down_count": down_count,
                "flat_count": flat_count,
            },
        }
    }


def top_movers_metric(key: str, label: str, source: str, records: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    ranked = [item for item in records if item.get("pct_change") is not None]
    if not ranked:
        raise RuntimeError("No usable percent-change column was found for top_movers")
    ranked.sort(key=lambda item: abs(item["pct_change"]), reverse=True)
    leaders = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "price": item.get("price"),
            "pct_change": item.get("pct_change"),
        }
        for item in ranked[:limit]
    ]
    return {
        key: {
            "label": label,
            "value": leaders[0].get("pct_change"),
            "unit": "%",
            "timestamp": now_iso(),
            "source": source,
            "leaders": leaders,
        }
    }


def quote_metric(key: str, label: str, source: str, records: list[dict[str, Any]], symbols: list[str], unit: str) -> dict[str, Any]:
    filtered = filter_watchlist(records, symbols)
    quotes = [
        {
            "symbol": item.get("symbol"),
            "name": item.get("name"),
            "price": item.get("price"),
            "pct_change": item.get("pct_change"),
        }
        for item in filtered[: max(len(symbols), 10) if symbols else 10]
    ]
    if not quotes:
        raise RuntimeError("Watchlist filter returned no matching rows")
    anchor_value = quotes[0].get("price")
    return {
        key: {
            "label": label,
            "value": anchor_value,
            "unit": unit,
            "timestamp": now_iso(),
            "source": source,
            "quotes": quotes,
        }
    }


def kline_summary_metric(
    key: str, label: str, source: str, df: Any, limit: int
) -> dict[str, Any]:
    """Compute trend/volume/OI summary from a minute-bar DataFrame."""
    df = df.tail(limit).reset_index(drop=True)
    n = len(df)
    if n == 0:
        raise RuntimeError("kline_summary: empty DataFrame after tail()")

    closes = [float(v) for v in df["close"]]
    volumes = [float(v) for v in df["volume"]]
    holds = [float(v) for v in df["hold"]]

    mid = n // 2
    first_mean = sum(closes[:mid]) / mid if mid else closes[0]
    last_mean = sum(closes[mid:]) / (n - mid) if (n - mid) else closes[-1]
    pct_diff = (last_mean - first_mean) / first_mean if first_mean else 0
    if pct_diff > 0.001:
        price_trend = "up"
    elif pct_diff < -0.001:
        price_trend = "down"
    else:
        price_trend = "flat"

    recent_vol = volumes[-3:] if len(volumes) >= 3 else volumes
    older_vol = volumes[:-3] if len(volumes) > 3 else volumes
    recent_avg = sum(recent_vol) / len(recent_vol)
    older_avg = sum(older_vol) / len(older_vol) if older_vol else recent_avg
    ratio = recent_avg / older_avg if older_avg else 1.0
    if ratio > 1.2:
        volume_signal = "expanding"
    elif ratio < 0.8:
        volume_signal = "shrinking"
    else:
        volume_signal = "neutral"

    oi_change = int(holds[-1] - holds[0])

    recent_slice = df.tail(3)
    recent_bars = [
        {
            "datetime": str(row["datetime"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": int(row["volume"]),
            "hold": int(row["hold"]),
        }
        for _, row in recent_slice.iterrows()
    ]

    return {
        key: {
            "label": label,
            "asset": source,
            "period": "60min",
            "bars_analyzed": n,
            "price_trend": price_trend,
            "volume_signal": volume_signal,
            "oi_change": oi_change,
            "recent_bars": recent_bars,
            "timestamp": now_iso(),
            "source": "futures_zh_minute_sina",
        }
    }


def fetch_dataframe(spec: dict[str, Any]) -> Any:
    dataset = str(spec.get("dataset") or "").strip()
    if dataset not in ALLOWED_DATASETS:
        raise RuntimeError(f"Unsupported dataset: {dataset}")
    ak = import_akshare()
    func = getattr(ak, dataset, None)
    if func is None:
        raise RuntimeError(f"AkShare does not expose dataset {dataset}")
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise RuntimeError("spec.params must be an object")
    return func(**params)


def build_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    key = str(spec.get("key") or "").strip()
    dataset = str(spec.get("dataset") or "").strip()
    mode = str(spec.get("mode") or "").strip()
    label = str(spec.get("label") or key or dataset).strip()
    unit = str(spec.get("unit") or "index").strip()
    symbols = spec.get("symbols") or []
    limit = int(spec.get("limit") or 5)

    if not key:
        raise RuntimeError("spec.key is required")
    if mode not in SUPPORTED_MODES:
        raise RuntimeError(f"Unsupported mode: {mode}")
    if not isinstance(symbols, list):
        raise RuntimeError("spec.symbols must be an array when provided")

    df = fetch_dataframe(spec)

    if mode == "kline_summary":
        if df is None or getattr(df, "empty", False):
            return {
                "provider": "akshare",
                "status": "empty",
                "metrics": {},
                "fetched_at": now_iso(),
            }
        metrics = kline_summary_metric(key, label or key, dataset, df, limit)
        return {
            "provider": "akshare",
            "status": "ok",
            "metrics": metrics,
            "fetched_at": now_iso(),
        }

    records = canonicalize_records(df, dataset, spec)
    if not records:
        return {
            "provider": "akshare",
            "status": "empty",
            "metrics": {},
            "fetched_at": now_iso(),
        }

    if mode == "market_breadth":
        metrics = market_breadth_metric(key, label or "Market breadth", dataset, records)
    elif mode == "top_movers":
        metrics = top_movers_metric(key, label or "Top movers", dataset, records, limit)
    elif mode in {"watchlist_quote", "futures_quote", "fx_quote"}:
        metrics = quote_metric(key, label or "Quotes", dataset, records, [str(item) for item in symbols], unit)
    else:
        raise RuntimeError(f"Unsupported mode: {mode}")

    return {
        "provider": "akshare",
        "status": "ok",
        "metrics": metrics,
        "fetched_at": now_iso(),
    }


def main() -> int:
    args = parse_args()
    try:
        spec = load_spec(args)
        snapshot = build_snapshot(spec)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "provider": "akshare",
                    "status": "error",
                    "error": str(exc),
                    "fetched_at": now_iso(),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
