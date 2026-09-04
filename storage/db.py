import json
from pathlib import Path
from typing import Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from storage.config import build_postgres_dsn

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(build_postgres_dsn(), pool_size=2, pool_pre_ping=True)
    return _engine


def append_to_spool(spool_path: Path, record: dict) -> None:
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    with spool_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def drain_spool(spool_path: Path, replay: Callable[[dict], None]) -> None:
    if not spool_path.exists():
        return
    lines = [line for line in spool_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    remaining: list[str] = []
    for line in lines:
        record = json.loads(line)
        try:
            replay(record)
        except Exception:
            remaining.append(line)
    if remaining:
        spool_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    else:
        spool_path.unlink(missing_ok=True)
