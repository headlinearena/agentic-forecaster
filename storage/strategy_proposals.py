import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

import storage.backtest as storage_backtest
from storage.db import get_engine

ALLOWED_PROPOSAL_FIELDS = {
    "blocked_challenge_keywords",
    "max_predictions_per_cycle",
    "revision_confidence_threshold",
    "revision_min_interval_seconds",
    "allow_revision",
    "skip_predicted_challenges",
}

ALLOWED_REVIEW_STATUSES = {"pending_review", "accepted", "rejected", "applied"}


def get_recent_lessons_for_persona(persona_id: str, limit: int = 10) -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, asset_key, strategy_scope, lesson_text, sample_size, created_at "
                    "FROM agent_lessons WHERE persona_id = :persona_id AND superseded_by IS NULL "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"persona_id": persona_id, "limit": limit},
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        print(f"get_recent_lessons_for_persona: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return []


def simulate_revision_confidence_threshold(
    persona_id: str, current_threshold: float, proposed_threshold: float, min_sample_size: int = 5
) -> dict[str, Any]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT challenge_id, direction, confidence, outcome FROM prediction_history "
                    "WHERE persona_id = :persona_id AND outcome != 'pending' "
                    "ORDER BY challenge_id, created_at"
                ),
                {"persona_id": persona_id},
            ).mappings().all()
    except Exception as exc:
        print(f"simulate_revision_confidence_threshold: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error", "affected_count": 0}

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["challenge_id"], []).append(dict(row))

    affected_correct_baseline = 0
    affected_correct_simulated = 0
    affected_count = 0
    for group_rows in groups.values():
        for prev, curr in zip(group_rows, group_rows[1:]):
            if curr["confidence"] is None or prev["confidence"] is None:
                continue
            direction_changed = curr["direction"] != prev["direction"]
            confidence_delta = abs(float(curr["confidence"]) - float(prev["confidence"]))
            would_submit_baseline = direction_changed or confidence_delta >= current_threshold
            would_submit_simulated = direction_changed or confidence_delta >= proposed_threshold
            if would_submit_baseline == would_submit_simulated:
                continue
            affected_count += 1
            is_correct = curr["outcome"] == "correct"
            if would_submit_baseline and is_correct:
                affected_correct_baseline += 1
            if would_submit_simulated and is_correct:
                affected_correct_simulated += 1

    if affected_count < min_sample_size:
        return {"status": "insufficient_sample", "affected_count": affected_count}

    baseline_win_rate = round(affected_correct_baseline / affected_count, 4)
    simulated_win_rate = round(affected_correct_simulated / affected_count, 4)
    if simulated_win_rate > baseline_win_rate:
        verdict = "improvement"
    elif simulated_win_rate < baseline_win_rate:
        verdict = "regression"
    else:
        verdict = "inconclusive"

    return {
        "status": "validated",
        "affected_count": affected_count,
        "baseline_win_rate": baseline_win_rate,
        "simulated_win_rate": simulated_win_rate,
        "verdict": verdict,
    }


def save_proposal(
    persona_id: str,
    signal_type: str,
    rationale: str,
    proposed_field: str,
    current_value: Any,
    proposed_value: Any,
    supporting_backtest_report_id: str | None,
    supporting_lesson_ids: list[str],
    validation_status: str,
    validation_metrics: dict[str, Any] | None,
) -> dict[str, Any]:
    """Inserts a new proposal, unless an identical (persona_id, proposed_field,
    proposed_value) proposal is already live (pending_review, not superseded) -- in
    which case the insert is skipped entirely to avoid duplicate pile-up. If a live
    proposal exists for the same field with a DIFFERENT value, the new row supersedes
    it (same insert-then-supersede-sibling pattern as storage/reflection.py::save_lesson)."""
    try:
        engine = get_engine()
        with engine.begin() as conn:
            # Scoped by (persona_id, proposed_field), not a single id -- pre-existing data
            # can (rarely) have more than one live row for the same field; match against
            # ALL of them so the dedup check and the supersede below both see the full set.
            live_rows = conn.execute(
                text(
                    "SELECT id, proposed_value FROM strategy_proposals "
                    "WHERE persona_id = :persona_id AND proposed_field = :proposed_field "
                    "AND superseded_by IS NULL AND review_status = 'pending_review'"
                ),
                {"persona_id": persona_id, "proposed_field": proposed_field},
            ).mappings().all()

            for row in live_rows:
                if row["proposed_value"] == proposed_value:
                    return {"status": "skipped_duplicate", "id": str(row["id"])}

            new_id = conn.execute(
                text(
                    "INSERT INTO strategy_proposals "
                    "(persona_id, signal_type, rationale, proposed_field, current_value, proposed_value, "
                    "supporting_backtest_report_id, supporting_lesson_ids, validation_status, validation_metrics) "
                    "VALUES (:persona_id, :signal_type, :rationale, :proposed_field, "
                    "CAST(:current_value AS jsonb), CAST(:proposed_value AS jsonb), "
                    ":supporting_backtest_report_id, CAST(:supporting_lesson_ids AS uuid[]), "
                    ":validation_status, CAST(:validation_metrics AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "persona_id": persona_id,
                    "signal_type": signal_type,
                    "rationale": rationale,
                    "proposed_field": proposed_field,
                    "current_value": json.dumps(current_value, ensure_ascii=False, default=str),
                    "proposed_value": json.dumps(proposed_value, ensure_ascii=False, default=str),
                    "supporting_backtest_report_id": supporting_backtest_report_id,
                    "supporting_lesson_ids": supporting_lesson_ids,
                    "validation_status": validation_status,
                    "validation_metrics": (
                        json.dumps(validation_metrics, ensure_ascii=False, default=str)
                        if validation_metrics is not None
                        else None
                    ),
                },
            ).scalar_one()

            if live_rows:
                conn.execute(
                    text(
                        "UPDATE strategy_proposals SET superseded_by = :new_id "
                        "WHERE persona_id = :persona_id AND proposed_field = :proposed_field "
                        "AND superseded_by IS NULL AND id != :new_id"
                    ),
                    {"new_id": new_id, "persona_id": persona_id, "proposed_field": proposed_field},
                )

        return {"status": "created", "id": str(new_id)}
    except Exception as exc:
        print(f"save_proposal: failed to write proposal for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error", "id": None}


def mark_reviewed(persona_id: str, proposal_id: str, review_status: str) -> dict[str, Any]:
    if review_status not in ALLOWED_REVIEW_STATUSES:
        return {"status": "invalid_status", "allowed": sorted(ALLOWED_REVIEW_STATUSES)}
    try:
        engine = get_engine()
        with engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE strategy_proposals SET review_status = :review_status, reviewed_at = now() "
                    "WHERE id = CAST(:proposal_id AS uuid) AND persona_id = :persona_id"
                ),
                {"review_status": review_status, "proposal_id": proposal_id, "persona_id": persona_id},
            )
    except Exception as exc:
        print(f"mark_reviewed: failed to update {proposal_id} for {persona_id}: {exc}", file=sys.stderr)
        return {"status": "error"}
    if result.rowcount == 0:
        return {"status": "not_found", "id": proposal_id}
    return {"status": "updated", "id": proposal_id, "review_status": review_status}


def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, persona_id, signal_type, rationale, proposed_field, current_value, "
                    "proposed_value, validation_status, validation_metrics, review_status, reviewed_at, "
                    "outcome_status, outcome_metrics, created_at "
                    "FROM strategy_proposals WHERE id = CAST(:proposal_id AS uuid)"
                ),
                {"proposal_id": proposal_id},
            ).mappings().first()
        return dict(row) if row is not None else None
    except Exception as exc:
        print(f"get_proposal: failed to query {proposal_id}: {exc}", file=sys.stderr)
        return None


def list_proposals(persona_id: str, review_status: str = "pending_review") -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, signal_type, rationale, proposed_field, current_value, proposed_value, "
                    "validation_status, validation_metrics, review_status, reviewed_at, outcome_status, "
                    "outcome_metrics, created_at "
                    "FROM strategy_proposals WHERE persona_id = :persona_id AND review_status = :review_status "
                    "ORDER BY created_at DESC"
                ),
                {"persona_id": persona_id, "review_status": review_status},
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        print(f"list_proposals: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return []


def list_all_proposals(review_status: str | None = None) -> list[dict[str, Any]]:
    query = (
        "SELECT id, persona_id, signal_type, rationale, proposed_field, current_value, proposed_value, "
        "validation_status, validation_metrics, review_status, reviewed_at, outcome_status, outcome_metrics, "
        "created_at FROM strategy_proposals "
    )
    params: dict[str, Any] = {}
    if review_status:
        query += "WHERE review_status = :review_status "
        params["review_status"] = review_status
    query += "ORDER BY created_at DESC"

    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(text(query), params).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        print(f"list_all_proposals: failed to query: {exc}", file=sys.stderr)
        return []


def get_live_proposal_for_field(persona_id: str, proposed_field: str) -> dict[str, Any] | None:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT id, proposed_value, review_status, created_at FROM strategy_proposals "
                    "WHERE persona_id = :persona_id AND proposed_field = :proposed_field "
                    "AND superseded_by IS NULL AND review_status = 'pending_review'"
                ),
                {"persona_id": persona_id, "proposed_field": proposed_field},
            ).mappings().first()
        return dict(row) if row is not None else None
    except Exception as exc:
        print(f"get_live_proposal_for_field: failed to query for {persona_id}/{proposed_field}: {exc}", file=sys.stderr)
        return None


def get_proposal_track_record(persona_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """The most recent RESOLVED (non-pending) proposals for this persona -- fed into the
    generation prompt so the LLM sees what happened to its past suggestions."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT proposed_field, proposed_value, review_status, outcome_status, outcome_metrics, "
                    "created_at FROM strategy_proposals "
                    "WHERE persona_id = :persona_id AND review_status != 'pending_review' "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"persona_id": persona_id, "limit": limit},
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        print(f"get_proposal_track_record: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return []


def list_proposals_needing_evaluation(persona_id: str) -> list[dict[str, Any]]:
    try:
        engine = get_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, persona_id, proposed_field, created_at, reviewed_at FROM strategy_proposals "
                    "WHERE persona_id = :persona_id AND review_status = 'applied' "
                    "AND outcome_status IN ('not_yet_evaluated', 'insufficient_data')"
                ),
                {"persona_id": persona_id},
            ).mappings().all()
        return [dict(row) for row in rows]
    except Exception as exc:
        print(f"list_proposals_needing_evaluation: failed to query for {persona_id}: {exc}", file=sys.stderr)
        return []


def evaluate_applied_proposal_outcome(
    proposal_id: str, min_sample_size: int = 5, lookback_days: int = 30
) -> dict[str, Any]:
    """Compares backtest win rate in the lookback_days window before the proposal was
    created (what the LLM saw) against the window since it was applied (reviewed_at to
    now). Writes outcome_status/outcome_metrics on the row. insufficient_data is not a
    dead end -- it's re-checked next cycle as more predictions settle."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT persona_id, created_at, reviewed_at FROM strategy_proposals "
                    "WHERE id = CAST(:proposal_id AS uuid)"
                ),
                {"proposal_id": proposal_id},
            ).mappings().first()
    except Exception as exc:
        print(f"evaluate_applied_proposal_outcome: failed to query {proposal_id}: {exc}", file=sys.stderr)
        return {"status": "error"}

    if row is None:
        return {"status": "not_found"}
    if row["reviewed_at"] is None:
        return {"status": "error", "reason": "missing_reviewed_at"}

    persona_id = row["persona_id"]
    created_at: datetime = row["created_at"]
    reviewed_at: datetime = row["reviewed_at"]

    before = storage_backtest.compute_backtest_metrics(
        persona_id, created_at.date() - timedelta(days=lookback_days), created_at.date()
    )
    after = storage_backtest.compute_backtest_metrics(
        persona_id, reviewed_at.date(), datetime.now(timezone.utc).date()
    )

    if after is None or after["total_predictions"] < min_sample_size:
        outcome_status = "insufficient_data"
        outcome_metrics = None
    else:
        before_win_rate = before["win_rate"] if before else None
        after_win_rate = after["win_rate"]
        delta = (after_win_rate - before_win_rate) if before_win_rate is not None else None
        if delta is None:
            outcome_status = "inconclusive"
        elif delta > 0.05:
            outcome_status = "improved"
        elif delta < -0.05:
            outcome_status = "regressed"
        else:
            outcome_status = "inconclusive"
        outcome_metrics = {
            "before_win_rate": before_win_rate,
            "before_total": before["total_predictions"] if before else 0,
            "after_win_rate": after_win_rate,
            "after_total": after["total_predictions"],
        }

    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE strategy_proposals SET outcome_status = :outcome_status, "
                    "outcome_metrics = CAST(:outcome_metrics AS jsonb) WHERE id = CAST(:proposal_id AS uuid)"
                ),
                {
                    "outcome_status": outcome_status,
                    "outcome_metrics": json.dumps(outcome_metrics, ensure_ascii=False) if outcome_metrics else None,
                    "proposal_id": proposal_id,
                },
            )
    except Exception as exc:
        print(f"evaluate_applied_proposal_outcome: failed to update {proposal_id}: {exc}", file=sys.stderr)
        return {"status": "error"}

    return {"status": "ok", "outcome_status": outcome_status, "outcome_metrics": outcome_metrics}
