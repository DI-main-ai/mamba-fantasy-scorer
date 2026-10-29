import asyncio
import json
import math
import threading
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from starlette.requests import Request as StarletteRequest

from app.yahoo_auth import _fantasy_get, _get_access_token, _refresh_access_token
from app.yahoo_dashboard import _extract_team_logo_urls
from app.yahoo_mamba import (
    _extract_unique_teams,
    _first_value_for_key,
    _league_metadata,
    _scalar_map,
    _walk_values_for_key,
)
from app.yahoo_shared_auth import _upstash_command, _upstash_config


roster_snapshot_router = APIRouter(tags=["league-roster-snapshot"])

_IS_WEEK_SELECTOR_TEST = "week-selector-test" in os.getenv(
    "YAHOO_REDIRECT_URI", ""
).lower()

ROSTER_SNAPSHOT_PREFIX = (
    "mamba:league-rosters:test:v1"
    if _IS_WEEK_SELECTOR_TEST
    else "mamba:league-rosters:v1"
)
SNAPSHOT_KEY = f"{ROSTER_SNAPSHOT_PREFIX}:latest"
LOCK_KEY = f"{ROSTER_SNAPSHOT_PREFIX}:refresh-lock"
REFRESH_SECONDS = 15 * 60
LOCK_SECONDS = 120
BENCH_POSITIONS = {"BN", "IR", "IL", "NA"}

_refresh_task: Optional[asyncio.Task] = None
_memory_snapshot: Optional[Dict[str, Any]] = None


def _server_request() -> StarletteRequest:
    """Build an internal request that can use Mamba's shared Yahoo OAuth token."""
    return StarletteRequest(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/internal/league-rosters",
            "raw_path": b"/internal/league-rosters",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 443),
            "session": {},
        }
    )


def _selected_position(player_resource: Any) -> str:
    node = _first_value_for_key(player_resource, "selected_position")
    fields = _scalar_map(node)
    return str(fields.get("position") or "")


def _player_name(player_resource: Any) -> str:
    node = _first_value_for_key(player_resource, "name")
    fields = _scalar_map(node)
    full = str(fields.get("full") or "").strip()
    if full:
        return full
    first = str(fields.get("first") or "").strip()
    last = str(fields.get("last") or "").strip()
    return " ".join(part for part in (first, last) if part) or "Unknown Player"


def _roster_group(slot: str) -> str:
    normalized = str(slot or "").upper()
    if normalized == "IR":
        return "ir"
    if normalized in {"IL", "NA"}:
        return "reserve"
    if normalized == "BN":
        return "bench"
    return "starter"


def _extract_roster_players(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    players: Dict[str, Dict[str, Any]] = {}

    for player_resource in _walk_values_for_key(payload, "player"):
        fields = _scalar_map(player_resource)
        player_key = fields.get("player_key")
        if not player_key:
            continue

        slot = _selected_position(player_resource)
        player = {
            "player_key": str(player_key),
            "name": _player_name(player_resource),
            "nfl_team": str(fields.get("editorial_team_abbr") or ""),
            "position": str(fields.get("display_position") or ""),
            "roster_slot": slot,
            "roster_group": _roster_group(slot),
            "status": str(fields.get("status") or ""),
            "status_full": str(fields.get("status_full") or ""),
        }
        players[player["player_key"]] = player

    group_order = {"starter": 0, "bench": 1, "ir": 2, "reserve": 3}
    return sorted(
        players.values(),
        key=lambda player: (
            group_order.get(str(player.get("roster_group") or ""), 9),
            str(player.get("roster_slot") or ""),
            str(player.get("name") or "").lower(),
        ),
    )


def _read_snapshot() -> Optional[Dict[str, Any]]:
    global _memory_snapshot

    if _upstash_config() is not None:
        try:
            raw = _upstash_command(["GET", SNAPSHOT_KEY])
            if raw not in (None, ""):
                value = json.loads(str(raw))
                if isinstance(value, dict):
                    _memory_snapshot = value
                    return value
        except Exception as exc:
            print(f"WARNING: roster snapshot read failed: {exc}")

    return dict(_memory_snapshot) if isinstance(_memory_snapshot, dict) else None


def _write_snapshot(snapshot: Dict[str, Any]) -> None:
    global _memory_snapshot
    _memory_snapshot = dict(snapshot)

    if _upstash_config() is None:
        return

    _upstash_command(
        [
            "SET",
            SNAPSHOT_KEY,
            json.dumps(snapshot, separators=(",", ":"), default=str),
        ]
    )


def _try_acquire_lock() -> bool:
    if _upstash_config() is None:
        return True

    try:
        result = _upstash_command(
            ["SET", LOCK_KEY, str(time.time()), "NX", "EX", LOCK_SECONDS]
        )
        return result == "OK"
    except Exception as exc:
        print(f"WARNING: roster snapshot lock failed: {exc}")
        return True


def _release_lock() -> None:
    if _upstash_config() is None:
        return
    try:
        _upstash_command(["DEL", LOCK_KEY])
    except Exception:
        pass


def refresh_roster_snapshot() -> Dict[str, Any]:
    """Fetch and atomically store one complete current-league roster snapshot."""
    if not _try_acquire_lock():
        existing = _read_snapshot()
        if existing:
            return existing
        raise RuntimeError("Roster snapshot refresh is already running.")

    try:
        request = _server_request()
        league_key = os.getenv("YAHOO_LEAGUE_KEY", "").strip()
        if not league_key:
            raise RuntimeError("YAHOO_LEAGUE_KEY is not configured.")

        encoded_league_key = urllib.parse.quote(league_key, safe=".-_")
        metadata_payload = _fantasy_get(request, f"league/{encoded_league_key}")
        metadata = _league_metadata(metadata_payload)

        teams_payload = _fantasy_get(request, f"league/{encoded_league_key}/teams")
        teams = _extract_unique_teams(teams_payload)
        logos = _extract_team_logo_urls(teams_payload)

        expected_team_count = int(metadata.get("num_teams") or len(teams) or 0)
        if expected_team_count <= 0 or len(teams) != expected_team_count:
            raise RuntimeError(
                f"Yahoo returned {len(teams)} teams; expected {expected_team_count}."
            )

        current_week = int(metadata.get("current_week") or 1)
        roster_teams: List[Dict[str, Any]] = []

        for team in teams:
            team_key = str(team.get("team_key") or "")
            if not team_key:
                raise RuntimeError("Yahoo returned a team without a team_key.")

            encoded_team_key = urllib.parse.quote(team_key, safe=".-_")
            roster_payload = _fantasy_get(
                request,
                f"team/{encoded_team_key}/roster;week={current_week}",
            )
            players = _extract_roster_players(roster_payload)
            if not players:
                raise RuntimeError(
                    f"Yahoo returned an empty roster for {team.get('name') or team_key}."
                )

            roster_teams.append(
                {
                    "team_key": team_key,
                    "team_id": team.get("team_id"),
                    "team_name": str(team.get("name") or "Yahoo Team"),
                    "logo_url": str(logos.get(team_key) or team.get("logo_url") or ""),
                    "roster_count": len(players),
                    "players": players,
                }
            )

        roster_teams.sort(
            key=lambda team: (
                int(team.get("team_id") or 9999),
                str(team.get("team_name") or "").lower(),
            )
        )

        updated_epoch = time.time()
        snapshot = {
            "success": True,
            "read_only": True,
            "source": "Yahoo Fantasy Sports API",
            "league": {
                "league_key": league_key,
                "league_name": str(metadata.get("name") or "The Mamba League"),
                "season": int(metadata.get("season") or datetime.now(timezone.utc).year),
                "current_week": current_week,
                "team_count": len(roster_teams),
            },
            "updated_at": datetime.fromtimestamp(
                updated_epoch, tz=timezone.utc
            ).isoformat(),
            "updated_at_epoch": updated_epoch,
            "refresh_policy": {
                "on_app_start": True,
                "while_awake_seconds": REFRESH_SECONDS,
                "complete_snapshots_only": True,
            },
            "teams": roster_teams,
        }

        _write_snapshot(snapshot)
        return snapshot
    finally:
        _release_lock()


async def _refresh_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(refresh_roster_snapshot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"WARNING: league roster snapshot refresh failed: {exc}")

        try:
            await asyncio.sleep(REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise


def start_roster_snapshot_collector() -> None:
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _refresh_task = asyncio.create_task(_refresh_loop())


async def stop_roster_snapshot_collector() -> None:
    global _refresh_task
    task = _refresh_task
    _refresh_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@roster_snapshot_router.get("/api/league-rosters", response_class=JSONResponse)
def league_roster_snapshot():
    """Return the latest stored complete roster snapshot without calling Yahoo."""
    snapshot = _read_snapshot()
    if not snapshot:
        raise HTTPException(
            status_code=503,
            detail=(
                "The league roster snapshot has not been created yet. "
                "The background refresher will populate it after app startup."
            ),
        )

    return JSONResponse(
        snapshot,
        headers={
            "Cache-Control": "no-store",
            "X-Mamba-Snapshot": "read-only",
        },
    )


_projection_cache: Dict[Any, Any] = {}
_projection_lock = threading.Lock()


def _projected_points(payload: Dict[str, Any], player_key: str, week: int) -> Optional[float]:
    """Accept only Yahoo's projected total for the exact player and week."""
    for player in _walk_values_for_key(payload, "player"):
        if str(_scalar_map(player).get("player_key") or "") != player_key:
            continue
        for node in _walk_values_for_key(player, "player_projected_points"):
            fields = _scalar_map(node)
            if fields.get("coverage_type") != "week" or str(fields.get("week")) != str(week):
                continue
            try:
                value = float(fields["total"])
            except (KeyError, ValueError, TypeError):
                continue
            if math.isfinite(value):
                return value
    return None


def _projection_get(request: StarletteRequest, path: str) -> Dict[str, Any]:
    """Yahoo's projection host, using the same league OAuth authorization."""
    token = _get_access_token(request)
    url = "https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2/" + path + "?format=json"
    for attempt in range(2):
        upstream = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(upstream, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0:
                token = _refresh_access_token(request)
                continue
            raise HTTPException(status_code=502, detail=f"Yahoo projection request failed ({exc.code}).") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Yahoo projection request failed.") from exc
    raise HTTPException(status_code=502, detail="Yahoo projection request failed.")


@roster_snapshot_router.get("/api/player-projection", response_class=JSONResponse)
def player_projection(
    player_key: str = Query(..., pattern=r"^[0-9]+\.p\.[0-9]+$"),
    week: int = Query(..., ge=1, le=18),
):
    """Test Yahoo's league-specific weekly projection for a rostered player."""
    snapshot = _read_snapshot()
    if not snapshot:
        raise HTTPException(status_code=503, detail="The league roster snapshot is not ready.")
    league_key = os.getenv("YAHOO_LEAGUE_KEY", "").strip()
    if not league_key or snapshot.get("league", {}).get("league_key") != league_key:
        raise HTTPException(status_code=503, detail="The snapshot does not match the configured league.")
    player = next((
        player for team in snapshot.get("teams", []) for player in team.get("players", [])
        if player.get("player_key") == player_key
    ), None)
    if player is None:
        raise HTTPException(status_code=404, detail="Player is not in the current league roster snapshot.")

    cache_key = (league_key, player_key, week)
    with _projection_lock:
        cached = _projection_cache.get(cache_key)
        if cached and time.time() - cached[0] < REFRESH_SECONDS:
            return JSONResponse(cached[1], headers={"Cache-Control": "no-store"})
        path = (
            f"league/{urllib.parse.quote(league_key, safe='.-_')}/players;player_keys={player_key}"
            f"/stats;type=week;week={week};show_projected_stats=1"
        )
        try:
            payload = _projection_get(_server_request(), path)
        except HTTPException as exc:
            # Do not expose upstream response bodies or authentication details.
            status = 401 if exc.status_code == 401 else 502
            raise HTTPException(status_code=status, detail=(
                "Yahoo needs to be reconnected on the test site." if status == 401
                else (exc.detail if str(exc.detail).startswith("Yahoo projection request failed")
                      else "Yahoo could not return this player's projection. Try again later.")
            )) from exc
        returned_league = _league_metadata(payload).get("league_key")
        if returned_league != league_key:
            raise HTTPException(status_code=502, detail="Yahoo returned a different league context.")
        points = _projected_points(payload, player_key, week)
        now = time.time()
        result = {
            "success": True,
            "source": "Yahoo Fantasy Sports API",
            "league_key": league_key,
            "season": snapshot["league"].get("season"),
            "player_key": player_key,
            "player_name": player["name"],
            "week": week,
            "projected_points": points,
            "available": points is not None,
            "scoring": "Yahoo league scoring; total supplied by Yahoo without recalculation",
            "updated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }
        _projection_cache[cache_key] = (now, result)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})
