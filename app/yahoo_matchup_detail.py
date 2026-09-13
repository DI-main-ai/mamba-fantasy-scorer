import hashlib
import json
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.routes import templates
from app.yahoo_auth import _fantasy_get
from app.yahoo_live_cache import load_cached_yahoo_dashboard_data
from app.yahoo_mamba import (
    _first_value_for_key,
    _scalar_map,
    _walk_values_for_key,
)
from app.yahoo_shared_auth import _upstash_command, _upstash_config


matchup_detail_router = APIRouter(tags=["yahoo-matchup-detail"])

DETAIL_CACHE_PREFIX = "mamba:yahoo:matchup-detail:v1"
STARTER_POSITION_ORDER = {
    "QB": 10,
    "RB": 20,
    "WR": 30,
    "TE": 40,
    "W/R": 50,
    "W/T": 50,
    "W/R/T": 50,
    "FLEX": 50,
    "Q/W/R/T": 55,
    "DEF": 60,
    "D/ST": 60,
    "K": 70,
    "BN": 900,
    "IR": 910,
}
BENCH_POSITIONS = {"BN", "IR", "IL", "NA"}


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _player_name(player_resource: Any) -> str:
    name_node = _first_value_for_key(player_resource, "name")
    fields = _scalar_map(name_node)
    full = fields.get("full")
    if full:
        return str(full)
    first = str(fields.get("first") or "").strip()
    last = str(fields.get("last") or "").strip()
    return " ".join(part for part in (first, last) if part) or "Unknown Player"


def _selected_position(player_resource: Any) -> str:
    position_node = _first_value_for_key(player_resource, "selected_position")
    fields = _scalar_map(position_node)
    return str(fields.get("position") or "—")


def _extract_roster_players(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    players: Dict[str, Dict[str, Any]] = {}
    for player_resource in _walk_values_for_key(payload, "player"):
        fields = _scalar_map(player_resource)
        player_key = fields.get("player_key")
        if not player_key:
            continue
        key = str(player_key)
        players[key] = {
            "player_key": key,
            "name": _player_name(player_resource),
            "selected_position": _selected_position(player_resource),
            "nfl_team": fields.get("editorial_team_abbr") or "",
            "display_position": fields.get("display_position") or "",
            "image_url": fields.get("image_url") or "",
            "points": None,
        }
    return list(players.values())


def _extract_player_points(payload: Dict[str, Any]) -> Dict[str, Optional[float]]:
    points: Dict[str, Optional[float]] = {}
    for player_resource in _walk_values_for_key(payload, "player"):
        fields = _scalar_map(player_resource)
        player_key = fields.get("player_key")
        if not player_key:
            continue
        points_node = _first_value_for_key(player_resource, "player_points")
        point_fields = _scalar_map(points_node)
        points[str(player_key)] = _as_float(point_fields.get("total"))
    return points


def _sort_roster(players: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        players,
        key=lambda player: (
            STARTER_POSITION_ORDER.get(str(player.get("selected_position") or ""), 500),
            str(player.get("name") or "").lower(),
        ),
    )


def _cache_key(season: int, week: int, team_keys: List[str]) -> str:
    material = "|".join(sorted(team_keys))
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:16]
    return f"{DETAIL_CACHE_PREFIX}:{int(season)}:{int(week)}:{digest}"


def _read_detail_cache(key: str) -> Optional[Dict[str, Any]]:
    if _upstash_config() is None:
        return None
    try:
        raw = _upstash_command(["GET", key])
        if not raw:
            return None
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else None
    except Exception as exc:
        print(f"WARNING: matchup detail cache read failed: {exc}")
        return None


def _write_detail_cache(key: str, value: Dict[str, Any], season: int) -> None:
    if _upstash_config() is None:
        return
    current_year = datetime.now(timezone.utc).year
    ttl_seconds = 60 if int(season) == current_year else 604800
    try:
        _upstash_command(
            [
                "SET",
                key,
                json.dumps(value, separators=(",", ":"), default=str),
                "EX",
                ttl_seconds,
            ]
        )
    except Exception as exc:
        print(f"WARNING: matchup detail cache write failed: {exc}")


def _load_team_roster(
    request: Request,
    league_key: str,
    team: Dict[str, Any],
    week: int,
) -> Dict[str, Any]:
    team_key = str(team.get("team_key") or "")
    if not team_key:
        raise HTTPException(status_code=404, detail="Yahoo team key is missing.")

    encoded_team_key = urllib.parse.quote(team_key, safe=".-_")
    roster_payload = _fantasy_get(
        request,
        f"team/{encoded_team_key}/roster;week={int(week)}",
    )
    players = _extract_roster_players(roster_payload)

    player_keys = [player["player_key"] for player in players]
    if player_keys:
        encoded_league_key = urllib.parse.quote(league_key, safe=".-_")
        encoded_player_keys = ",".join(
            urllib.parse.quote(player_key, safe=".-_")
            for player_key in player_keys
        )
        stats_payload = _fantasy_get(
            request,
            (
                f"league/{encoded_league_key}/players;player_keys={encoded_player_keys}"
                f"/stats;type=week;week={int(week)}"
            ),
        )
        points_by_key = _extract_player_points(stats_payload)
        for player in players:
            player["points"] = points_by_key.get(player["player_key"])

    players = _sort_roster(players)
    starters = [
        player
        for player in players
        if str(player.get("selected_position") or "") not in BENCH_POSITIONS
    ]
    bench = [
        player
        for player in players
        if str(player.get("selected_position") or "") in BENCH_POSITIONS
    ]

    return {
        "team_key": team_key,
        "name": str(team.get("name") or "Yahoo Team"),
        "logo_url": team.get("logo_url") or "",
        "score": team.get("score"),
        "starters": starters,
        "bench": bench,
    }


@matchup_detail_router.get("/matchup", response_class=HTMLResponse)
def matchup_detail(
    request: Request,
    season: int = Query(..., ge=2011, le=2100),
    week: int = Query(..., ge=1, le=18),
    matchup: int = Query(..., ge=0),
):
    dashboard = load_cached_yahoo_dashboard_data(
        request=request,
        season=season,
        requested_week=week,
    )
    matchups = dashboard.get("matchups", [])
    if matchup >= len(matchups):
        raise HTTPException(status_code=404, detail="That Yahoo matchup was not found.")

    selected_matchup = matchups[matchup]
    teams = list(selected_matchup.get("teams", []))[:2]
    if len(teams) != 2:
        raise HTTPException(status_code=404, detail="Yahoo did not return both matchup teams.")

    league_key = str(dashboard.get("league_key") or "")
    detail_key = _cache_key(
        season,
        week,
        [str(team.get("team_key") or "") for team in teams],
    )
    cached = _read_detail_cache(detail_key)

    if cached:
        team_details = cached.get("teams", [])
    else:
        team_details = [
            _load_team_roster(request, league_key, team, week)
            for team in teams
        ]
        _write_detail_cache(
            detail_key,
            {"teams": team_details},
            season,
        )

    winner_team_key = str(selected_matchup.get("winner_team_key") or "")
    for team_detail in team_details:
        team_detail["is_winner"] = bool(
            winner_team_key and team_detail.get("team_key") == winner_team_key
        )

    return templates.TemplateResponse(
        "matchup_detail.html",
        {
            "request": request,
            "page_title": f"Week {week} Matchup - Mamba Fantasy",
            "season": season,
            "current_week": week,
            "yahoo_source": True,
            "yahoo_refresh_epoch": 0,
            "live_refresh_enabled": False,
            "league_name": dashboard.get("league_name") or "The Mamba League",
            "teams": team_details,
            "matchup_status": str(selected_matchup.get("status") or ""),
            "back_url": f"/?season={season}&week={week}#weekly-matchups",
        },
    )
