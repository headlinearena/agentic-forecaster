from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from storage.db import append_to_spool, drain_spool, get_engine


def is_processed(persona_id: str, item_type: str, item_id: str) -> bool:
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT 1 FROM agent_processed_items "
                "WHERE persona_id = :persona_id AND item_type = :item_type AND item_id = :item_id"
            ),
            {"persona_id": persona_id, "item_type": item_type, "item_id": item_id},
        ).first()
    return result is not None


def mark_processed(persona_id: str, item_type: str, item_id: str, *, spool_path: Path) -> None:
    record = {"persona_id": persona_id, "item_type": item_type, "item_id": item_id}
    try:
        _insert(record)
    except SQLAlchemyError:
        append_to_spool(spool_path, record)


def drain_pending_processed_items(spool_path: Path) -> None:
    drain_spool(spool_path, _insert)


def _insert(record: dict[str, Any]) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_processed_items (persona_id, item_type, item_id, processed_at) "
                "VALUES (:persona_id, :item_type, :item_id, now()) "
                "ON CONFLICT (persona_id, item_type, item_id) DO NOTHING"
            ),
            record,
        )
