import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Request

import app.yahoo_auth as yahoo_auth


GLOBAL_TOKEN_SLOT = "__mamba_global_yahoo__"
TOKEN_FILE_ENV = "YAHOO_TOKEN_FILE"
BOOTSTRAP_REFRESH_ENV = "YAHOO_REFRESH_TOKEN"


def _token_file_path() -> Optional[Path]:
    """Return an optional persistent token path.

    On the free Render test service this is intentionally unset, so tokens are
    still lost when Render spins down. On a paid service with a persistent disk,
    set YAHOO_TOKEN_FILE to something such as /var/data/yahoo_tokens.json and
    the exact same code will survive restarts/deploys.
    """

    value = os.getenv(TOKEN_FILE_ENV, "").strip()
    return Path(value) if value else None


def _normalize_record(token_data: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = token_data.get("refresh_token") or existing.get("refresh_token")
    expires_in = int(token_data.get("expires_in") or 3600)

    return {
        "access_token": token_data.get("access_token"),
        "refresh_token": refresh_token,
        "expires_at": time.time() + max(expires_in - 60, 60),
    }


def _persist_record(record: Dict[str, Any]) -> None:
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
        # Authentication should keep working in memory even if persistence is
        # unavailable on the current Render instance.
        return


def _load_persisted_record() -> Optional[Dict[str, Any]]:
    path = _token_file_path()
    if path is not None and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("refresh_token"):
                return {
                    "access_token": data.get("access_token"),
                    "refresh_token": data.get("refresh_token"),
                    "expires_at": float(data.get("expires_at") or 0),
                }
        except (OSError, ValueError, TypeError):
            pass

    # Optional bootstrap for deployments that already have a securely stored
    # Yahoo refresh token. This is not required for the current test service.
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

    with yahoo_auth._TOKEN_STORE_LOCK:
        existing = yahoo_auth._TOKEN_STORE.get(GLOBAL_TOKEN_SLOT, {})
        record = _normalize_record(token_data, existing)

        # Keep the browser-specific entry for compatibility with the existing
        # test routes, but also keep one application-wide token. Mamba tracks a
        # single private league through one approved Yahoo account, so visitors
        # do not need to individually authorize Yahoo just to view standings.
        if session_id:
            yahoo_auth._TOKEN_STORE[session_id] = dict(record)
        yahoo_auth._TOKEN_STORE[GLOBAL_TOKEN_SLOT] = dict(record)

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
    """Make one Yahoo authorization available to every Mamba viewer.

    This fixes desktop-vs-mobile browser isolation immediately. Persistence
    across Render restarts is enabled automatically when YAHOO_TOKEN_FILE is
    pointed at a persistent disk.
    """

    yahoo_auth._store_tokens = _shared_store_tokens
    yahoo_auth._get_stored_tokens = _shared_get_stored_tokens

    persisted = _load_persisted_record()
    if persisted:
        with yahoo_auth._TOKEN_STORE_LOCK:
            yahoo_auth._TOKEN_STORE[GLOBAL_TOKEN_SLOT] = dict(persisted)
