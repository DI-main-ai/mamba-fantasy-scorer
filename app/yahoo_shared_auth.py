import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request

import app.yahoo_auth as yahoo_auth


GLOBAL_TOKEN_SLOT = "__mamba_global_yahoo__"
TOKEN_FILE_ENV = "YAHOO_TOKEN_FILE"
BOOTSTRAP_REFRESH_ENV = "YAHOO_REFRESH_TOKEN"
UPSTASH_URL_ENV = "UPSTASH_REDIS_REST_URL"
UPSTASH_TOKEN_ENV = "UPSTASH_REDIS_REST_TOKEN"
UPSTASH_TOKEN_KEY = "mamba:yahoo:oauth:v1"

storage_status_router = APIRouter(
    prefix="/auth/yahoo",
    tags=["yahoo-storage"],
)


def _token_file_path() -> Optional[Path]:
    """Return an optional persistent token path.

    A persistent disk remains supported as a fallback, but the free Mamba
    deployment normally uses Upstash Redis instead so Render can spin down and
    restart without losing Yahoo authorization.
    """

    value = os.getenv(TOKEN_FILE_ENV, "").strip()
    return Path(value) if value else None


def _upstash_config() -> Optional[Dict[str, str]]:
    url = os.getenv(UPSTASH_URL_ENV, "").strip().rstrip("/")
    token = os.getenv(UPSTASH_TOKEN_ENV, "").strip()

    if url and token:
        return {"url": url, "token": token}
    return None


def _upstash_command(command: List[Any]) -> Any:
    config = _upstash_config()
    if config is None:
        raise RuntimeError("Upstash Redis is not configured.")

    request = urllib.request.Request(
        config["url"],
        data=json.dumps(command).encode("utf-8"),
        method="POST",
    )
    request.add_header("Authorization", f"Bearer {config['token']}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise RuntimeError(f"Upstash request failed: {exc}") from exc

    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"Upstash command failed: {payload['error']}")

    if not isinstance(payload, dict) or "result" not in payload:
        raise RuntimeError("Upstash returned an unexpected response.")

    return payload["result"]


def _normalize_record(
    token_data: Dict[str, Any],
    existing: Dict[str, Any],
) -> Dict[str, Any]:
    refresh_token = token_data.get("refresh_token") or existing.get("refresh_token")
    expires_in = int(token_data.get("expires_in") or 3600)

    return {
        "access_token": token_data.get("access_token"),
        "refresh_token": refresh_token,
        "expires_at": time.time() + max(expires_in - 60, 60),
    }


def _valid_persisted_record(data: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(data, dict) or not data.get("refresh_token"):
        return None

    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token"),
        "expires_at": float(data.get("expires_at") or 0),
    }


def _persist_to_upstash(record: Dict[str, Any]) -> bool:
    if _upstash_config() is None:
        return False

    try:
        result = _upstash_command(
            ["SET", UPSTASH_TOKEN_KEY, json.dumps(record, separators=(",", ":"))]
        )
        if result != "OK":
            raise RuntimeError(f"Unexpected SET result: {result}")
        return True
    except RuntimeError as exc:
        print(f"WARNING: Yahoo token was not persisted to Upstash: {exc}")
        return False


def _load_from_upstash() -> Optional[Dict[str, Any]]:
    if _upstash_config() is None:
        return None

    try:
        raw = _upstash_command(["GET", UPSTASH_TOKEN_KEY])
        if raw in (None, ""):
            return None
        data = json.loads(str(raw))
        return _valid_persisted_record(data)
    except (RuntimeError, ValueError, TypeError) as exc:
        print(f"WARNING: Yahoo token could not be loaded from Upstash: {exc}")
        return None


def _persist_record(record: Dict[str, Any]) -> None:
    # Upstash is the preferred free persistence layer. It survives Render
    # spin-downs, deploys, and restarts and is shared by desktop/mobile users.
    if _persist_to_upstash(record):
        return

    # A persistent disk remains supported as a secondary deployment option.
    path = _token_file_path()
    if path is None:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(path)
    except OSError:
        return


def _load_persisted_record() -> Optional[Dict[str, Any]]:
    upstash_record = _load_from_upstash()
    if upstash_record:
        return upstash_record

    path = _token_file_path()
    if path is not None and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            record = _valid_persisted_record(data)
            if record:
                return record
        except (OSError, ValueError, TypeError):
            pass

    # Optional bootstrap for deployments that already have a securely stored
    # Yahoo refresh token.
    refresh_token = os.getenv(BOOTSTRAP_REFRESH_ENV, "").strip()
    if refresh_token:
        return {
            "access_token": None,
            "refresh_token": refresh_token,
            "expires_at": 0,
        }

    return None


def _shared_store_tokens(request: Request, token_data: Dict[str, Any]) -> None:
    session_id = yahoo_auth._session_id(request)

    # Prefer the globally cached/persisted refresh token if Yahoo omits a new
    # refresh token during an access-token refresh.
    existing = _shared_get_stored_tokens(request) or {}
    record = _normalize_record(token_data, existing)

    with yahoo_auth._TOKEN_STORE_LOCK:
        if session_id:
            yahoo_auth._TOKEN_STORE[session_id] = dict(record)
        yahoo_auth._TOKEN_STORE[GLOBAL_TOKEN_SLOT] = dict(record)

    # Persist every successful authorization/refresh. Yahoo can rotate refresh
    # tokens, so the newest value must replace the old one in Upstash.
    _persist_record(record)


def _shared_get_stored_tokens(request: Request) -> Optional[Dict[str, Any]]:
    session_id = yahoo_auth._session_id(request, create=False)

    with yahoo_auth._TOKEN_STORE_LOCK:
        if session_id:
            token_data = yahoo_auth._TOKEN_STORE.get(session_id)
            if token_data:
                return dict(token_data)

        token_data = yahoo_auth._TOKEN_STORE.get(GLOBAL_TOKEN_SLOT)
        if token_data:
            return dict(token_data)

    persisted = _load_persisted_record()
    if not persisted:
        return None

    with yahoo_auth._TOKEN_STORE_LOCK:
        yahoo_auth._TOKEN_STORE[GLOBAL_TOKEN_SLOT] = dict(persisted)
        if session_id:
            yahoo_auth._TOKEN_STORE[session_id] = dict(persisted)

    return dict(persisted)


def install_shared_yahoo_auth() -> None:
    """Make one persistent Yahoo authorization available to all Mamba viewers."""

    yahoo_auth._store_tokens = _shared_store_tokens
    yahoo_auth._get_stored_tokens = _shared_get_stored_tokens

    persisted = _load_persisted_record()
    if persisted:
        with yahoo_auth._TOKEN_STORE_LOCK:
            yahoo_auth._TOKEN_STORE[GLOBAL_TOKEN_SLOT] = dict(persisted)


@storage_status_router.get("/storage-status")
def yahoo_storage_status():
    """Report persistence health without exposing any OAuth credentials."""

    configured = _upstash_config() is not None
    record = _load_from_upstash() if configured else None

    return {
        "upstash_configured": configured,
        "persistent_yahoo_authorization_found": bool(
            record and record.get("refresh_token")
        ),
        "storage": "upstash" if configured else "memory-only",
        "message": (
            "Yahoo authorization is persisted in Upstash."
            if record and record.get("refresh_token")
            else (
                "Upstash is configured, but no Yahoo refresh token has been saved yet. Reconnect Yahoo once."
                if configured
                else "Upstash is not configured."
            )
        ),
    }
