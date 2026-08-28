import urllib.parse
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, Request

from app.scoring import calculate_weekly_mamba_points
from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import _extract_matchups, _extract_unique_teams, _league_metadata
from app.yahoo_seasons import discover_mamba_seasons


HYBRID_START_SEASON = 2020
MAMBA_SCORING_END_WEEK = 13
SPECIAL_MAMBA_SCORING_END_WEEKS = {
    2024: 14,
}
MAX_YAHOO_WEEK = 18


def mamba_scoring_end_week(season: int) -> int:
    """Return the final week used for Mamba/Hybrid scoring for a season."""
    return SPECIAL_MAMBA_SCORING_END_WEEKS.get(int(season), MAMBA_SCORING_END_WEEK)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _resolve_season_record(request: Request, season: int) -> Dict[str, Any]:
    seasons = discover_mamba_seasons(request)
    selected = next(
        (item for item in seasons if int(item.get("season", 0)) == int(season)),
        None,
    )
    if selected is None:
        available = ", ".join(str(item.get("season")) for item in seasons)
        raise HTTPException(
            status_code=404,
            detail=f"Season {season} is not linked to The Mamba League. Available: {available}",
        )
    return dict(selected)


def _normalize_matchups(
    raw_matchups: List[Dict[str, Any]],
    team_names_by_key: Dict[str, str],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for matchup in raw_matchups:
        teams: List[Dict[str, Any]] = []
        for team in matchup.get("teams", []):
            key = str(team.get("team_key") or "")
            normalized_team = dict(team)
            if key in team_names_by_key:
                normalized_team["name"] = team_names_by_key[key]
            teams.append(normalized_team)
        if len(teams) >= 2:
            normalized.append({**matchup, "teams": teams[:2]})
    return normalized


def _result_for_team(
    matchup: Dict[str, Any],
    team: Dict[str, Any],
    opponent: Dict[str, Any],
) -> str:
    winner_key = matchup.get("winner_team_key")
    team_key = str(team.get("team_key") or "")
    if winner_key:
        return "W" if str(winner_key) == team_key else "L"

    team_score = team.get("score")
    opponent_score = opponent.get("score")
    if team_score is None or opponent_score is None:
        return "P"

    team_score = float(team_score)
    opponent_score = float(opponent_score)
    if team_score > opponent_score:
        return "W"
    if team_score < opponent_score:
        return "L"

    status = str(matchup.get("status") or "").lower()
    if status in {"postevent", "final", "complete", "completed"}:
        return "T"
    return "P"


def _matchups_have_scoring_data(matchups: List[Dict[str, Any]]) -> bool:
    final_statuses = {"postevent", "final", "complete", "completed"}
    for matchup in matchups:
        if matchup.get("winner_team_key"):
            return True
        if str(matchup.get("status") or "").lower() in final_statuses:
            return True
        for team in matchup.get("teams", []):
            score = team.get("score")
            if score is None:
                continue
            try:
                if float(score) != 0.0:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _latest_week_with_data(
    request: Request,
    encoded_key: str,
    team_names_by_key: Dict[str, str],
    current_week: int,
    end_week: int,
) -> int:
    newest_candidate = max(1, min(current_week, end_week))
    for week_number in range(newest_candidate, 0, -1):
        scoreboard_payload = _fantasy_get(
            request, f"league/{encoded_key}/scoreboard;week={week_number}"
        )
        matchups = _normalize_matchups(
            _extract_matchups(scoreboard_payload), team_names_by_key
        )
        if _matchups_have_scoring_data(matchups):
            return week_number
    return 1


def load_yahoo_dashboard_data(
    request: Request,
    season: int,
    requested_week: Optional[int],
) -> Dict[str, Any]:
    """Load one Mamba League season from Yahoo for the shared dashboard UI.

    Hybrid/Mamba scoring exists only for seasons 2020 and newer. Seasons before
    2020 are loaded as Yahoo-only history: cumulative Yahoo standings through
    the selected week plus the official head-to-head matchups for that week.
    """
    season_record = _resolve_season_record(request, season)
    league_key = str(season_record["league_key"])
    encoded_key = urllib.parse.quote(league_key, safe=".-_")

    metadata_payload = _fantasy_get(request, f"league/{encoded_key}")
    metadata = _league_metadata(metadata_payload)
    metadata.update({"league_key": league_key, "season": season})

    end_week = _as_int(metadata.get("end_week"), MAX_YAHOO_WEEK)
    end_week = max(1, min(end_week, MAX_YAHOO_WEEK))
    current_week = _as_int(metadata.get("current_week"), 1)
    hybrid_scoring_enabled = int(season) >= HYBRID_START_SEASON
    scoring_end_week = min(mamba_scoring_end_week(season), end_week)

    teams_payload = _fantasy_get(request, f"league/{encoded_key}/teams")
    teams = _extract_unique_teams(teams_payload)
    team_names_by_key = {
        str(team["team_key"]): str(team["name"])
        for team in teams
    }

    if requested_week is None:
        if season >= 2026:
            selected_week = _latest_week_with_data(
                request=request,
                encoded_key=encoded_key,
                team_names_by_key=team_names_by_key,
                current_week=current_week,
                end_week=end_week,
            )
        elif hybrid_scoring_enabled:
            selected_week = scoring_end_week
        else:
            # Keep historical Yahoo-only seasons opening at the traditional
            # Week 13 regular-season checkpoint while still allowing any week
            # to be selected from the dropdown.
            selected_week = min(MAMBA_SCORING_END_WEEK, end_week)
    else:
        selected_week = max(1, min(int(requested_week), end_week))

    available_weeks = list(range(1, end_week + 1))

    # For Hybrid-era seasons, weeks after the Mamba scoring cutoff remain
    # matchup-only. Yahoo-only seasons do not use this cutoff and continue to
    # show Yahoo standings plus matchups for whichever week is selected.
    if hybrid_scoring_enabled and selected_week > scoring_end_week:
        scoreboard_payload = _fantasy_get(
            request, f"league/{encoded_key}/scoreboard;week={selected_week}"
        )
        matchups = _normalize_matchups(
            _extract_matchups(scoreboard_payload), team_names_by_key
        )
        return {
            "mode": "matchups",
            "season": season,
            "league_key": league_key,
            "league_name": metadata.get("name") or "The Mamba League",
            "current_week": selected_week,
            "maximum_week": end_week,
            "available_weeks": available_weeks,
            "teams": teams,
            "matchups": matchups,
            "weeks": {},
            "weekly_mamba_points": {},
            "yahoo_teams": {},
            "hybrid_scoring_enabled": True,
        }

    week_numbers = [str(number) for number in range(1, selected_week + 1)]
    weeks: Dict[str, Dict[str, float]] = {}
    weekly_mamba_points: Dict[str, Dict[str, float]] = {}
    yahoo_teams: Dict[str, Dict[str, Any]] = {
        name: {"weekly_results": {}}
        for name in team_names_by_key.values()
    }
    selected_week_matchups: List[Dict[str, Any]] = []

    for week_number in range(1, selected_week + 1):
        scoreboard_payload = _fantasy_get(
            request, f"league/{encoded_key}/scoreboard;week={week_number}"
        )
        matchups = _normalize_matchups(
            _extract_matchups(scoreboard_payload), team_names_by_key
        )
        if week_number == selected_week:
            selected_week_matchups = matchups

        week_scores: Dict[str, float] = {}
        for matchup in matchups:
            matchup_teams = matchup.get("teams", [])
            if len(matchup_teams) < 2:
                continue
            first, second = matchup_teams[0], matchup_teams[1]
            for team, opponent in ((first, second), (second, first)):
                name = str(team.get("name") or "")
                score = team.get("score")
                if not name or score is None:
                    continue
                week_scores[name] = float(score)
                yahoo_teams.setdefault(name, {"weekly_results": {}})
                yahoo_teams[name]["weekly_results"][str(week_number)] = (
                    _result_for_team(matchup, team, opponent)
                )

        expected_team_names = set(team_names_by_key.values())
        if set(week_scores) != expected_team_names:
            missing = sorted(expected_team_names - set(week_scores))
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Yahoo did not return complete Week {week_number} scores "
                    f"for season {season}. Missing teams: {', '.join(missing)}"
                ),
            )

        missing_results = [
            name
            for name in expected_team_names
            if str(week_number)
            not in yahoo_teams.get(name, {}).get("weekly_results", {})
        ]
        if missing_results:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Yahoo did not return complete Week {week_number} matchup "
                    f"results for season {season}."
                ),
            )

        weeks[str(week_number)] = week_scores
        if hybrid_scoring_enabled:
            weekly_mamba_points[str(week_number)] = calculate_weekly_mamba_points(
                week_scores
            )

    return {
        "mode": "dashboard" if hybrid_scoring_enabled else "yahoo_only",
        "season": season,
        "league_key": league_key,
        "league_name": metadata.get("name") or "The Mamba League",
        "current_week": selected_week,
        "maximum_week": end_week,
        "available_weeks": available_weeks,
        "teams": teams,
        "matchups": selected_week_matchups,
        "week_numbers": week_numbers,
        "weeks": weeks,
        "weekly_mamba_points": weekly_mamba_points,
        "yahoo_teams": yahoo_teams,
        "hybrid_scoring_enabled": hybrid_scoring_enabled,
    }