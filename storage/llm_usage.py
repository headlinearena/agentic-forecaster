import json
import sys
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from storage.db import get_engine


def record_llm_usage(
    persona_id: str,
    *,
    action: str,
    status: str,
    backend: str | None,
    model_provider: str | None,
    model_name: str | None,
    deployment: str | None,
    usage_normalized: dict[str, Any] | None,
    usage_raw: dict[str, Any] | None,
    context: dict[str, Any] | None,
    error: str | None = None,
) -> None:
    details = {"usage_raw": usage_raw, "context": context or {}, "error": error}
    try:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_llm_usage (
                        persona_id, backend, model, model_provider, deployment,
                        status, request_type, prompt_tokens, completion_tokens, details
                    ) VALUES (
                        :persona_id, :backend, :model, :model_provider, :deployment,
                        :status, :request_type, :prompt_tokens, :completion_tokens, CAST(:details AS jsonb)
                    )
                    """
                ),
                {
                    "persona_id": persona_id,
                    "backend": backend or "unknown",
                    "model": model_name or "unknown",
                    "model_provider": model_provider,
                    "deployment": deployment,
                    "status": status,
                    "request_type": action,
                    "prompt_tokens": (usage_normalized or {}).get("prompt_tokens"),
                    "completion_tokens": (usage_normalized or {}).get("completion_tokens"),
                    "details": json.dumps(details, ensure_ascii=False, default=str),
                },
            )
    except (SQLAlchemyError, RuntimeError) as exc:
        print(f"record_llm_usage: failed to write usage row for {persona_id}: {exc}", file=sys.stderr)
