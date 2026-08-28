import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from app.yahoo_dashboard import load_yahoo_dashboard_data
from app.yahoo_shared_auth import _upstash_command, _upstash_config


CACHE_PREFIX = "mamba:yahoo:dashboard:v3"
LOCK_PREFIX = "mamba:yahoo:dashboard-lock:v3"
LIVE_REFRESH_SECONDS = 45
IDLE_REFRESH_SECONDS = 300
HISTORICAL_REFRESH_SECONDS = 3600


def _cache_key(season: int, requested_week: Optional[int]) -> str:
    week_part = "auto" if requested_week is None else str(int(requested_week))
    return f"{CACHE_PREFIX}:{int(season)}:{week_part}"


def _lock_key(season: int, requested_week: Optional[int]) -> str:
    week_part = "auto" if requested_week is None else str(int(requested_week))
    return f"{LOCK_PREFIX}:{int(season)}:{week_part}"


def _data_signature(data: Dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in data.items()
        if not str(key).startswith("_refresh_")
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _current_week_state(data: Dict[str, Any]) -> str:
    """Classify current-season activity as live, idle, or historical.

    Yahoo's scoreboard can expose current W/L direction before a matchup is
    final, so for the current season we treat any non-zero fantasy scoring as
    live activity. This favors timely Sunday updates; when nobody is viewing
    Mamba, no browser polling occurs and therefore no Yahoo traffic is created.
    """

    season = int(data.get("season") or 0)
    current_year = datetime.now(timezone.utc).year
    if season != current_year:
        return "historical"

    current_week = str(data.get("current_week") or "")

    if data.get("mode") in {"dashboard", "yahoo_only"}:
        scores = data.get("weeks", {}).get(current_week, {})
        has_nonzero_score = any(
            abs(float(value)) > 0.000001
            for value in scores.values()
            if value is not None
        )
        return "live" if has_nonzero_score else "idle"

    for matchup in data.get("matchups", []):
        for team in matchup.get("teams", []):
            score = team.get("score")
            if score is None:
                continue
            try:
                if abs(float(score)) > 0.000001:
                    return "live"
            except (TypeError, ValueError):
                pass

    return "idle"


def _recommended_refresh_seconds(data: Dict[str, Any]) -> int:
    state = _current_week_state(data)
    if state == "live":
        return LIVE_REFRESH_SECONDS
    if state == "idle":
        return IDLE_REFRESH_SECONDS
    return HISTORICAL_REFRESH_SECONDS


def _read_cache(key: str) -> Optional[Dict[str, Any]]:
    if _upstash_config() is None:
        return None

    try:
        raw = _upstash_command(["GET", key])
        if raw in (None, ""):
            return None
        record = json.loads(str(raw))
        if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
            return None
        return record
    except Exception as exc:
        print(f"WARNING: Yahoo dashboard cache read failed: {exc}")
        return None


def _write_cache(key: str, data: Dict[str, Any], refreshed_at: float) -> None:
    if _upstash_config() is None:
        return

    record = {
        "refreshed_at": refreshed_at,
        "signature": _data_signature(data),
        "refresh_interval_seconds": _recommended_refresh_seconds(data),
        "data": data,
    }

    try:
        _upstash_command(
            ["SET", key, json.dumps(record, separators=(",", ":"), default=str)]
        )
    except Exception as exc:
        print(f"WARNING: Yahoo dashboard cache write failed: {exc}")


def _try_acquire_refresh_lock(season: int, requested_week: Optional[int]) -> bool:
    if _upstash_config() is None:
        return True

    try:
        result = _upstash_command(
            ["SET", _lock_key(season, requested_week), str(time.time()), "NX", "EX", 25]
        )
        return result == "OK"
    except Exception as exc:
        print(f"WARNING: Yahoo dashboard refresh lock failed: {exc}")
        return True


def _decorate_cached_data(
    record: Dict[str, Any],
    *,
    stale: bool,
    error: Optional[str] = None,
    refreshing: bool = False,
) -> Dict[str, Any]:
    data = dict(record["data"])
    data["_refresh_meta"] = {
        "refreshed_at": float(record.get("refreshed_at") or 0),
        "signature": str(record.get("signature") or _data_signature(data)),
        "refresh_interval_seconds": int(
            record.get("refresh_interval_seconds") or IDLE_REFRESH_SECONDS
        ),
        "stale": bool(stale),
        "refreshing": bool(refreshing),
        "error": error,
        "storage": "upstash-cache",
    }
    return data


def load_cached_yahoo_dashboard_data(
    request: Request,
    season: int,
    requested_week: Optional[int],
) -> Dict[str, Any]:
    """Load Yahoo dashboard data with shared Upstash caching and fallback.

    During live scoring, at most one Yahoo refresh is allowed every ~45 seconds
    for a given season/week view, even when many league members are watching.
    If Yahoo temporarily fails, the last successful cached snapshot is served.
    """

    key = _cache_key(season, requested_week)
    cached = _read_cache(key)
    now = time.time()

    if cached:
        age = max(0.0, now - float(cached.get("refreshed_at") or 0))
        fresh_for = int(
            cached.get("refresh_interval_seconds") or IDLE_REFRESH_SECONDS
        )
        if age < fresh_for:
            return _decorate_cached_data(cached, stale=False)

    lock_acquired = _try_acquire_refresh_lock(season, requested_week)
    if not lock_acquired and cached:
        return _decorate_cached_data(cached, stale=False, refreshing=True)

    try:
        data = load_yahoo_dashboard_data(
            request=request,
            season=season,
            requested_week=requested_week,
        )
        refreshed_at = time.time()
        _write_cache(key, data, refreshed_at)

        data = dict(data)
        data["_refresh_meta"] = {
            "refreshed_at": refreshed_at,
            "signature": _data_signature(data),
            "refresh_interval_seconds": _recommended_refresh_seconds(data),
            "stale": False,
            "refreshing": False,
            "error": None,
            "storage": "upstash-cache" if _upstash_config() else "direct-yahoo",
        }
        return data
    except HTTPException as exc:
        if cached:
            return _decorate_cached_data(
                cached,
                stale=True,
                error=f"Yahoo returned HTTP {exc.status_code}",
            )
        raise
    except Exception:
        if cached:
            return _decorate_cached_data(
                cached,
                stale=True,
                error="Yahoo refresh temporarily failed",
            )
        raise
