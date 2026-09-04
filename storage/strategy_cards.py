"""Versioned, self-maintained "strategy cards" plus shadow-prediction A/B bookkeeping.

A strategy card is a bounded, prompt-text-only JSON document that an agentic persona
maintains about its own trading rules. Cards never touch config or code: they are
injected into the prediction system prompt and nothing else, so the recursive
self-improvement loop has a fixed blast radius.

Lifecycle: candidate -> (shadow A/B vs the currently-active card) -> active -> retired,
with a post-promotion canary window that can roll the promotion back. card_version 0 in
shadow_predictions denotes the card-less baseline (the live persona before any card).
"""

import json
import sys
from datetime import datetime
from typing import Any

from sqlalchemy import text

from storage.db import get_engine

_RESOLVED_DIRECTIONS = {"bullish", "bearish", "neutral"}

# field -> (max items, max chars per item) for list fields
CARD_LIST_FIELDS: dict[str, tuple[int, int]] = {
    "entry_rules": (6, 240),
    "no_trade_conditions": (4, 240),
    "known_failure_modes": (6, 240),
}
# field -> max chars for scalar text fields
CARD_TEXT_FIELDS: dict[str, int] = {
    "confidence_guidance": 480,
}
CARD_FIELDS = set(CARD_LIST_FIELDS) | set(CARD_TEXT_FIELDS)


def validate_card(card: Any) -> list[str]:
    """Return a list of validation errors; an empty list means the card is acceptable."""
    errors: list[str] = []
    if not isinstance(card, dict):
        return ["card must be a JSON object"]
    unknown = set(card) - CARD_FIELDS
    if unknown:
        errors.append(f"unknown card fields: {', '.join(sorted(unknown))}")
    for field, (max_items, max_chars) in CARD_LIST_FIELDS.items():
        value = card.get(field)
        if value is None:
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            errors.append(f"{field} must be a list of strings")
            continue
        if len(value) > max_items:
            errors.append(f"{field} has {len(value)} items (max {max_items})")
        if any(len(item) > max_chars for item in value):
            errors.append(f"{field} has an item longer than {max_chars} chars")
        if any(not item.strip() for item in value):
            errors.append(f"{field} contains an empty item")
    for field, max_chars in CARD_TEXT_FIELDS.items():
        value = card.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            errors.append(f"{field} must be a string")
        elif len(value) > max_chars:
            errors.append(f"{field} is longer than {max_chars} chars")
    if not any(card.get(field) for field in CARD_FIELDS):
        errors.append("card must set at least one field")
    return errors


def _row_to_card(row: Any) -> dict[str, Any]:
    record = dict(row)
    if isinstance(record.get("card"), str):
        record["card"] = json.loads(record["card"])
    return record


def _get_card_by_status(persona_id: str, status: str) -> dict[str, Any] | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, persona_id, version, parent_version, card, status, source, "
                    "rationale, canary_status, created_at, activated_at, deactivated_at "
                    "FROM strategy_cards WHERE persona_id = :p AND status = :s "
                    "ORDER BY version DESC LIMIT 1"
                ),
                {"p": persona_id, "s": status},
            ).mappings().first()
    except Exception as exc:
        print(f"_get_card_by_status: failed for {persona_id}/{status}: {exc}", file=sys.stderr)
        return None
    return _row_to_card(row) if row else None


def get_active_card(persona_id: str) -> dict[str, Any] | None:
    return _get_card_by_status(persona_id, "active")


def get_candidate_card(persona_id: str) -> dict[str, Any] | None:
    return _get_card_by_status(persona_id, "candidate")


def get_card_history(persona_id: str, limit: int = 10) -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT version, parent_version, status, source, rationale, canary_status, "
                    "created_at, activated_at, deactivated_at "
                    "FROM strategy_cards WHERE persona_id = :p ORDER BY version DESC LIMIT :limit"
                ),
                {"p": persona_id, "limit": limit},
            ).mappings().all()
    except Exception as exc:
        print(f"get_card_history: failed for {persona_id}: {exc}", file=sys.stderr)
        return []
    return [dict(row) for row in rows]


def get_last_card_event_at(persona_id: str) -> datetime | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT max(created_at) FROM strategy_cards WHERE persona_id = :p"),
                {"p": persona_id},
            ).first()
    except Exception as exc:
        print(f"get_last_card_event_at: failed for {persona_id}: {exc}", file=sys.stderr)
        return None
    return row[0] if row else None


def create_candidate(
    persona_id: str, card: dict[str, Any], rationale: str, source: str = "reflection"
) -> dict[str, Any]:
    errors = validate_card(card)
    if errors:
        return {"status": "invalid", "errors": errors}
    if get_candidate_card(persona_id) is not None:
        return {"status": "error", "error": "a candidate card already exists"}
    active = get_active_card(persona_id)
    parent_version = active["version"] if active else None
    try:
        engine = get_engine()
        with engine.begin() as conn:
            version = conn.execute(
                text("SELECT coalesce(max(version), 0) + 1 FROM strategy_cards WHERE persona_id = :p"),
                {"p": persona_id},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO strategy_cards "
                    "(persona_id, version, parent_version, card, status, source, rationale) "
                    "VALUES (:p, :version, :parent_version, CAST(:card AS jsonb), 'candidate', :source, :rationale)"
                ),
                {
                    "p": persona_id,
                    "version": version,
                    "parent_version": parent_version,
                    "card": json.dumps(card, ensure_ascii=False),
                    "source": source,
                    "rationale": rationale,
                },
            )
    except Exception as exc:
        print(f"create_candidate: failed for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)}
    return {"status": "created", "version": version, "parent_version": parent_version}


def promote_candidate(persona_id: str) -> dict[str, Any]:
    candidate = get_candidate_card(persona_id)
    if candidate is None:
        return {"status": "error", "error": "no candidate card to promote"}
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE strategy_cards SET status = 'retired', deactivated_at = now() "
                    "WHERE persona_id = :p AND status = 'active'"
                ),
                {"p": persona_id},
            )
            conn.execute(
                text(
                    "UPDATE strategy_cards SET status = 'active', activated_at = now(), "
                    "canary_status = 'pending' "
                    "WHERE persona_id = :p AND version = :version"
                ),
                {"p": persona_id, "version": candidate["version"]},
            )
    except Exception as exc:
        print(f"promote_candidate: failed for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)}
    return {"status": "promoted", "version": candidate["version"]}


def reject_candidate(persona_id: str, reason: str) -> dict[str, Any]:
    candidate = get_candidate_card(persona_id)
    if candidate is None:
        return {"status": "error", "error": "no candidate card to reject"}
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE strategy_cards SET status = 'rejected', "
                    "rationale = coalesce(rationale, '') || :note "
                    "WHERE persona_id = :p AND version = :version"
                ),
                {"p": persona_id, "version": candidate["version"], "note": f"\n[rejected] {reason}"},
            )
    except Exception as exc:
        print(f"reject_candidate: failed for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)}
    return {"status": "rejected", "version": candidate["version"]}


def set_canary_status(persona_id: str, version: int, canary_status: str) -> None:
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE strategy_cards SET canary_status = :cs "
                    "WHERE persona_id = :p AND version = :version"
                ),
                {"p": persona_id, "version": version, "cs": canary_status},
            )
    except Exception as exc:
        print(f"set_canary_status: failed for {persona_id} v{version}: {exc}", file=sys.stderr)


def rollback_active(persona_id: str, reason: str) -> dict[str, Any]:
    """Deactivate the current active card and re-activate its parent's content as a new version.

    If the active card has no parent (it was the first card), the persona simply reverts to the
    card-less baseline. Rollback rows get canary_status 'passed' so a rollback is never itself
    rolled back by the canary check.
    """
    active = get_active_card(persona_id)
    if active is None:
        return {"status": "error", "error": "no active card to roll back"}
    parent_version = active.get("parent_version")
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE strategy_cards SET status = 'rolled_back', canary_status = 'rolled_back', "
                    "deactivated_at = now(), "
                    "rationale = coalesce(rationale, '') || :note "
                    "WHERE persona_id = :p AND version = :version"
                ),
                {"p": persona_id, "version": active["version"], "note": f"\n[rolled back] {reason}"},
            )
            if parent_version is None:
                return {"status": "rolled_back", "from_version": active["version"], "restored_version": None}
            parent = conn.execute(
                text("SELECT card FROM strategy_cards WHERE persona_id = :p AND version = :v"),
                {"p": persona_id, "v": parent_version},
            ).mappings().first()
            if parent is None:
                return {"status": "rolled_back", "from_version": active["version"], "restored_version": None}
            new_version = conn.execute(
                text("SELECT coalesce(max(version), 0) + 1 FROM strategy_cards WHERE persona_id = :p"),
                {"p": persona_id},
            ).scalar_one()
            parent_card = parent["card"]
            conn.execute(
                text(
                    "INSERT INTO strategy_cards "
                    "(persona_id, version, parent_version, card, status, source, rationale, "
                    " canary_status, activated_at) "
                    "VALUES (:p, :version, :parent_version, CAST(:card AS jsonb), 'active', "
                    "'rollback', :rationale, 'passed', now())"
                ),
                {
                    "p": persona_id,
                    "version": new_version,
                    "parent_version": parent_version,
                    "card": json.dumps(parent_card, ensure_ascii=False)
                    if not isinstance(parent_card, str)
                    else parent_card,
                    "rationale": f"rollback of v{active['version']}: {reason}",
                },
            )
    except Exception as exc:
        print(f"rollback_active: failed for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error", "error": str(exc)}
    return {"status": "rolled_back", "from_version": active["version"], "restored_version": new_version}


def record_shadow_prediction(
    persona_id: str,
    card_version: int,
    challenge_id: str,
    asset: str | None,
    direction: str,
    confidence: float,
    reasoning: str | None,
) -> None:
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO shadow_predictions "
                    "(persona_id, card_version, challenge_id, asset, direction, confidence, reasoning) "
                    "VALUES (:p, :card_version, :challenge_id, :asset, :direction, :confidence, :reasoning) "
                    "ON CONFLICT ON CONSTRAINT uq_shadow_predictions_persona_version_challenge DO NOTHING"
                ),
                {
                    "p": persona_id,
                    "card_version": card_version,
                    "challenge_id": challenge_id,
                    "asset": asset,
                    "direction": str(direction).lower(),
                    "confidence": float(confidence),
                    "reasoning": reasoning,
                },
            )
    except Exception as exc:
        print(f"record_shadow_prediction: failed for {persona_id}/{challenge_id}: {exc}", file=sys.stderr)


def get_earliest_pending_shadow_date(persona_id: str) -> datetime | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT min(created_at) FROM shadow_predictions "
                    "WHERE persona_id = :p AND outcome = 'pending'"
                ),
                {"p": persona_id},
            ).first()
    except Exception as exc:
        print(f"get_earliest_pending_shadow_date: failed for {persona_id}: {exc}", file=sys.stderr)
        return None
    return row[0] if row else None


def backfill_shadow_outcomes(persona_id: str, resolved_items: list[dict[str, Any]]) -> int:
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
                        "UPDATE shadow_predictions "
                        "SET outcome = CASE WHEN lower(direction) = :resolved_dir "
                        "THEN 'correct' ELSE 'incorrect' END, "
                        "actual_result = CAST(:actual_result AS jsonb), settled_at = now() "
                        "WHERE persona_id = :p AND challenge_id = :challenge_id AND outcome = 'pending'"
                    ),
                    {
                        "p": persona_id,
                        "challenge_id": challenge_id,
                        "resolved_dir": resolved_dir,
                        "actual_result": json.dumps(
                            {"result": resolved_dir, "close_price": item.get("close_price")},
                            ensure_ascii=False,
                        ),
                    },
                )
                updated += result.rowcount or 0
    except Exception as exc:
        print(f"backfill_shadow_outcomes: failed for {persona_id}: {exc}", file=sys.stderr)
    return updated


def compare_shadow_brier(persona_id: str, candidate_version: int, baseline_version: int) -> dict[str, Any] | None:
    """Paired Brier comparison over challenges both card versions predicted and that settled.

    Brier per prediction = (confidence - correct)^2 with correct in {0, 1}; lower is better.
    """
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT count(*) AS paired_n, "
                    "avg(power(c.confidence - (c.outcome = 'correct')::int, 2)) AS candidate_brier, "
                    "avg(power(b.confidence - (b.outcome = 'correct')::int, 2)) AS baseline_brier "
                    "FROM shadow_predictions c "
                    "JOIN shadow_predictions b ON b.persona_id = c.persona_id "
                    "  AND b.challenge_id = c.challenge_id AND b.card_version = :baseline_version "
                    "WHERE c.persona_id = :p AND c.card_version = :candidate_version "
                    "  AND c.outcome IN ('correct', 'incorrect') AND b.outcome IN ('correct', 'incorrect')"
                ),
                {"p": persona_id, "candidate_version": candidate_version, "baseline_version": baseline_version},
            ).mappings().first()
    except Exception as exc:
        print(f"compare_shadow_brier: failed for {persona_id}: {exc}", file=sys.stderr)
        return None
    if not row or not row["paired_n"]:
        return {"paired_n": 0, "candidate_brier": None, "baseline_brier": None}
    return {
        "paired_n": int(row["paired_n"]),
        "candidate_brier": round(float(row["candidate_brier"]), 4),
        "baseline_brier": round(float(row["baseline_brier"]), 4),
    }


def get_live_brier(
    persona_id: str, created_after: datetime | None = None, created_before: datetime | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Brier score over this persona's settled live predictions in prediction_history.

    With `limit`, only the most recent N settled predictions in the window are used.
    """
    clauses = ["persona_id = :p", "outcome IN ('correct', 'incorrect')", "confidence IS NOT NULL"]
    params: dict[str, Any] = {"p": persona_id}
    if created_after is not None:
        clauses.append("created_at >= :after")
        params["after"] = created_after
    if created_before is not None:
        clauses.append("created_at < :before")
        params["before"] = created_before
    inner = (
        "SELECT confidence, (outcome = 'correct')::int AS correct FROM prediction_history "
        f"WHERE {' AND '.join(clauses)} ORDER BY created_at DESC"
    )
    if limit is not None:
        inner += f" LIMIT {int(limit)}"
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(f"SELECT count(*) AS n, avg(power(confidence - correct, 2)) AS brier FROM ({inner}) sub"),
                params,
            ).mappings().first()
    except Exception as exc:
        print(f"get_live_brier: failed for {persona_id}: {exc}", file=sys.stderr)
        return {"n": 0, "brier": None}
    if not row or not row["n"]:
        return {"n": 0, "brier": None}
    return {"n": int(row["n"]), "brier": round(float(row["brier"]), 4)}


def format_card_for_prompt(card: dict[str, Any]) -> str:
    lines: list[str] = []
    labels = {
        "entry_rules": "Entry rules",
        "no_trade_conditions": "No-trade conditions",
        "known_failure_modes": "Known failure modes",
    }
    for field, label in labels.items():
        items = card.get(field) or []
        if items:
            lines.append(f"{label}:")
            lines.extend(f"- {item}" for item in items)
    guidance = card.get("confidence_guidance")
    if guidance:
        lines.append(f"Confidence guidance: {guidance}")
    return "\n".join(lines)
