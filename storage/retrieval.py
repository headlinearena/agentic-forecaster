import sys
from typing import Any

from sqlalchemy import text

from storage.db import get_engine
from storage.embeddings import compute_embedding, vector_literal


def get_recent_market_trend(
    asset_key: str | None = None, source: str | None = None, lookback_hours: int = 24
) -> list[dict[str, Any]]:
    conditions = ["fetched_at > now() - make_interval(hours => :lookback_hours)"]
    params: dict[str, Any] = {"lookback_hours": lookback_hours}
    if asset_key:
        conditions.append("asset_key = :asset_key")
        params["asset_key"] = asset_key
    if source:
        conditions.append("source = :source")
        params["source"] = source
    where_clause = " AND ".join(conditions)
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT source, purpose, payload, fetched_at FROM market_data_snapshot "
                    f"WHERE {where_clause} ORDER BY fetched_at DESC LIMIT 50"
                ),
                params,
            ).mappings().all()
        return [
            {
                "source": row["source"],
                "purpose": row["purpose"],
                "payload": row["payload"],
                "fetched_at": row["fetched_at"].isoformat(),
            }
            for row in rows
        ]
    except Exception as exc:
        print(f"get_recent_market_trend: failed to query for {asset_key}: {exc}", file=sys.stderr)
        return []


def get_similar_predictions(
    query_text: str, persona_id: str | None = None, asset_key: str | None = None, k: int = 5
) -> list[dict[str, Any]]:
    try:
        embedding = compute_embedding(query_text)
        if embedding is None:
            return []
        conditions = ["embedding IS NOT NULL"]
        params: dict[str, Any] = {"query_embedding": vector_literal(embedding), "k": k}
        if persona_id:
            conditions.append("persona_id = :persona_id")
            params["persona_id"] = persona_id
        if asset_key:
            conditions.append("asset_key = :asset_key")
            params["asset_key"] = asset_key
        where_clause = " AND ".join(conditions)
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT persona_id, challenge_id, asset_key, direction, confidence, outcome, created_at, model_output, "
                    f"embedding <=> CAST(:query_embedding AS vector) AS distance "
                    f"FROM prediction_history WHERE {where_clause} "
                    f"ORDER BY distance ASC LIMIT :k"
                ),
                params,
            ).mappings().all()
        return [
            {
                "persona_id": row["persona_id"],
                "challenge_id": row["challenge_id"],
                "asset_key": row["asset_key"],
                "direction": row["direction"],
                "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
                "outcome": row["outcome"],
                "created_at": row["created_at"].isoformat(),
                "distance": float(row["distance"]),
                "reasoning": (
                    row["model_output"].get("reasoning") if isinstance(row["model_output"], dict) else None
                ),
            }
            for row in rows
        ]
    except Exception as exc:
        print(f"get_similar_predictions: failed to query: {exc}", file=sys.stderr)
        return []


def get_relevant_lessons(
    query_text: str, persona_id: str | None = None, asset_key: str | None = None, k: int = 3
) -> list[dict[str, Any]]:
    try:
        embedding = compute_embedding(query_text)
        if embedding is None:
            return []
        conditions = ["embedding IS NOT NULL", "superseded_by IS NULL"]
        params: dict[str, Any] = {"query_embedding": vector_literal(embedding), "k": k}
        if persona_id:
            conditions.append("(persona_id = :persona_id OR persona_id IS NULL)")
            params["persona_id"] = persona_id
        if asset_key:
            conditions.append("(asset_key = :asset_key OR asset_key IS NULL)")
            params["asset_key"] = asset_key
        where_clause = " AND ".join(conditions)
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    f"SELECT persona_id, asset_key, lesson_text, sample_size, created_at, "
                    f"embedding <=> CAST(:query_embedding AS vector) AS distance "
                    f"FROM agent_lessons WHERE {where_clause} "
                    f"ORDER BY distance ASC LIMIT :k"
                ),
                params,
            ).mappings().all()
        return [
            {
                "persona_id": row["persona_id"],
                "asset_key": row["asset_key"],
                "lesson_text": row["lesson_text"],
                "sample_size": row["sample_size"],
                "created_at": row["created_at"].isoformat(),
                "distance": float(row["distance"]),
            }
            for row in rows
        ]
    except Exception as exc:
        print(f"get_relevant_lessons: failed to query: {exc}", file=sys.stderr)
        return []


def get_latest_backtest_report(persona_id: str) -> dict[str, Any] | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT persona_id, period_start, period_end, metrics, generated_at "
                    "FROM backtest_reports WHERE persona_id = :persona_id "
                    "ORDER BY generated_at DESC LIMIT 1"
                ),
                {"persona_id": persona_id},
            ).mappings().first()
        if row is None:
            return None
        return {
            "persona_id": row["persona_id"],
            "period_start": row["period_start"].isoformat(),
            "period_end": row["period_end"].isoformat(),
            "metrics": row["metrics"],
            "generated_at": row["generated_at"].isoformat(),
        }
    except Exception as exc:
        print(f"get_latest_backtest_report: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return None
