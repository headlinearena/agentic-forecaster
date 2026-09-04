import json
import math
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from storage.db import get_engine


def get_cached_snapshot(source: str, purpose: str, request_hash: str, ttl_seconds: int) -> dict[str, Any] | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT payload FROM market_data_snapshot
                    WHERE source = :source AND purpose = :purpose AND request_hash = :request_hash
                      AND fetched_at > now() - make_interval(secs => :ttl_seconds)
                    ORDER BY fetched_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "source": source,
                    "purpose": purpose,
                    "request_hash": request_hash,
                    "ttl_seconds": ttl_seconds,
                },
            ).first()
    except (SQLAlchemyError, RuntimeError) as exc:
        print(f"get_cached_snapshot: failed to read cache for {source}/{purpose}: {exc}", file=sys.stderr)
        return None
    if row is None:
        return None
    return row[0]


def _json_safe(value: Any) -> Any:
    # json.dumps emits bare NaN/Infinity tokens by default, which Postgres jsonb rejects.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def store_snapshot(source: str, asset_key: str, purpose: str, request_hash: str, payload: dict[str, Any]) -> None:
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO market_data_snapshot (source, asset_key, purpose, payload, request_hash, fetched_at)
                    VALUES (:source, :asset_key, :purpose, CAST(:payload AS jsonb), :request_hash, now())
                    """
                ),
                {
                    "source": source,
                    "asset_key": asset_key,
                    "purpose": purpose,
                    "payload": json.dumps(_json_safe(payload), ensure_ascii=False, default=str),
                    "request_hash": request_hash,
                },
            )
    except (SQLAlchemyError, RuntimeError) as exc:
        print(f"store_snapshot: failed to write snapshot for {source}/{purpose}: {exc}", file=sys.stderr)
