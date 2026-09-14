"""Settlement backfill for Civic Index / official-statistics forecasts.

Civic rows in prediction_history (strategy 'civic_<shape>') resolve on different platform
endpoints than market challenges, so storage/settlement.py never touches them. This module
settles them from a per-challenge detail payload fetched by the caller.

Outcome rules (deliberately simple, calibration-oriented):
- civic_numeric_distribution: correct iff |actual - mean| <= std (the forecast's own one-sigma
  band covered the release).
- civic_binary_probability:   correct iff (yes_probability >= 0.5) matches the yes/no outcome.

The detail payload's shape is not contractually documented, so extraction is defensive: if no
unambiguous actual value can be found, the row stays 'pending' -- outcomes are never guessed.
"""

import json
import sys
from typing import Any, Callable

from sqlalchemy import text

from storage.db import get_engine

_ACTUAL_KEYS = ("actual_value", "actual_category", "resolved_value", "released_value", "actual", "result_value")
_YES_VALUES = {"yes", "true", "1"}
_NO_VALUES = {"no", "false", "0"}


def _num(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def extract_civic_actual(payload: Any) -> Any | None:
    """Best-effort extraction of the released/actual value from a challenge detail payload."""
    if not isinstance(payload, dict):
        return None
    containers = [payload]
    for key in ("challenge", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    for container in containers:
        for key in _ACTUAL_KEYS:
            if container.get(key) is not None:
                return container[key]
        result = container.get("result")
        if isinstance(result, dict):
            for key in _ACTUAL_KEYS + ("value",):
                if result.get(key) is not None:
                    return result[key]
        elif result is not None:
            return result
    return None


def decide_civic_outcome(
    strategy: str, model_output: dict[str, Any], actual: Any
) -> tuple[str, dict[str, Any]] | None:
    """Return (outcome, actual_result_json) or None when the row cannot be settled safely."""
    if strategy == "civic_numeric_distribution":
        actual_num = _num(actual)
        mean = _num(model_output.get("mean"))
        std = _num(model_output.get("std"))
        if actual_num is None or mean is None or std is None:
            return None
        outcome = "correct" if abs(actual_num - mean) <= abs(std) else "incorrect"
        return outcome, {"actual": actual_num, "mean": mean, "std": std}
    if strategy == "civic_binary_probability":
        probability = _num(model_output.get("yes_probability"))
        raw = str(actual).strip().lower()
        if raw in _YES_VALUES:
            actual_yes = True
        elif raw in _NO_VALUES:
            actual_yes = False
        else:
            return None
        if probability is None:
            return None
        outcome = "correct" if (probability >= 0.5) == actual_yes else "incorrect"
        return outcome, {"actual": "yes" if actual_yes else "no", "yes_probability": probability}
    return None


def list_pending_civic(persona_id: str) -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, challenge_id, strategy, model_output FROM prediction_history "
                    "WHERE persona_id = :p AND outcome = 'pending' AND strategy LIKE 'civic_%'"
                ),
                {"p": persona_id},
            ).mappings().all()
    except Exception as exc:
        print(f"list_pending_civic: failed for {persona_id}: {exc}", file=sys.stderr)
        return []
    result = []
    for row in rows:
        record = dict(row)
        if isinstance(record.get("model_output"), str):
            try:
                record["model_output"] = json.loads(record["model_output"])
            except ValueError:
                record["model_output"] = {}
        result.append(record)
    return result


def backfill_civic_outcomes(
    persona_id: str, fetch_detail: Callable[[str], dict[str, Any] | None]
) -> int:
    """Settle this persona's pending civic rows. fetch_detail is called at most once per
    distinct challenge_id and should return the platform's challenge detail payload (or None)."""
    pending = list_pending_civic(persona_id)
    if not pending:
        return 0
    actual_by_challenge: dict[str, Any] = {}
    for row in pending:
        challenge_id = str(row["challenge_id"])
        if challenge_id not in actual_by_challenge:
            try:
                actual_by_challenge[challenge_id] = extract_civic_actual(fetch_detail(challenge_id))
            except Exception as exc:
                print(f"backfill_civic_outcomes: fetch failed for {challenge_id}: {exc}", file=sys.stderr)
                actual_by_challenge[challenge_id] = None

    updated = 0
    try:
        engine = get_engine()
        with engine.begin() as conn:
            for row in pending:
                actual = actual_by_challenge.get(str(row["challenge_id"]))
                if actual is None:
                    continue
                decision = decide_civic_outcome(
                    str(row["strategy"]), row.get("model_output") or {}, actual
                )
                if decision is None:
                    continue
                outcome, actual_result = decision
                result = conn.execute(
                    text(
                        "UPDATE prediction_history SET outcome = :outcome, "
                        "actual_result = CAST(:actual_result AS jsonb), settled_at = now() "
                        "WHERE id = :id AND outcome = 'pending'"
                    ),
                    {
                        "id": row["id"],
                        "outcome": outcome,
                        "actual_result": json.dumps(actual_result, ensure_ascii=False),
                    },
                )
                updated += result.rowcount or 0
    except Exception as exc:
        print(f"backfill_civic_outcomes: failed for {persona_id}: {exc}", file=sys.stderr)
    return updated
