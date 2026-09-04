import json
import sys
from datetime import date
from typing import Any

from sqlalchemy import text

from storage.db import get_engine

_RESOLVED_DIRECTIONS = {"bullish", "bearish", "neutral"}


def get_earliest_pending_date(persona_id: str) -> date | None:
    try:
        engine = get_engine()
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT MIN(created_at) FROM prediction_history WHERE persona_id = :p AND outcome = 'pending'"),
                {"p": persona_id},
            ).first()
    except Exception as exc:
        print(f"get_earliest_pending_date: failed for {persona_id}: {exc}", file=sys.stderr)
        return None
    if not row or row[0] is None:
        return None
    return row[0].date()


def get_settlement_summary_by_persona() -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT persona_id, count(*) AS total, "
                    "count(*) FILTER (WHERE outcome != 'pending') AS settled, "
                    "count(*) FILTER (WHERE outcome = 'correct') AS correct "
                    "FROM prediction_history GROUP BY persona_id ORDER BY persona_id"
                )
            ).mappings().all()
    except Exception as exc:
        print(f"get_settlement_summary_by_persona: failed to query: {exc}", file=sys.stderr)
        return []

    summary = []
    for row in rows:
        row = dict(row)
        row["win_rate"] = round(row["correct"] / row["settled"], 4) if row["settled"] else None
        summary.append(row)
    return summary


def backfill_outcomes(persona_id: str, resolved_items: list[dict[str, Any]]) -> int:
    updated = 0
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for item in resolved_items:
                if not isinstance(item, dict):
                    continue
                challenge_id = str(item.get("id") or "").strip()
                if not challenge_id:
                    continue
                resolved_dir = str(
                    item.get("result")
                    or item.get("resolved_direction")
                    or item.get("final_direction")
                    or item.get("outcome")
                    or ""
                ).strip().lower()
                if resolved_dir not in _RESOLVED_DIRECTIONS:
                    continue
                result = conn.execute(
                    text(
                        "UPDATE prediction_history "
                        "SET outcome = CASE WHEN lower(direction) = :resolved_dir THEN 'correct' ELSE 'incorrect' END, "
                        "actual_result = CAST(:actual_result AS jsonb), "
                        "settled_at = now() "
                        "WHERE persona_id = :persona_id AND challenge_id = :challenge_id AND outcome = 'pending'"
                    ),
                    {
                        "persona_id": persona_id,
                        "challenge_id": challenge_id,
                        "resolved_dir": resolved_dir,
                        "actual_result": json.dumps(item, ensure_ascii=False, default=str),
                    },
                )
                updated += result.rowcount
    except Exception as exc:
        print(f"backfill_outcomes: failed to backfill for {persona_id}: {exc}", file=sys.stderr)
        return 0
    return updated
