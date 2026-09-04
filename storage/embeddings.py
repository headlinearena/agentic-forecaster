import json
import os
import sys
from urllib import request

EMBEDDING_SERVICE_URL_ENV = "EMBEDDING_SERVICE_URL"
DEFAULT_EMBEDDING_SERVICE_URL = "http://embedding-service:8000"


def compute_embedding(text: str) -> list[float] | None:
    base_url = os.environ.get(EMBEDDING_SERVICE_URL_ENV, DEFAULT_EMBEDDING_SERVICE_URL)
    body = json.dumps({"text": text}).encode("utf-8")
    req = request.Request(
        f"{base_url}/embed",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"compute_embedding: failed to reach embedding service: {exc}", file=sys.stderr)
        return None
    embedding = payload.get("embedding") if isinstance(payload, dict) else None
    return embedding if isinstance(embedding, list) else None


def vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"
