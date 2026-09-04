import os
import uuid
from typing import Any


def build_langfuse_callbacks(config: dict[str, Any], persona_id: str, action: str) -> tuple[list, dict]:
    langfuse_config = (config.get("observability") or {}).get("langfuse") or {}
    if not langfuse_config.get("enabled", False):
        return [], {}

    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    host = os.environ.get("LANGFUSE_HOST")
    if not public_key or not secret_key or not host:
        return [], {}

    from langfuse.langchain import CallbackHandler

    trace_id = uuid.uuid4().hex
    handler = CallbackHandler(trace_context={"trace_id": trace_id})
    metadata = {
        "langfuse_session_id": f"{persona_id}-{action}",
        "langfuse_tags": [persona_id, action],
    }
    return [handler], metadata
