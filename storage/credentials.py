import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from storage.crypto import decrypt_secret, encrypt_secret
from storage.db import append_to_spool, drain_spool, get_engine

_NON_PAYLOAD_KEYS = {
    "agent_id",
    "client_id",
    "client_secret",
    "access_token",
    "token_expires_at",
    "granted_scopes",
    "last_cycle_at",
}

# Row fields that hold raw (already-encrypted) bytes and therefore need a
# JSON-safe encoding when written to the plaintext-on-disk JSONL spool.
_BINARY_ROW_KEYS = ("encrypted_client_secret", "encrypted_access_token")


def save_credentials(persona_id: str, credentials: dict[str, Any], *, spool_path: Path) -> None:
    row = _build_row(persona_id, credentials)
    try:
        _upsert(row)
    except SQLAlchemyError:
        append_to_spool(spool_path, {"kind": "credentials", **_encode_binary_fields(row)})


def load_credentials(persona_id: str) -> dict[str, Any]:
    engine = get_engine()
    with engine.connect() as conn:
        result = (
            conn.execute(
                text(
                    "SELECT agent_id, encrypted_client_secret, encrypted_access_token, "
                    "expires_at, payload, granted_scopes, last_cycle_at "
                    "FROM agent_credentials WHERE persona_id = :persona_id"
                ),
                {"persona_id": persona_id},
            )
            .mappings()
            .first()
        )
    if result is None:
        return {}
    credentials: dict[str, Any] = dict(result["payload"] or {})
    if result["agent_id"] is not None:
        credentials["agent_id"] = result["agent_id"]
    if result["encrypted_client_secret"] is not None:
        credentials["client_secret"] = decrypt_secret(bytes(result["encrypted_client_secret"]))
    if result["encrypted_access_token"] is not None:
        credentials["access_token"] = decrypt_secret(bytes(result["encrypted_access_token"]))
    if result["expires_at"] is not None:
        credentials["token_expires_at"] = result["expires_at"].timestamp()
    if result["granted_scopes"]:
        credentials["granted_scopes"] = list(result["granted_scopes"])
    if result["last_cycle_at"] is not None:
        credentials["last_cycle_at"] = result["last_cycle_at"].isoformat()
    return credentials


def drain_pending_credentials(spool_path: Path) -> None:
    def replay(record: dict[str, Any]) -> None:
        kind = record.get("kind")
        if kind == "credentials":
            row = {k: v for k, v in record.items() if k != "kind"}
            _upsert(_decode_binary_fields(row))
        elif kind == "last_cycle_at":
            _update_last_cycle_at(record["persona_id"], record["last_cycle_at"])

    drain_spool(spool_path, replay)


def touch_last_cycle_at(persona_id: str, *, spool_path: Path) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        _update_last_cycle_at(persona_id, now_iso)
    except SQLAlchemyError:
        append_to_spool(
            spool_path, {"kind": "last_cycle_at", "persona_id": persona_id, "last_cycle_at": now_iso}
        )


def _build_row(persona_id: str, credentials: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in credentials.items() if k not in _NON_PAYLOAD_KEYS}
    expires_at = credentials.get("token_expires_at")
    client_secret = credentials.get("client_secret")
    access_token = credentials.get("access_token")
    return {
        "persona_id": persona_id,
        "agent_id": credentials.get("agent_id") or credentials.get("client_id"),
        "encrypted_client_secret": encrypt_secret(client_secret) if client_secret else None,
        "encrypted_access_token": encrypt_secret(access_token) if access_token else None,
        "expires_at": datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat() if expires_at else None,
        "payload": payload,
        "granted_scopes": list(credentials.get("granted_scopes") or []),
        "last_cycle_at": credentials.get("last_cycle_at"),
    }


def _to_jsonb_bind(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _encode_binary_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Base64-encode already-encrypted bytes fields so the row is JSON-safe
    for the spool. The spooled value remains ciphertext (base64 of Fernet
    output), never plaintext."""
    encoded = dict(row)
    for key in _BINARY_ROW_KEYS:
        value = encoded.get(key)
        if isinstance(value, (bytes, bytearray)):
            encoded[key] = base64.b64encode(bytes(value)).decode("ascii")
    return encoded


def _decode_binary_fields(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key in _BINARY_ROW_KEYS:
        value = decoded.get(key)
        if isinstance(value, str):
            decoded[key] = base64.b64decode(value)
    return decoded


def _upsert(row: dict[str, Any]) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO agent_credentials (
                    persona_id, agent_id, encrypted_client_secret, encrypted_access_token,
                    expires_at, payload, granted_scopes, last_cycle_at, updated_at
                ) VALUES (
                    :persona_id, :agent_id, :encrypted_client_secret, :encrypted_access_token,
                    :expires_at, CAST(:payload AS jsonb), CAST(:granted_scopes AS jsonb), :last_cycle_at, now()
                )
                ON CONFLICT (persona_id) DO UPDATE SET
                    agent_id = EXCLUDED.agent_id,
                    encrypted_client_secret = EXCLUDED.encrypted_client_secret,
                    encrypted_access_token = EXCLUDED.encrypted_access_token,
                    expires_at = EXCLUDED.expires_at,
                    payload = EXCLUDED.payload,
                    granted_scopes = EXCLUDED.granted_scopes,
                    last_cycle_at = EXCLUDED.last_cycle_at,
                    updated_at = now()
                """
            ),
            {
                "persona_id": row["persona_id"],
                "agent_id": row["agent_id"],
                "encrypted_client_secret": row["encrypted_client_secret"],
                "encrypted_access_token": row["encrypted_access_token"],
                "expires_at": row["expires_at"],
                "payload": _to_jsonb_bind(row["payload"]),
                "granted_scopes": _to_jsonb_bind(row["granted_scopes"]),
                "last_cycle_at": row["last_cycle_at"],
            },
        )


def _update_last_cycle_at(persona_id: str, last_cycle_at_iso: str) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_credentials SET last_cycle_at = :last_cycle_at WHERE persona_id = :persona_id"),
            {"persona_id": persona_id, "last_cycle_at": last_cycle_at_iso},
        )
