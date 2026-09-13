import asyncio
import hashlib
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.requests import Request as StarletteRequest

from app.routes import templates
from app.yahoo_auth import _fantasy_get
from app.yahoo_dashboard import _extract_team_logo_urls, _normalize_matchups
from app.yahoo_mamba import _extract_matchups, _extract_unique_teams, _league_metadata
from app.yahoo_matchup_detail import (
    BENCH_POSITIONS,
    _extract_player_points,
    _extract_roster_players,
)
from app.yahoo_shared_auth import _upstash_command, _upstash_config


scoring_log_router = APIRouter(tags=["scoring-log"])

SCORING_LOG_PREFIX = "mamba:scoring-log:v1"
CURRENT_STATUS_KEY = f"{SCORING_LOG_PREFIX}:current-status"
COLLECTOR_LOCK_KEY = f"{SCORING_LOG_PREFIX}:collector-lock"
MAX_EVENTS_PER_WEEK = 5000
PLAYER_BATCH_SIZE = 20
FULL_ROSTER_SCAN_SECONDS = 15 * 60
LIVE_POLL_SECONDS = 45
IDLE_POLL_SECONDS = 300
CENTRAL_TZ = ZoneInfo("America/Chicago")
EPSILON = 0.0001

_collector_task: Optional[asyncio.Task] = None


def _snapshot_key(season: int, week: int) -> str:
    return f"{SCORING_LOG_PREFIX}:{int(season)}:{int(week)}:snapshot"


def _events_key(season: int, week: int) -> str:
    return f"{SCORING_LOG_PREFIX}:{int(season)}:{int(week)}:events"


def _version_key(season: int, week: int) -> str:
    return f"{SCORING_LOG_PREFIX}:{int(season)}:{int(week)}:version"


def _status_key(season: int, week: int) -> str:
    return f"{SCORING_LOG_PREFIX}:{int(season)}:{int(week)}:status"


def _weeks_key(season: int) -> str:
    return f"{SCORING_LOG_PREFIX}:{int(season)}:weeks"


def _server_request() -> StarletteRequest:
    """Build an internal request that can use the shared Yahoo OAuth token."""
    return StarletteRequest(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/internal/scoring-log",
            "raw_path": b"/internal/scoring-log",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 443),
            "session": {},
        }
    )


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(key: str) -> Optional[Dict[str, Any]]:
    if _upstash_config() is None:
        return None
    try:
        raw = _upstash_command(["GET", key])
        if raw in (None, ""):
            return None
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else None
    except Exception as exc:
        print(f"WARNING: scoring log read failed for {key}: {exc}")
        return None


def _write_json(key: str, value: Dict[str, Any]) -> None:
    if _upstash_config() is None:
        raise RuntimeError("Upstash Redis is required for the scoring log.")
    _upstash_command(
        ["SET", key, json.dumps(value, separators=(",", ":"), default=str)]
    )


def _read_version(season: int, week: int) -> int:
    if _upstash_config() is None:
        return 0
    try:
        raw = _upstash_command(["GET", _version_key(season, week)])
        return int(raw or 0)
    except Exception:
        return 0


def _read_events(season: int, week: int) -> List[Dict[str, Any]]:
    if _upstash_config() is None:
        return []
    try:
        raw = _upstash_command(
            ["LRANGE", _events_key(season, week), 0, MAX_EVENTS_PER_WEEK - 1]
        )
        if not isinstance(raw, list):
            return []
        events: List[Dict[str, Any]] = []
        seen_ids = set()
        for item in raw:
            try:
                event = json.loads(str(item))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            event_id = str(event.get("event_id") or "")
            if event_id and event_id in seen_ids:
                continue
            if event_id:
                seen_ids.add(event_id)
            events.append(event)
        return events
    except Exception as exc:
        print(f"WARNING: scoring log event read failed: {exc}")
        return []


def _append_events(season: int, week: int, events: List[Dict[str, Any]]) -> int:
    if not events:
        return _read_version(season, week)
    if _upstash_config() is None:
        raise RuntimeError("Upstash Redis is required for the scoring log.")

    encoded = [json.dumps(event, separators=(",", ":"), default=str) for event in events]
    # LPUSH reverses multiple values, so reverse first to keep the newest event
    # at the left while preserving a stable order for events detected together.
    _upstash_command(["LPUSH", _events_key(season, week), *reversed(encoded)])
    _upstash_command(
        ["LTRIM", _events_key(season, week), 0, MAX_EVENTS_PER_WEEK - 1]
    )
    return int(_upstash_command(["INCR", _version_key(season, week)]) or 0)


def _register_week(season: int, week: int) -> None:
    if _upstash_config() is None:
        return
    try:
        _upstash_command(["SADD", _weeks_key(season), int(week)])
    except Exception as exc:
        print(f"WARNING: scoring log week index update failed: {exc}")


def _available_logged_weeks(season: int) -> List[int]:
    if _upstash_config() is None:
        return []
    try:
        raw = _upstash_command(["SMEMBERS", _weeks_key(season)])
        values = []
        for item in raw or []:
            try:
                values.append(int(item))
            except (TypeError, ValueError):
                continue
        return sorted(set(values))
    except Exception:
        return []


def _event_id(
    season: int,
    week: int,
    team_key: str,
    player_key: str,
    previous_total: float,
    new_total: float,
    event_type: str,
) -> str:
    material = (
        f"{season}|{week}|{team_key}|{player_key}|"
        f"{previous_total:.4f}|{new_total:.4f}|{event_type}"
    )
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:20]


def _make_event(
    *,
    season: int,
    week: int,
    team: Dict[str, Any],
    player: Dict[str, Any],
    previous_total: float,
    new_total: float,
    detected_at: float,
    event_type: str,
) -> Dict[str, Any]:
    team_key = str(team.get("team_key") or "")
    player_key = str(player.get("player_key") or "")
    delta = float(new_total) - float(previous_total)
    return {
        "event_id": _event_id(
            season,
            week,
            team_key,
            player_key,
            float(previous_total),
            float(new_total),
            event_type,
        ),
        "season": int(season),
        "week": int(week),
        "detected_at": float(detected_at),
        "event_type": event_type,
        "player_key": player_key,
        "player_name": str(player.get("name") or "Unknown Player"),
        "selected_position": str(player.get("selected_position") or ""),
        "display_position": str(player.get("display_position") or ""),
        "nfl_team": str(player.get("nfl_team") or ""),
        "team_key": team_key,
        "team_name": str(team.get("name") or "Yahoo Team"),
        "team_logo_url": str(team.get("logo_url") or ""),
        "matchup_index": team.get("matchup_index"),
        "previous_total": round(float(previous_total), 4),
        "new_total": round(float(new_total), 4),
        "delta": round(float(delta), 4),
    }


def _load_current_metadata(request: Request, league_key: str) -> Tuple[int, Dict[str, Any]]:
    encoded_key = urllib.parse.quote(league_key, safe=".-_")
    payload = _fantasy_get(request, f"league/{encoded_key}")
    metadata = _league_metadata(payload)
    try:
        week = int(metadata.get("current_week") or 1)
    except (TypeError, ValueError):
        week = 1
    return max(1, min(week, 18)), metadata


def _load_teams_and_scoreboard(
    request: Request,
    league_key: str,
    week: int,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    encoded_key = urllib.parse.quote(league_key, safe=".-_")
    teams_payload = _fantasy_get(request, f"league/{encoded_key}/teams")
    teams = _extract_unique_teams(teams_payload)
    team_logos_by_key = _extract_team_logo_urls(teams_payload)
    team_names_by_key: Dict[str, str] = {}

    for team in teams:
        team_key = str(team.get("team_key") or "")
        if team_key in team_logos_by_key:
            team["logo_url"] = team_logos_by_key[team_key]
        team_names_by_key[team_key] = str(team.get("name") or "Yahoo Team")

    scoreboard_payload = _fantasy_get(
        request, f"league/{encoded_key}/scoreboard;week={int(week)}"
    )
    matchups = _normalize_matchups(
        _extract_matchups(scoreboard_payload),
        team_names_by_key,
        team_logos_by_key,
    )

    matchup_by_team: Dict[str, int] = {}
    score_by_team: Dict[str, Optional[float]] = {}
    for matchup_index, matchup in enumerate(matchups):
        for team in matchup.get("teams", []):
            team_key = str(team.get("team_key") or "")
            matchup_by_team[team_key] = matchup_index
            score_by_team[team_key] = _as_float(team.get("score"))

    for team in teams:
        team_key = str(team.get("team_key") or "")
        team["matchup_index"] = matchup_by_team.get(team_key)
        team["score"] = score_by_team.get(team_key)

    return teams, matchups


def _load_starter_roster(
    request: Request,
    team_key: str,
    week: int,
) -> Dict[str, Dict[str, Any]]:
    encoded_team_key = urllib.parse.quote(team_key, safe=".-_")
    payload = _fantasy_get(
        request,
        f"team/{encoded_team_key}/roster;week={int(week)}",
    )
    players = _extract_roster_players(payload)
    starters = {}
    for player in players:
        if str(player.get("selected_position") or "") in BENCH_POSITIONS:
            continue
        player_key = str(player.get("player_key") or "")
        if not player_key:
            continue
        starters[player_key] = dict(player)
    return starters


def _fetch_player_points_batched(
    request: Request,
    league_key: str,
    week: int,
    player_keys: List[str],
) -> Dict[str, Optional[float]]:
    encoded_league_key = urllib.parse.quote(league_key, safe=".-_")
    results: Dict[str, Optional[float]] = {}
    unique_keys = list(dict.fromkeys(key for key in player_keys if key))

    for index in range(0, len(unique_keys), PLAYER_BATCH_SIZE):
        batch = unique_keys[index : index + PLAYER_BATCH_SIZE]
        encoded_keys = ",".join(
            urllib.parse.quote(player_key, safe=".-_") for player_key in batch
        )
        payload = _fantasy_get(
            request,
            (
                f"league/{encoded_league_key}/players;player_keys={encoded_keys}"
                f"/stats;type=week;week={int(week)}"
            ),
        )
        results.update(_extract_player_points(payload))

    return results


def _initialize_snapshot(
    request: Request,
    season: int,
    week: int,
    league_key: str,
    teams: List[Dict[str, Any]],
    detected_at: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    snapshot_teams: Dict[str, Dict[str, Any]] = {}
    all_player_keys: List[str] = []

    for team in teams:
        team_key = str(team.get("team_key") or "")
        if not team_key:
            continue
        roster = _load_starter_roster(request, team_key, week)
        all_player_keys.extend(roster.keys())
        snapshot_teams[team_key] = {
            "team_key": team_key,
            "name": str(team.get("name") or "Yahoo Team"),
            "logo_url": str(team.get("logo_url") or ""),
            "matchup_index": team.get("matchup_index"),
            "score": team.get("score"),
            "players": roster,
        }

    points_by_key = _fetch_player_points_batched(
        request, league_key, week, all_player_keys
    )

    imported_events: List[Dict[str, Any]] = []
    for team in snapshot_teams.values():
        for player_key, player in team.get("players", {}).items():
            points = points_by_key.get(player_key)
            player["points"] = points
            if points is None or abs(float(points)) <= EPSILON:
                continue
            imported_events.append(
                _make_event(
                    season=season,
                    week=week,
                    team=team,
                    player=player,
                    previous_total=0.0,
                    new_total=float(points),
                    detected_at=detected_at,
                    event_type="imported",
                )
            )

    snapshot = {
        "season": int(season),
        "week": int(week),
        "league_key": league_key,
        "initialized_at": detected_at,
        "refreshed_at": detected_at,
        "last_roster_refresh_at": detected_at,
        "teams": snapshot_teams,
    }
    return snapshot, imported_events


def _refresh_rosters(
    request: Request,
    snapshot: Dict[str, Any],
    teams: List[Dict[str, Any]],
    week: int,
) -> None:
    snapshot_teams = snapshot.setdefault("teams", {})
    for team in teams:
        team_key = str(team.get("team_key") or "")
        if not team_key:
            continue
        current = snapshot_teams.setdefault(
            team_key,
            {
                "team_key": team_key,
                "players": {},
            },
        )
        current["name"] = str(team.get("name") or current.get("name") or "Yahoo Team")
        current["logo_url"] = str(team.get("logo_url") or current.get("logo_url") or "")
        current["matchup_index"] = team.get("matchup_index")

        new_roster = _load_starter_roster(request, team_key, week)
        old_players = current.get("players", {})
        merged_players: Dict[str, Dict[str, Any]] = {}
        for player_key, player in new_roster.items():
            merged = dict(player)
            if player_key in old_players:
                merged["points"] = old_players[player_key].get("points")
            merged_players[player_key] = merged
        current["players"] = merged_players


def _collect_existing_snapshot(
    request: Request,
    snapshot: Dict[str, Any],
    season: int,
    week: int,
    league_key: str,
    teams: List[Dict[str, Any]],
    detected_at: float,
) -> List[Dict[str, Any]]:
    snapshot_teams: Dict[str, Dict[str, Any]] = snapshot.setdefault("teams", {})
    full_scan = (
        detected_at - float(snapshot.get("last_roster_refresh_at") or 0)
        >= FULL_ROSTER_SCAN_SECONDS
    )

    if full_scan:
        _refresh_rosters(request, snapshot, teams, week)
        snapshot["last_roster_refresh_at"] = detected_at

    incoming_by_key = {
        str(team.get("team_key") or ""): team
        for team in teams
        if team.get("team_key")
    }

    candidate_team_keys: List[str] = []
    for team_key, incoming in incoming_by_key.items():
        current = snapshot_teams.setdefault(
            team_key,
            {
                "team_key": team_key,
                "name": str(incoming.get("name") or "Yahoo Team"),
                "logo_url": str(incoming.get("logo_url") or ""),
                "matchup_index": incoming.get("matchup_index"),
                "score": incoming.get("score"),
                "players": {},
            },
        )
        current["name"] = str(incoming.get("name") or current.get("name") or "Yahoo Team")
        current["logo_url"] = str(incoming.get("logo_url") or current.get("logo_url") or "")
        current["matchup_index"] = incoming.get("matchup_index")

        previous_score = _as_float(current.get("score"))
        new_score = _as_float(incoming.get("score"))
        score_changed = (
            previous_score is not None
            and new_score is not None
            and abs(new_score - previous_score) > EPSILON
        )
        if full_scan or score_changed:
            candidate_team_keys.append(team_key)
        elif previous_score is None and new_score is not None:
            current["score"] = new_score

    all_player_keys: List[str] = []
    for team_key in candidate_team_keys:
        team = snapshot_teams.get(team_key, {})
        all_player_keys.extend((team.get("players") or {}).keys())

    if not all_player_keys:
        for team_key, incoming in incoming_by_key.items():
            snapshot_team = snapshot_teams.get(team_key)
            if snapshot_team is not None and snapshot_team.get("score") is None:
                snapshot_team["score"] = incoming.get("score")
        snapshot["refreshed_at"] = detected_at
        return []

    points_by_key = _fetch_player_points_batched(
        request, league_key, week, all_player_keys
    )

    events: List[Dict[str, Any]] = []
    teams_with_player_change = set()

    for team_key in candidate_team_keys:
        team = snapshot_teams.get(team_key, {})
        for player_key, player in (team.get("players") or {}).items():
            new_points = points_by_key.get(player_key)
            if new_points is None:
                continue
            previous_points = _as_float(player.get("points"))
            if previous_points is None:
                # A newly tracked starter is baselined without inventing a live
                # scoring event that may have happened before the roster refresh.
                player["points"] = float(new_points)
                continue

            delta = float(new_points) - float(previous_points)
            if abs(delta) > EPSILON:
                events.append(
                    _make_event(
                        season=season,
                        week=week,
                        team=team,
                        player=player,
                        previous_total=float(previous_points),
                        new_total=float(new_points),
                        detected_at=detected_at,
                        event_type="change",
                    )
                )
                teams_with_player_change.add(team_key)
            player["points"] = float(new_points)

    for team_key, incoming in incoming_by_key.items():
        current = snapshot_teams.get(team_key)
        if current is None:
            continue
        previous_score = _as_float(current.get("score"))
        new_score = _as_float(incoming.get("score"))
        if new_score is None:
            continue
        # If Yahoo's team total moved but player totals have not caught up yet,
        # keep the older team total so this team is retried on the next cycle.
        if (
            previous_score is not None
            and abs(new_score - previous_score) > EPSILON
            and team_key not in teams_with_player_change
            and not full_scan
        ):
            continue
        current["score"] = new_score

    snapshot["refreshed_at"] = detected_at
    return events


def _suggested_poll_seconds(now_epoch: Optional[float] = None) -> int:
    now = datetime.fromtimestamp(now_epoch or time.time(), tz=CENTRAL_TZ)
    weekday = now.weekday()  # Monday=0, Sunday=6
    hour = now.hour

    # Keep tight polling around normal NFL windows so individual changes are not
    # collapsed into large deltas. Outside game windows a five-minute heartbeat
    # is enough to notice unusual scheduling or stat corrections.
    live_window = (
        (weekday == 6 and 7 <= hour <= 23)  # Sunday, including London games
        or (weekday == 0 and 16 <= hour <= 23)  # Monday night
        or (weekday == 2 and 16 <= hour <= 23)  # occasional Wednesday/opening games
        or (weekday == 3 and 16 <= hour <= 23)  # Thursday night
        or (weekday == 5 and 9 <= hour <= 23)  # late-season Saturday games
    )
    return LIVE_POLL_SECONDS if live_window else IDLE_POLL_SECONDS


def _write_status(
    *,
    season: int,
    week: int,
    last_success: float,
    version: int,
    interval_seconds: int,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    status = {
        "season": int(season),
        "week": int(week),
        "last_success": float(last_success),
        "version": int(version),
        "interval_seconds": int(interval_seconds),
        "active": int(interval_seconds) <= LIVE_POLL_SECONDS,
        "error": error,
    }
    _write_json(_status_key(season, week), status)
    _write_json(CURRENT_STATUS_KEY, status)
    return status


def collect_scoring_log_once() -> Dict[str, Any]:
    if _upstash_config() is None:
        raise RuntimeError("Upstash Redis is not configured for the scoring log.")

    lock = _upstash_command(
        ["SET", COLLECTOR_LOCK_KEY, str(time.time()), "NX", "EX", 40]
    )
    if lock != "OK":
        current = _read_json(CURRENT_STATUS_KEY) or {}
        return {**current, "skipped": True}

    league_key = os.getenv("YAHOO_LEAGUE_KEY", "").strip()
    if not league_key:
        raise RuntimeError("YAHOO_LEAGUE_KEY is not configured.")

    request = _server_request()
    season = datetime.now(timezone.utc).year
    week, _ = _load_current_metadata(request, league_key)
    detected_at = time.time()
    teams, _ = _load_teams_and_scoreboard(request, league_key, week)
    snapshot = _read_json(_snapshot_key(season, week))

    if snapshot is None:
        snapshot, events = _initialize_snapshot(
            request,
            season,
            week,
            league_key,
            teams,
            detected_at,
        )
    else:
        events = _collect_existing_snapshot(
            request,
            snapshot,
            season,
            week,
            league_key,
            teams,
            detected_at,
        )

    _write_json(_snapshot_key(season, week), snapshot)
    _register_week(season, week)
    version = _append_events(season, week, events)
    interval = _suggested_poll_seconds(detected_at)
    status = _write_status(
        season=season,
        week=week,
        last_success=detected_at,
        version=version,
        interval_seconds=interval,
        error=None,
    )
    return {
        **status,
        "events_added": len(events),
    }


def _record_collector_error(exc: Exception) -> int:
    print(f"WARNING: scoring log collector failed: {exc}")
    current = _read_json(CURRENT_STATUS_KEY) or {}
    season = int(current.get("season") or datetime.now(timezone.utc).year)
    week = int(current.get("week") or 1)
    last_success = float(current.get("last_success") or 0)
    version = int(current.get("version") or _read_version(season, week))
    interval = IDLE_POLL_SECONDS
    try:
        _write_status(
            season=season,
            week=week,
            last_success=last_success,
            version=version,
            interval_seconds=interval,
            error=str(exc)[:240],
        )
    except Exception:
        pass
    return interval


async def _collector_loop() -> None:
    await asyncio.sleep(3)
    while True:
        interval = IDLE_POLL_SECONDS
        try:
            result = await asyncio.to_thread(collect_scoring_log_once)
            interval = int(result.get("interval_seconds") or IDLE_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            interval = _record_collector_error(exc)
        await asyncio.sleep(max(30, interval))


def start_scoring_log_collector() -> None:
    global _collector_task
    if _collector_task is None or _collector_task.done():
        _collector_task = asyncio.create_task(_collector_loop())


async def stop_scoring_log_collector() -> None:
    global _collector_task
    if _collector_task is None:
        return
    _collector_task.cancel()
    try:
        await _collector_task
    except asyncio.CancelledError:
        pass
    _collector_task = None


def _format_event_time(epoch: float) -> str:
    value = datetime.fromtimestamp(float(epoch), tz=CENTRAL_TZ)
    return value.strftime("%a %I:%M:%S %p").replace(" 0", " ") + " CT"


def _decorate_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    decorated = []
    for event in events:
        item = dict(event)
        epoch = float(item.get("detected_at") or 0)
        item["time_label"] = _format_event_time(epoch) if epoch else "—"
        delta = float(item.get("delta") or 0)
        item["delta_label"] = f"{delta:+.2f}"
        item["new_total_label"] = f"{float(item.get('new_total') or 0):.2f}"
        decorated.append(item)
    return decorated


@scoring_log_router.get("/scoring-log", response_class=HTMLResponse)
def scoring_log_page(
    request: Request,
    season: Optional[int] = Query(default=None, ge=2000, le=2100),
    week: Optional[int] = Query(default=None, ge=1, le=18),
    team: Optional[str] = Query(default=None),
    matchup: Optional[int] = Query(default=None, ge=0),
    order: str = Query(default="newest"),
):
    current = _read_json(CURRENT_STATUS_KEY) or {}
    selected_season = int(season or current.get("season") or datetime.now(timezone.utc).year)
    logged_weeks = _available_logged_weeks(selected_season)
    selected_week = int(
        week
        or current.get("week")
        or (logged_weeks[-1] if logged_weeks else 1)
    )

    snapshot = _read_json(_snapshot_key(selected_season, selected_week)) or {}
    status = _read_json(_status_key(selected_season, selected_week)) or {}
    events = _read_events(selected_season, selected_week)

    if team:
        events = [event for event in events if str(event.get("team_key") or "") == team]
    if matchup is not None:
        events = [
            event
            for event in events
            if event.get("matchup_index") is not None
            and int(event.get("matchup_index")) == int(matchup)
        ]
    if order == "oldest":
        events = list(reversed(events))
    else:
        order = "newest"

    teams = list((snapshot.get("teams") or {}).values())
    teams.sort(key=lambda item: str(item.get("name") or "").lower())

    matchup_groups: Dict[int, List[str]] = {}
    for snapshot_team in teams:
        matchup_index = snapshot_team.get("matchup_index")
        if matchup_index is None:
            continue
        matchup_groups.setdefault(int(matchup_index), []).append(
            str(snapshot_team.get("name") or "Yahoo Team")
        )
    matchup_options = [
        {
            "index": index,
            "label": " vs. ".join(names[:2]),
        }
        for index, names in sorted(matchup_groups.items())
    ]

    initialized_at = float(snapshot.get("initialized_at") or 0)
    last_success = float(status.get("last_success") or snapshot.get("refreshed_at") or 0)
    version = int(status.get("version") or _read_version(selected_season, selected_week))
    current_week = int(current.get("week") or selected_week)
    is_current_log = (
        selected_season == int(current.get("season") or selected_season)
        and selected_week == current_week
    )

    if selected_week not in logged_weeks and snapshot:
        logged_weeks.append(selected_week)
        logged_weeks.sort()

    return templates.TemplateResponse(
        "scoring_log.html",
        {
            "request": request,
            "page_title": f"Week {selected_week} Live Scoring Log - Mamba Fantasy",
            "season": selected_season,
            "current_week": selected_week,
            "yahoo_source": True,
            "yahoo_refresh_epoch": 0,
            "live_refresh_enabled": False,
            "scoring_log_header": True,
            "scoring_log_last_success": last_success,
            "scoring_log_active": bool(status.get("active")) and is_current_log,
            "scoring_log_error": status.get("error"),
            "scoring_log_version": version,
            "scoring_log_poll_seconds": 15 if is_current_log else 0,
            "events": _decorate_events(events),
            "event_count": len(events),
            "teams": teams,
            "matchup_options": matchup_options,
            "selected_team": team or "",
            "selected_matchup": matchup,
            "selected_order": order,
            "logged_weeks": logged_weeks,
            "tracking_started_label": (
                _format_event_time(initialized_at) if initialized_at else None
            ),
            "is_current_log": is_current_log,
            "show_bottom_matchups": False,
        },
    )


@scoring_log_router.get("/api/scoring-log/status", response_class=JSONResponse)
def scoring_log_status(
    season: int = Query(..., ge=2000, le=2100),
    week: int = Query(..., ge=1, le=18),
):
    status = _read_json(_status_key(season, week)) or {}
    current = _read_json(CURRENT_STATUS_KEY) or {}
    is_current = (
        int(current.get("season") or -1) == int(season)
        and int(current.get("week") or -1) == int(week)
    )
    return {
        "season": int(season),
        "week": int(week),
        "refreshed_at": float(status.get("last_success") or 0),
        "version": int(status.get("version") or _read_version(season, week)),
        "interval_seconds": int(status.get("interval_seconds") or IDLE_POLL_SECONDS),
        "active": bool(status.get("active")) and is_current,
        "stale": bool(status.get("error")),
        "error": status.get("error"),
        "current": is_current,
    }
