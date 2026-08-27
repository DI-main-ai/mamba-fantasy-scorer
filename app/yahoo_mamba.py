import html
import os
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.scoring import calculate_weekly_mamba_points
from app.yahoo_auth import _fantasy_get


mamba_yahoo_router = APIRouter(
    prefix="/auth/yahoo/mamba",
    tags=["yahoo-mamba"],
)


def _target_league_key() -> str:
    league_key = os.getenv("YAHOO_LEAGUE_KEY", "").strip()
    if not league_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Missing required server environment variable: "
                "YAHOO_LEAGUE_KEY"
            ),
        )
    return league_key


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


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _team_from_resource(team_resource: Any) -> Optional[Dict[str, Any]]:
    fields = _scalar_map(team_resource)
    team_key = fields.get("team_key")
    if not team_key:
        return None

    points_node = _first_value_for_key(team_resource, "team_points")
    points_fields = _scalar_map(points_node)

    standings_node = _first_value_for_key(team_resource, "team_standings")
    standings_fields = _scalar_map(standings_node)

    outcome_node = _first_value_for_key(standings_node, "outcome_totals")
    outcome_fields = _scalar_map(outcome_node)

    return {
        "team_key": str(team_key),
        "team_id": fields.get("team_id"),
        "name": fields.get("name") or "Unnamed Yahoo Team",
        "score": _as_float(points_fields.get("total")),
        "rank": standings_fields.get("rank"),
        "wins": outcome_fields.get("wins"),
        "losses": outcome_fields.get("losses"),
        "ties": outcome_fields.get("ties"),
        "points_for": _as_float(standings_fields.get("points_for")),
        "points_against": _as_float(standings_fields.get("points_against")),
    }


def _extract_unique_teams(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    teams: Dict[str, Dict[str, Any]] = {}

    for team_resource in _walk_values_for_key(payload, "team"):
        team = _team_from_resource(team_resource)
        if not team:
            continue
        key = team["team_key"]
        existing = teams.get(key, {})
        # Prefer later/non-empty values so standings/score payloads can enrich
        # the same team metadata without relying on Yahoo's array ordering.
        merged = dict(existing)
        for field, value in team.items():
            if value not in (None, ""):
                merged[field] = value
        teams[key] = merged

    return sorted(
        teams.values(),
        key=lambda team: int(team.get("team_id") or 9999),
    )


def _extract_matchups(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    matchups: List[Dict[str, Any]] = []
    seen_signatures = set()

    for matchup_resource in _walk_values_for_key(payload, "matchup"):
        matchup_fields = _scalar_map(matchup_resource)
        teams = _extract_unique_teams({"matchup": matchup_resource})

        if len(teams) < 2:
            continue

        signature = tuple(sorted(team["team_key"] for team in teams))
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)

        winner_team_key = matchup_fields.get("winner_team_key")
        matchups.append(
            {
                "week": matchup_fields.get("week"),
                "status": matchup_fields.get("status"),
                "is_playoffs": matchup_fields.get("is_playoffs"),
                "is_consolation": matchup_fields.get("is_consolation"),
                "winner_team_key": winner_team_key,
                "teams": teams[:2],
            }
        )

    return matchups


def _league_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
    league_resource = payload.get("fantasy_content", {}).get("league", {})
    fields = _scalar_map(league_resource)
    return {
        "league_key": fields.get("league_key"),
        "name": fields.get("name") or "The Mamba League",
        "num_teams": fields.get("num_teams"),
        "current_week": fields.get("current_week"),
        "start_week": fields.get("start_week"),
        "end_week": fields.get("end_week"),
        "season": fields.get("season"),
    }


def _format_score(value: Optional[float]) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}"


def _format_value(value: Any) -> str:
    return "—" if value in (None, "") else str(value)


def _render_snapshot(
    metadata: Dict[str, Any],
    teams: List[Dict[str, Any]],
    standings: List[Dict[str, Any]],
    matchups: List[Dict[str, Any]],
    week: int,
) -> str:
    standings_by_key = {
        team["team_key"]: team
        for team in standings
    }

    standings_rows = []
    standings_order = sorted(
        teams,
        key=lambda team: int(
            standings_by_key.get(team["team_key"], {}).get("rank") or 9999
        ),
    )

    for fallback_rank, team in enumerate(standings_order, start=1):
        standing = standings_by_key.get(team["team_key"], {})
        rank = standing.get("rank") or fallback_rank
        standings_rows.append(
            """
            <tr>
              <td>{rank}</td>
              <td>{name}</td>
              <td>{wins}-{losses}-{ties}</td>
              <td>{pf}</td>
              <td>{pa}</td>
            </tr>
            """.format(
                rank=html.escape(_format_value(rank)),
                name=html.escape(str(team.get("name") or "Unnamed Team")),
                wins=html.escape(_format_value(standing.get("wins") or 0)),
                losses=html.escape(_format_value(standing.get("losses") or 0)),
                ties=html.escape(_format_value(standing.get("ties") or 0)),
                pf=html.escape(_format_score(standing.get("points_for"))),
                pa=html.escape(_format_score(standing.get("points_against"))),
            )
        )

    score_map: Dict[str, float] = {}
    matchup_cards = []
    for index, matchup in enumerate(matchups, start=1):
        team_lines = []
        for team in matchup["teams"]:
            score = team.get("score")
            if score is not None:
                score_map[str(team["name"])] = float(score)
            winner = " ✓" if team["team_key"] == matchup.get("winner_team_key") else ""
            team_lines.append(
                "<div class=\"matchup-team\"><span>{}</span><strong>{}{}</strong></div>".format(
                    html.escape(str(team["name"])),
                    html.escape(_format_score(score)),
                    winner,
                )
            )

        matchup_cards.append(
            """
            <article class="matchup">
              <div class="matchup-label">Matchup {index}</div>
              {teams}
            </article>
            """.format(index=index, teams="".join(team_lines))
        )

    mamba_rows = []
    if len(score_map) == len(teams) and score_map:
        mamba_points = calculate_weekly_mamba_points(score_map)
        ordered = sorted(
            score_map,
            key=lambda name: (-mamba_points[name], -score_map[name], name.lower()),
        )
        for rank, name in enumerate(ordered, start=1):
            mamba_rows.append(
                "<tr><td>{}</td><td>{}</td><td>{:.2f}</td><td>{}</td></tr>".format(
                    rank,
                    html.escape(name),
                    score_map[name],
                    _format_value(mamba_points[name]),
                )
            )

    if mamba_rows:
        mamba_section = """
        <section class="card">
          <h2>Week {week} provisional Mamba Points</h2>
          <p class="note">Calculated directly from the Yahoo scores above using the existing Mamba scoring function.</p>
          <div class="table-wrap"><table>
            <thead><tr><th>Rank</th><th>Team</th><th>Yahoo Score</th><th>Mamba Pts</th></tr></thead>
            <tbody>{rows}</tbody>
          </table></div>
        </section>
        """.format(week=week, rows="".join(mamba_rows))
    else:
        mamba_section = """
        <section class="card">
          <h2>Week {week} Mamba calculation</h2>
          <p class="note">Yahoo has not returned a score for all {team_count} teams yet, so Mamba Points are intentionally not calculated.</p>
        </section>
        """.format(week=week, team_count=len(teams))

    previous_week = max(1, week - 1)
    next_week = min(18, week + 1)

    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Yahoo Live Audit - Mamba Fantasy</title>
      <style>
        body {{ margin:0; padding:28px 16px 50px; font-family:Arial,sans-serif; background:#0d0d0f; color:#f7f7f8; }}
        main {{ max-width:1050px; margin:0 auto; }}
        h1,h2 {{ color:#fdb927; }}
        h1 {{ margin-bottom:6px; }}
        .sub,.note {{ color:#b8b8c2; line-height:1.5; }}
        .card {{ background:#17171c; border:1px solid #303038; border-radius:16px; padding:20px; margin:18px 0; }}
        .meta-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }}
        .meta {{ background:#111116; border-radius:10px; padding:10px 12px; }}
        .actions {{ display:flex; gap:14px; flex-wrap:wrap; margin:18px 0 24px; }}
        a {{ color:#c7a7e8; }}
        .matchups {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:12px; }}
        .matchup {{ background:#111116; border:1px solid #292932; border-radius:12px; padding:14px; }}
        .matchup-label {{ color:#9999a3; font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:10px; }}
        .matchup-team {{ display:flex; justify-content:space-between; gap:16px; padding:7px 0; }}
        .table-wrap {{ overflow-x:auto; }}
        table {{ width:100%; border-collapse:collapse; min-width:600px; }}
        th,td {{ text-align:left; border-bottom:1px solid #303038; padding:10px 9px; }}
        th {{ color:#c7a7e8; }}
        code {{ color:#c7a7e8; overflow-wrap:anywhere; }}
        footer {{ color:#9999a3; font-size:13px; margin-top:28px; }}
      </style>
    </head>
    <body>
      <main>
        <h1>{league_name}</h1>
        <p class="sub">Live read-only Yahoo audit for the league Mamba will track. This page does not permanently store Yahoo Fantasy information.</p>
        <div class="meta-grid">
          <div class="meta"><strong>League key</strong><br><code>{league_key}</code></div>
          <div class="meta"><strong>Season</strong><br>{season}</div>
          <div class="meta"><strong>Yahoo teams</strong><br>{team_count}</div>
          <div class="meta"><strong>Yahoo current week</strong><br>{current_week}</div>
          <div class="meta"><strong>Viewing scoreboard</strong><br>Week {week}</div>
        </div>
        <div class="actions">
          <a href="/auth/yahoo/mamba/snapshot?week={previous_week}">← Week {previous_week}</a>
          <a href="/auth/yahoo/mamba/snapshot?week={week}">Refresh Week {week}</a>
          <a href="/auth/yahoo/mamba/snapshot?week={next_week}">Week {next_week} →</a>
          <a href="/auth/yahoo/leagues">League discovery</a>
          <a href="/">Mamba home</a>
        </div>
        <section class="card">
          <h2>Yahoo standings</h2>
          <div class="table-wrap"><table>
            <thead><tr><th>Rank</th><th>Team</th><th>W-L-T</th><th>PF</th><th>PA</th></tr></thead>
            <tbody>{standings_rows}</tbody>
          </table></div>
        </section>
        <section class="card">
          <h2>Week {week} Yahoo matchups</h2>
          <div class="matchups">{matchup_cards}</div>
        </section>
        {mamba_section}
        <footer>Fantasy data provided by <a href="https://football.fantasysports.yahoo.com/" target="_blank" rel="noopener noreferrer">Yahoo Fantasy</a>.</footer>
      </main>
    </body>
    </html>
    """.format(
        league_name=html.escape(str(metadata.get("name") or "The Mamba League")),
        league_key=html.escape(str(metadata.get("league_key") or _target_league_key())),
        season=html.escape(_format_value(metadata.get("season") or 2026)),
        team_count=len(teams),
        current_week=html.escape(_format_value(metadata.get("current_week"))),
        week=week,
        previous_week=previous_week,
        next_week=next_week,
        standings_rows="".join(standings_rows) or "<tr><td colspan=\"5\">No standings returned yet.</td></tr>",
        matchup_cards="".join(matchup_cards) or "<p class=\"note\">No matchup data returned for this week yet.</p>",
        mamba_section=mamba_section,
    )


@mamba_yahoo_router.get("", response_class=RedirectResponse)
def mamba_yahoo_home():
    return RedirectResponse("/auth/yahoo/mamba/snapshot?week=1")


@mamba_yahoo_router.get("/snapshot", response_class=HTMLResponse)
def mamba_yahoo_snapshot(
    request: Request,
    week: int = Query(default=1, ge=1, le=18),
):
    league_key = _target_league_key()
    encoded_key = urllib.parse.quote(league_key, safe=".-_")

    try:
        metadata_payload = _fantasy_get(
            request,
            f"league/{encoded_key}/metadata",
        )
        teams_payload = _fantasy_get(
            request,
            f"league/{encoded_key}/teams",
        )
        standings_payload = _fantasy_get(
            request,
            f"league/{encoded_key}/standings",
        )
        scoreboard_payload = _fantasy_get(
            request,
            f"league/{encoded_key}/scoreboard;week={week}",
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/auth/yahoo")
        raise

    metadata = _league_metadata(metadata_payload)
    teams = _extract_unique_teams(teams_payload)
    standings = _extract_unique_teams(standings_payload)
    matchups = _extract_matchups(scoreboard_payload)

    if not teams:
        raise HTTPException(
            status_code=502,
            detail="Yahoo returned no teams for the configured Mamba league.",
        )

    return HTMLResponse(
        _render_snapshot(
            metadata=metadata,
            teams=teams,
            standings=standings,
            matchups=matchups,
            week=week,
        )
    )
