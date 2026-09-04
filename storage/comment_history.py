import sys

from sqlalchemy import text

from storage.db import get_engine
from storage.embeddings import compute_embedding, vector_literal


def save_comment(persona_id: str, news_id: str, comment_text: str) -> None:
    try:
        embedding = compute_embedding(comment_text) if comment_text.strip() else None
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO comment_history (persona_id, news_id, comment_text, embedding, created_at)
                    VALUES (:persona_id, :news_id, :comment_text, CAST(:embedding AS vector), now())
                    """
                ),
                {
                    "persona_id": persona_id,
                    "news_id": news_id,
                    "comment_text": comment_text,
                    "embedding": vector_literal(embedding) if embedding is not None else None,
                },
            )
    except Exception as exc:
        print(f"save_comment: failed to write row for {persona_id}/{news_id}: {exc}", file=sys.stderr)
