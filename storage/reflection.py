import sys
from typing import Any

from sqlalchemy import text

from storage.db import get_engine
from storage.embeddings import compute_embedding, vector_literal

MIN_SAMPLE_SIZE = 3


def select_settled_predictions_for_reflection(persona_id: str, lookback_hours: int = 24) -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, asset_key, strategy, direction, confidence, outcome, model_output, created_at "
                    "FROM prediction_history "
                    "WHERE persona_id = :persona_id AND settled_at IS NOT NULL "
                    "AND settled_at > now() - make_interval(hours => :lookback_hours) "
                    "ORDER BY asset_key, strategy, created_at"
                ),
                {"persona_id": persona_id, "lookback_hours": lookback_hours},
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        print(f"select_settled_predictions_for_reflection: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return []


def group_by_asset_and_strategy(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        key = (row["asset_key"], row["strategy"])
        groups.setdefault(key, []).append(row)
    return groups


def save_lesson(
    persona_id: str,
    asset_key: str,
    strategy_scope: str,
    lesson_text: str,
    supporting_prediction_ids: list[str],
    sample_size: int,
) -> None:
    try:
        embedding = None
        try:
            embedding = compute_embedding(lesson_text)
        except Exception as exc:
            print(f"save_lesson: failed to compute embedding for {persona_id}/{asset_key}/{strategy_scope}: {exc}", file=sys.stderr)

        engine = get_engine()
        with engine.begin() as conn:
            new_id = conn.execute(
                text(
                    "INSERT INTO agent_lessons "
                    "(persona_id, asset_key, strategy_scope, lesson_text, embedding, supporting_prediction_ids, sample_size, created_at) "
                    "VALUES (:persona_id, :asset_key, :strategy_scope, :lesson_text, CAST(:embedding AS vector), "
                    "CAST(:supporting_prediction_ids AS uuid[]), :sample_size, now()) "
                    "RETURNING id"
                ),
                {
                    "persona_id": persona_id,
                    "asset_key": asset_key,
                    "strategy_scope": strategy_scope,
                    "lesson_text": lesson_text,
                    "embedding": vector_literal(embedding) if embedding is not None else None,
                    "supporting_prediction_ids": supporting_prediction_ids,
                    "sample_size": sample_size,
                },
            ).scalar_one()
            conn.execute(
                text(
                    "UPDATE agent_lessons SET superseded_by = :new_id "
                    "WHERE persona_id = :persona_id AND asset_key = :asset_key AND strategy_scope = :strategy_scope "
                    "AND superseded_by IS NULL AND id != :new_id"
                ),
                {
                    "new_id": new_id,
                    "persona_id": persona_id,
                    "asset_key": asset_key,
                    "strategy_scope": strategy_scope,
                },
            )
    except Exception as exc:
        print(f"save_lesson: failed to write lesson for {persona_id}/{asset_key}/{strategy_scope}: {exc}", file=sys.stderr)
