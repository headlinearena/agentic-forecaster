import json
import sys
from typing import Any

from sqlalchemy import text

from storage.db import get_engine
from storage.embeddings import compute_embedding, vector_literal


def save_prediction(
    persona_id: str,
    challenge_id: str,
    asset_key: str,
    strategy: str,
    model_output: dict[str, Any],
    direction: str | None,
    confidence: float | None,
) -> None:
    try:
        reasoning = str(model_output.get("reasoning") or "").strip()
        embedding = compute_embedding(reasoning) if reasoning else None
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO prediction_history (
                        persona_id, challenge_id, asset_key, strategy, snapshot_ids,
                        model_output, direction, confidence, embedding, outcome, created_at
                    ) VALUES (
                        :persona_id, :challenge_id, :asset_key, :strategy, '{}',
                        CAST(:model_output AS jsonb), :direction, :confidence,
                        CAST(:embedding AS vector), 'pending', now()
                    )
                    """
                ),
                {
                    "persona_id": persona_id,
                    "challenge_id": challenge_id,
                    "asset_key": asset_key,
                    "strategy": strategy,
                    "model_output": json.dumps(model_output, ensure_ascii=False, default=str),
                    "direction": direction,
                    "confidence": confidence,
                    "embedding": vector_literal(embedding) if embedding is not None else None,
                },
            )
    except Exception as exc:
        print(f"save_prediction: failed to write row for {persona_id}/{challenge_id}: {exc}", file=sys.stderr)
