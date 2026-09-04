import json
import sys
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import text

from storage.db import get_engine


def compute_backtest_metrics(persona_id: str, period_start: date, period_end: date) -> dict[str, Any] | None:
    start_ts = datetime.combine(period_start, time.min, tzinfo=timezone.utc)
    end_ts = datetime.combine(period_end, time.min, tzinfo=timezone.utc)
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT asset_key, outcome, confidence FROM prediction_history "
                    "WHERE persona_id = :persona_id AND outcome != 'pending' "
                    "AND created_at >= :start_ts AND created_at < :end_ts"
                ),
                {"persona_id": persona_id, "start_ts": start_ts, "end_ts": end_ts},
            ).mappings().all()

            total = len(rows)
            if total == 0:
                return None

            correct = sum(1 for row in rows if row["outcome"] == "correct")
            by_asset: dict[str, dict[str, int]] = {}
            for row in rows:
                bucket = by_asset.setdefault(row["asset_key"], {"total": 0, "correct": 0})
                bucket["total"] += 1
                if row["outcome"] == "correct":
                    bucket["correct"] += 1

            correct_confidences = [
                float(row["confidence"]) for row in rows if row["outcome"] == "correct" and row["confidence"] is not None
            ]
            incorrect_confidences = [
                float(row["confidence"]) for row in rows if row["outcome"] == "incorrect" and row["confidence"] is not None
            ]

            return {
                "total_predictions": total,
                "correct": correct,
                "win_rate": round(correct / total, 4),
                "by_asset": {
                    key: {**value, "win_rate": round(value["correct"] / value["total"], 4)}
                    for key, value in by_asset.items()
                },
                "avg_confidence_when_correct": (
                    round(sum(correct_confidences) / len(correct_confidences), 4) if correct_confidences else None
                ),
                "avg_confidence_when_incorrect": (
                    round(sum(incorrect_confidences) / len(incorrect_confidences), 4) if incorrect_confidences else None
                ),
            }
    except Exception as exc:
        print(f"compute_backtest_metrics: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return None


def save_backtest_report(persona_id: str, period_start: date, period_end: date, metrics: dict[str, Any]) -> None:
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO backtest_reports (persona_id, period_start, period_end, metrics, generated_at) "
                    "VALUES (:persona_id, :period_start, :period_end, CAST(:metrics AS jsonb), now())"
                ),
                {
                    "persona_id": persona_id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "metrics": json.dumps(metrics, ensure_ascii=False, default=str),
                },
            )
    except Exception as exc:
        print(f"save_backtest_report: failed to write report for {persona_id}: {exc}", file=sys.stderr)
