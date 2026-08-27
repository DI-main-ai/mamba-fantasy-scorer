import html
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.scoring import calculate_weekly_mamba_points
from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import (
    _extract_matchups,
    _extract_unique_teams,
    _league_metadata,
    _scalar_map,
    _target_league_key,
)


history_router = APIRouter(
    prefix="/auth/yahoo/mamba",
    tags=["yahoo-mamba-history"],
)


def _walk_values_for_key(node: Any, wanted_key: str) -> Iterable[Any]:
    if isinstance(node, dict):
        if wanted_key in node:
            yield node[wanted_key]
        for value in node.values():
            yield from _walk_values_for_key(value, wanted_key)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_values_for_key(value, wanted_key)


def _first_value_for_key(node: Any, wanted_key: str) -> Any:
    return next(_walk_values_for_key(node, wanted_key), None)


def _normalize_league_key(value: Any) -> Optional[str]:
    """Convert Yahoo renewal-link formats into a canonical league key."""
    if value is None:
        return None

    if isinstance(value, dict):
        for key in ("league_key", "key", "value"):
            if key in value:
                normalized = _normalize_league_key(value[key])
                if normalized:
                    return normalized
        return None

    if isinstance(value, list):
        for item in value:
            normalized = _normalize_league_key(item)
            if normalized:
                return normalized
        return None

    text = str(value).strip()
    if not text:
        return None

    canonical = re.search(r"(\d+\.l\.\d+)", text)
    if canonical:
        return canonical.group(1)

    # Yahoo has historically exposed renewal references as GAME_LEAGUE.
    underscore = re.search(r"(?:^|[^0-9])(\d+)_(\d+)(?:$|[^0-9])", text)
    if underscore:
        return f"{underscore.group(1)}.l.{underscore.group(2)}"

    return None


def _metadata_with_links(payload: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _league_metadata(payload)
    league_resource = payload.get("fantasy_content", {}).get("league", {})

    renew = _normalize_league_key(_first_value_for_key(league_resource, "renew"))
    renewed = _normalize_league_key(
        _first_value_for_key(league_resource, "renewed")
    )

    metadata["renew"] = renew
    metadata["renewed"] = renewed
    return metadata


def _league_payload(request: Request, league_key: str) -> Dict[str, Any]:
    encoded = urllib.parse.quote(league_key, safe=".-_")
    return _fantasy_get(request, f"league/{encoded}")


def _discover_same_name_leagues(
    request: Request,
    target_name: str,
) -> Dict[str, Dict[str, Any]]:
    """Fallback for seasons Yahoo may not expose through renew/renewed links."""
    payload = _fantasy_get(
        request,
        "users;use_login=1/games;game_codes=nfl/leagues",
    )

    matches: Dict[str, Dict[str, Any]] = {}
    target_normalized = target_name.strip().casefold()

    for league_resource in _walk_values_for_key(payload, "league"):
        fields = _scalar_map(league_resource)
        key = fields.get("league_key")
        season = fields.get("season")
        name = fields.get("name")

        if not key or not season or not name:
            continue
        if str(name).strip().casefold() != target_normalized:
            continue

        matches[str(key)] = {
            "league_key": str(key),
            "name": str(name),
            "season": int(season),
            "num_teams": fields.get("num_teams"),
            "current_week": fields.get("current_week"),
            "start_week": fields.get("start_week"),
            "end_week": fields.get("end_week"),
            "renew": _normalize_league_key(fields.get("renew")),
            "renewed": _normalize_league_key(fields.get("renewed")),
        }

    return matches


def _discover_league_history(request: Request) -> List[Dict[str, Any]]:
    """Follow Yahoo's renewal chain and supplement it with exact-name history."""
    current_key = _target_league_key()
    current_payload = _league_payload(request, current_key)
    current = _metadata_with_links(current_payload)
    target_name = str(current.get("name") or "The Mamba League")

    by_key: Dict[str, Dict[str, Any]] = {}
    visited = set()

    def add_chain(start_key: Optional[str], direction: str) -> None:
        key = start_key
        for _ in range(30):
            if not key or key in visited:
                return
            visited.add(key)

            try:
                payload = _league_payload(request, key)
            except HTTPException:
                return

            metadata = _metadata_with_links(payload)
            resolved_key = str(metadata.get("league_key") or key)
            metadata["league_key"] = resolved_key
            by_key[resolved_key] = metadata
            key = metadata.get(direction)

    add_chain(current_key, "renew")

    # Walk forward as well in case this route is later used from an older key.
    forward_key = current.get("renewed")
    add_chain(forward_key, "renewed")

    # Yahoo's renewal fields only represent explicit renewals. Exact-name
    # discovery catches a manually linked historical season in many leagues.
    try:
        by_key.update(_discover_same_name_leagues(request, target_name))
    except HTTPException:
        pass

    history = [item for item in by_key.values() if item.get("season")]
    history.sort(key=lambda item: int(item["season"]), reverse=True)
    return history


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_score(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.2f}"


def _format_value(value: Any) -> str:
    return "—" if value in (None, "") else str(value)


def _render_history_page(
    history: List[Dict[str, Any]],
    selected: Dict[str, Any],
    teams: List[Dict[str, Any]],
    standings: List[Dict[str, Any]],
    matchups: List[Dict[str, Any]],
    week: int,
) -> str:
    season = int(selected["season"])
    league_key = str(selected["league_key"])
    end_week = int(selected.get("end_week") or selected.get("current_week") or 18)
    end_week = max(1, min(end_week, 18))

    season_options = "".join(
        '<option value="{season}"{selected_attr}>{season}</option>'.format(
            season=int(item["season"]),
            selected_attr=" selected" if int(item["season"]) == season else "",
        )
        for item in history
    )
    week_options = "".join(
        '<option value="{week_value}"{selected_attr}>Week {week_value}</option>'.format(
            week_value=week_value,
            selected_attr=" selected" if week_value == week else "",
        )
        for week_value in range(1, end_week + 1)
    )

    standings_by_key = {team["team_key"]: team for team in standings}
    standings_order = sorted(
        teams,
        key=lambda team: int(
            standings_by_key.get(team["team_key"], {}).get("rank") or 9999
        ),
    )

    standing_rows: List[str] = []
    for fallback_rank, team in enumerate(standings_order, start=1):
        standing = standings_by_key.get(team["team_key"], {})
        standing_rows.append(
            "<tr><td>{rank}</td><td>{name}</td><td>{wins}-{losses}-{ties}</td>"
            "<td>{pf}</td><td>{pa}</td></tr>".format(
                rank=html.escape(_format_value(standing.get("rank") or fallback_rank)),
                name=html.escape(str(team.get("name") or "Unnamed Team")),
                wins=html.escape(_format_value(standing.get("wins") or 0)),
                losses=html.escape(_format_value(standing.get("losses") or 0)),
                ties=html.escape(_format_value(standing.get("ties") or 0)),
                pf=html.escape(_format_score(_as_float(standing.get("points_for")))),
                pa=html.escape(_format_score(_as_float(standing.get("points_against")))),
            )
        )

    score_map: Dict[str, float] = {}
    matchup_cards: List[str] = []
    for index, matchup in enumerate(matchups, start=1):
        lines: List[str] = []
        for team in matchup.get("teams", []):
            score = _as_float(team.get("score"))
            if score is not None:
                score_map[str(team["name"])] = score
            winner = " ✓" if team.get("team_key") == matchup.get("winner_team_key") else ""
            lines.append(
                '<div class="matchup-team"><span>{name}</span><strong>{score}{winner}</strong></div>'.format(
                    name=html.escape(str(team.get("name") or "Unnamed Team")),
                    score=html.escape(_format_score(score)),
                    winner=winner,
                )
            )
        matchup_cards.append(
            '<article class="matchup"><div class="matchup-label">Matchup {}</div>{}</article>'.format(
                index,
                "".join(lines),
            )
        )

    if score_map and len(score_map) == len(teams):
        mamba_points = calculate_weekly_mamba_points(score_map)
        ordered = sorted(
            score_map,
            key=lambda name: (-mamba_points[name], -score_map[name], name.lower()),
        )
        mamba_rows = "".join(
            "<tr><td>{rank}</td><td>{name}</td><td>{score:.2f}</td><td>{points}</td></tr>".format(
                rank=rank,
                name=html.escape(name),
                score=score_map[name],
                points=_format_value(mamba_points[name]),
            )
            for rank, name in enumerate(ordered, start=1)
        )
        mamba_section = (
            '<section class="card"><h2>Week {week} Mamba Points</h2>'
            '<p class="note">Calculated live from Yahoo scores for this historical season.</p>'
            '<div class="table-wrap"><table><thead><tr><th>Rank</th><th>Team</th>'
            '<th>Yahoo Score</th><th>Mamba Pts</th></tr></thead><tbody>{rows}</tbody>'
            '</table></div></section>'
        ).format(week=week, rows=mamba_rows)
    else:
        mamba_section = (
            '<section class="card"><h2>Week {week} Mamba calculation</h2>'
            '<p class="note">Yahoo did not return scores for all {team_count} teams for this week.</p>'
            '</section>'
        ).format(week=week, team_count=len(teams))

    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Mamba League History</title>
      <style>
        body {{ margin:0; padding:28px 16px 50px; font-family:Arial,sans-serif; background:#0d0d0f; color:#f7f7f8; }}
        main {{ max-width:1050px; margin:0 auto; }}
        h1,h2 {{ color:#fdb927; }}
        .sub,.note {{ color:#b8b8c2; line-height:1.5; }}
        .toolbar {{ display:flex; gap:12px; flex-wrap:wrap; align-items:end; padding:16px; background:#17171c; border:1px solid #303038; border-radius:14px; margin:18px 0; }}
        .field {{ display:flex; flex-direction:column; gap:6px; }}
        label {{ color:#b8b8c2; font-size:12px; font-weight:700; text-transform:uppercase; }}
        select,button {{ background:#111116; color:#f7f7f8; border:1px solid #4a4a55; border-radius:9px; padding:10px 12px; font:inherit; }}
        button {{ color:#fdb927; cursor:pointer; font-weight:700; }}
        .meta-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin:18px 0; }}
        .meta {{ background:#111116; border-radius:10px; padding:10px 12px; }}
        .card {{ background:#17171c; border:1px solid #303038; border-radius:16px; padding:20px; margin:18px 0; }}
        .matchups {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px; }}
        .matchup {{ background:#111116; border:1px solid #292932; border-radius:12px; padding:14px; }}
        .matchup-label {{ color:#9999a3; font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:10px; }}
        .matchup-team {{ display:flex; justify-content:space-between; gap:16px; padding:7px 0; }}
        .table-wrap {{ overflow-x:auto; }}
        table {{ width:100%; border-collapse:collapse; min-width:600px; }}
        th,td {{ text-align:left; border-bottom:1px solid #303038; padding:10px 9px; }}
        th {{ color:#c7a7e8; }}
        a,code {{ color:#c7a7e8; }}
        footer {{ color:#9999a3; font-size:13px; margin-top:28px; }}
      </style>
    </head>
    <body>
      <main>
        <h1>{league_name} history</h1>
        <p class="sub">Choose any Yahoo season that can be linked to this league, then inspect any regular-season week live from Yahoo.</p>
        <form class="toolbar" method="get" action="/auth/yahoo/mamba/history">
          <div class="field"><label for="season">Season</label><select id="season" name="season" onchange="this.form.submit()">{season_options}</select></div>
          <div class="field"><label for="week">Week</label><select id="week" name="week" onchange="this.form.submit()">{week_options}</select></div>
          <button type="submit">Load</button>
        </form>
        <div class="meta-grid">
          <div class="meta"><strong>Season</strong><br>{season}</div>
          <div class="meta"><strong>League key</strong><br><code>{league_key}</code></div>
          <div class="meta"><strong>Teams</strong><br>{team_count}</div>
          <div class="meta"><strong>Viewing</strong><br>Week {week}</div>
        </div>
        <p><a href="/auth/yahoo/mamba/snapshot?week=1">Back to 2026 live audit</a></p>
        <section class="card"><h2>Yahoo standings</h2><div class="table-wrap"><table>
          <thead><tr><th>Rank</th><th>Team</th><th>W-L-T</th><th>PF</th><th>PA</th></tr></thead>
          <tbody>{standing_rows}</tbody>
        </table></div></section>
        <section class="card"><h2>Week {week} Yahoo matchups</h2><div class="matchups">{matchup_cards}</div></section>
        {mamba_section}
        <footer>Fantasy data provided by <a href="https://football.fantasysports.yahoo.com/" target="_blank" rel="noopener noreferrer">Yahoo Fantasy</a>.</footer>
      </main>
    </body>
    </html>
    """.format(
        league_name=html.escape(str(selected.get("name") or "The Mamba League")),
        season_options=season_options,
        week_options=week_options,
        season=season,
        league_key=html.escape(league_key),
        team_count=len(teams),
        week=week,
        standing_rows="".join(standing_rows) or '<tr><td colspan="5">No standings returned.</td></tr>',
        matchup_cards="".join(matchup_cards) or '<p class="note">No matchup data returned for this week.</p>',
        mamba_section=mamba_section,
    )


@history_router.get("/history", response_class=HTMLResponse)
def league_history(
    request: Request,
    season: Optional[int] = Query(default=None, ge=2000, le=2100),
    week: int = Query(default=1, ge=1, le=18),
):
    try:
        history = _discover_league_history(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/auth/yahoo")
        raise

    if not history:
        raise HTTPException(
            status_code=404,
            detail="Yahoo did not return any linked seasons for this league.",
        )

    if season is None:
        season = max(int(item["season"]) for item in history)

    selected = next(
        (item for item in history if int(item["season"]) == season),
        None,
    )
    if selected is None:
        available = ", ".join(str(item["season"]) for item in history)
        raise HTTPException(
            status_code=404,
            detail=f"Season {season} is not linked to this league. Available: {available}",
        )

    max_week = int(selected.get("end_week") or selected.get("current_week") or 18)
    max_week = max(1, min(max_week, 18))
    week = min(week, max_week)

    league_key = urllib.parse.quote(str(selected["league_key"]), safe=".-_")
    metadata_payload = _fantasy_get(request, f"league/{league_key}")
    teams_payload = _fantasy_get(request, f"league/{league_key}/teams")
    standings_payload = _fantasy_get(request, f"league/{league_key}/standings")
    scoreboard_payload = _fantasy_get(
        request,
        f"league/{league_key}/scoreboard;week={week}",
    )

    metadata = _metadata_with_links(metadata_payload)
    selected.update(metadata)
    teams = _extract_unique_teams(teams_payload)
    standings = _extract_unique_teams(standings_payload)
    matchups = _extract_matchups(scoreboard_payload)

    return HTMLResponse(
        _render_history_page(
            history=history,
            selected=selected,
            teams=teams,
            standings=standings,
            matchups=matchups,
            week=week,
        )
    )
