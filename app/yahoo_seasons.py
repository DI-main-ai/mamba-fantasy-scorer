import os
import re
import urllib.parse
from collections import deque
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, HTTPException, Request

from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import _league_metadata, _target_league_key


season_router = APIRouter(
    prefix="/auth/yahoo/mamba",
    tags=["yahoo-mamba-seasons"],
)

# Yahoo's explicit renewal chain stops at 2014 for this league. These optional
# environment variables let us attach the verified predecessor leagues without
# exposing private league identifiers in the public repository.
MANUAL_LEAGUE_ENV_BY_SEASON = {
    2013: "YAHOO_LEAGUE_KEY_2013",
    2012: "YAHOO_LEAGUE_KEY_2012",
    2011: "YAHOO_LEAGUE_KEY_2011",
}


def _walk_values_for_key(node: Any, wanted_key: str) -> Iterable[Any]:
    if isinstance(node, dict):
        if wanted_key in node:
            yield node[wanted_key]
        for value in node.values():
            yield from _walk_values_for_key(value, wanted_key)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_values_for_key(value, wanted_key)


def _scalar_map(node: Any) -> Dict[str, Any]:
    values: Dict[str, Any] = {}

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                values.setdefault(str(key), item)
            else:
                visit(item)

    visit(node)
    return values


def _normalize_league_key(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, (dict, list)):
        for candidate in _walk_values_for_key(value, "league_key"):
            normalized = _normalize_league_key(candidate)
            if normalized:
                return normalized
        for candidate in _walk_values_for_key(value, "value"):
            normalized = _normalize_league_key(candidate)
            if normalized:
                return normalized
        return None

    text = str(value).strip()
    if not text:
        return None

    canonical = re.search(r"(\d+\.l\.\d+)", text)
    if canonical:
        return canonical.group(1)

    legacy = re.search(r"(?:^|[^0-9])(\d+)_(\d+)(?:$|[^0-9])", text)
    if legacy:
        return f"{legacy.group(1)}.l.{legacy.group(2)}"

    return None


def _first_value(node: Any, key: str) -> Any:
    return next(_walk_values_for_key(node, key), None)


def _league_record(resource: Any) -> Optional[Dict[str, Any]]:
    fields = _scalar_map(resource)
    league_key = _normalize_league_key(fields.get("league_key"))
    season = fields.get("season")

    if not league_key or season in (None, ""):
        return None

    try:
        season_number = int(season)
    except (TypeError, ValueError):
        return None

    return {
        "league_key": league_key,
        "season": season_number,
        "name": str(fields.get("name") or ""),
        "renew": _normalize_league_key(_first_value(resource, "renew")),
        "renewed": _normalize_league_key(_first_value(resource, "renewed")),
    }


def _manual_legacy_records(request: Request) -> List[Dict[str, Any]]:
    """Fetch manually verified predecessor leagues from Render env vars."""

    records: List[Dict[str, Any]] = []

    for expected_season, env_name in MANUAL_LEAGUE_ENV_BY_SEASON.items():
        raw_key = os.getenv(env_name, "").strip()
        league_key = _normalize_league_key(raw_key)
        if not league_key:
            continue

        encoded_key = urllib.parse.quote(league_key, safe=".-_")
        payload = _fantasy_get(request, f"league/{encoded_key}")
        resource = payload.get("fantasy_content", {}).get("league", {})
        record = _league_record(resource)

        if record is None:
            metadata = _league_metadata(payload)
            record = {
                "league_key": league_key,
                "season": int(metadata.get("season") or expected_season),
                "name": str(metadata.get("name") or ""),
                "renew": _normalize_league_key(_first_value(resource, "renew")),
                "renewed": _normalize_league_key(_first_value(resource, "renewed")),
            }

        # The env var itself declares which Mamba season this key belongs to.
        # Keep Yahoo's metadata for name/links, but pin the expected year so a
        # malformed response cannot attach a verified league to the wrong slot.
        record["season"] = expected_season
        records.append(record)

    return records


def discover_mamba_seasons(request: Request) -> List[Dict[str, Any]]:
    """Discover seasons linked to the configured Mamba league.

    Yahoo's `renew` field points to the previous season and `renewed` points
    to the next season. We build the chain in both directions and also follow
    reverse links returned by the user's complete NFL league history. Exact
    league-name matching is a fallback for manually linked history, and the
    verified 2011-2013 predecessor leagues can be supplied through environment
    variables when Yahoo's renewal chain no longer reaches them.
    """
    current_key = _target_league_key()
    encoded_current = urllib.parse.quote(current_key, safe=".-_")

    current_payload = _fantasy_get(request, f"league/{encoded_current}")
    current_resource = current_payload.get("fantasy_content", {}).get("league", {})
    current_record = _league_record(current_resource)

    if current_record is None:
        metadata = _league_metadata(current_payload)
        current_record = {
            "league_key": current_key,
            "season": int(metadata.get("season") or 2026),
            "name": str(metadata.get("name") or "The Mamba League"),
            "renew": _normalize_league_key(_first_value(current_resource, "renew")),
            "renewed": _normalize_league_key(_first_value(current_resource, "renewed")),
        }

    all_payload = _fantasy_get(
        request,
        "users;use_login=1/games;game_codes=nfl/leagues",
    )

    records: Dict[str, Dict[str, Any]] = {current_key: current_record}
    for resource in _walk_values_for_key(all_payload, "league"):
        record = _league_record(resource)
        if record:
            records[record["league_key"]] = record

    # Build an undirected graph from Yahoo's explicit renewal links so it
    # works regardless of which season the traversal starts from.
    adjacency: Dict[str, set] = {key: set() for key in records}
    for key, record in records.items():
        for linked in (record.get("renew"), record.get("renewed")):
            if linked:
                adjacency.setdefault(key, set()).add(linked)
                adjacency.setdefault(linked, set()).add(key)

    connected: List[Dict[str, Any]] = []
    queue = deque([current_key])
    visited = set()

    while queue:
        key = queue.popleft()
        if key in visited:
            continue
        visited.add(key)

        record = records.get(key)
        if record:
            connected.append(record)

        for neighbor in adjacency.get(key, set()):
            if neighbor not in visited:
                queue.append(neighbor)

    # Yahoo does not expose manually attached league-history entries through
    # renew/renewed. If the chain is sparse, include the user's leagues with
    # the exact same name as a pragmatic fallback.
    current_name = current_record.get("name", "").strip().casefold()
    if current_name:
        connected_keys = {item["league_key"] for item in connected}
        for record in records.values():
            if record.get("name", "").strip().casefold() == current_name:
                if record["league_key"] not in connected_keys:
                    connected.append(record)
                    connected_keys.add(record["league_key"])

    # Explicitly attach the verified 2011-2013 predecessors. This is the bridge
    # across Yahoo's broken 2013 -> 2014 renewal history.
    connected_keys = {item["league_key"] for item in connected}
    for record in _manual_legacy_records(request):
        if record["league_key"] not in connected_keys:
            connected.append(record)
            connected_keys.add(record["league_key"])

    # De-duplicate by season. The explicit/manual records above are already the
    # intended Mamba league for their respective years.
    by_season: Dict[int, Dict[str, Any]] = {}
    for record in sorted(connected, key=lambda item: -int(item["season"])):
        by_season.setdefault(int(record["season"]), record)

    return [by_season[season] for season in sorted(by_season, reverse=True)]


@season_router.get("/seasons")
def yahoo_mamba_seasons(request: Request):
    try:
        seasons = discover_mamba_seasons(request)
    except HTTPException:
        raise

    return {
        "seasons": seasons,
        "season_numbers": [int(item["season"]) for item in seasons],
        "current_league_key": _target_league_key(),
    }
