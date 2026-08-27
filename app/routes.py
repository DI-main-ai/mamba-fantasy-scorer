from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.data_loader import (
    load_historical_season,
    load_yahoo_wl_season,
)
from app.scoring import (
    build_hybrid_standings,
    validate_historical_mamba_points,
)


router = APIRouter()
templates = Jinja2Templates(directory="templates")


def format_rank(value: float) -> str:
    """Display whole-number ranks without a decimal."""

    numeric_value = float(value)

    if numeric_value.is_integer():
        return str(int(numeric_value))

    return f"{numeric_value:.1f}"


def format_number(value: float) -> str:
    """Display whole numbers cleanly and preserve half-points."""

    numeric_value = float(value)

    if numeric_value.is_integer():
        return str(int(numeric_value))

    return f"{numeric_value:.1f}"


templates.env.filters["format_rank"] = format_rank
templates.env.filters["format_number"] = format_number


def validate_team_sets(
    historical_teams: set,
    yahoo_teams: set,
) -> None:
    """Confirm that both historical files contain the same teams."""

    missing_from_yahoo = historical_teams - yahoo_teams
    missing_from_historical = yahoo_teams - historical_teams

    if missing_from_yahoo:
        missing_text = ", ".join(sorted(missing_from_yahoo))
        raise ValueError(
            "Teams missing from yahoo_wl_2025.json: "
            f"{missing_text}"
        )

    if missing_from_historical:
        missing_text = ", ".join(sorted(missing_from_historical))
        raise ValueError(
            "Teams missing from historical_2025.json: "
            f"{missing_text}"
        )


def build_yahoo_snapshot(
    yahoo_teams: Dict[str, Dict[str, Any]],
    weeks: Dict[str, Dict[str, float]],
    week_numbers: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """
    Recalculate Yahoo standings through the selected week.

    Teams are ranked by total wins, then cumulative Points For.
    """

    snapshot: Dict[str, Dict[str, Any]] = {}

    for team_name, team_data in yahoo_teams.items():
        weekly_results: List[Dict[str, Any]] = []
        total_wins = 0.0

        for week_number in week_numbers:
            result = team_data["weekly_results"].get(week_number)

            if result is None:
                raise ValueError(
                    "Yahoo W/L data is missing "
                    f"Week {week_number} for '{team_name}'."
                )

            if result == "W":
                total_wins += 1.0
            elif result == "T":
                total_wins += 0.5

            weekly_results.append(
                {
                    "week": int(week_number),
                    "result": result,
                    "is_win": result == "W",
                    "is_loss": result == "L",
                    "is_tie": result == "T",
                }
            )

        total_points_for = sum(
            float(weeks[week_number][team_name])
            for week_number in week_numbers
        )

        snapshot[team_name] = {
            "total_wins": total_wins,
            "total_points_for": total_points_for,
            "weekly_results": weekly_results,
        }

    ordered_team_names = sorted(
        snapshot.keys(),
        key=lambda team_name: (
            -float(snapshot[team_name]["total_wins"]),
            -float(snapshot[team_name]["total_points_for"]),
            team_name.lower(),
        ),
    )

    yahoo_ranks = {
        team_name: rank
        for rank, team_name in enumerate(
            ordered_team_names,
            start=1,
        )
    }

    rows: List[Dict[str, Any]] = []

    for team_name in ordered_team_names:
        rows.append(
            {
                "rank": yahoo_ranks[team_name],
                "team_name": team_name,
                "total_wins": snapshot[team_name]["total_wins"],
                "total_points_for": snapshot[team_name][
                    "total_points_for"
                ],
                "weekly_results": snapshot[team_name][
                    "weekly_results"
                ],
            }
        )

    return rows, yahoo_ranks


def build_points_for_audit_rows(
    weeks: Dict[str, Dict[str, float]],
    week_numbers: List[str],
    standings: List[Any],
) -> List[Dict[str, Any]]:
    """Build the cumulative Points For audit table."""

    total_points_for = {
        standing.team_name: float(standing.points_for)
        for standing in standings
    }

    weekly_highs = {
        week_number: max(
            float(score)
            for score in weeks[week_number].values()
        )
        for week_number in week_numbers
    }

    weekly_lows = {
        week_number: min(
            float(score)
            for score in weeks[week_number].values()
        )
        for week_number in week_numbers
    }

    ordered_team_names = sorted(
        total_points_for.keys(),
        key=lambda team_name: (
            -total_points_for[team_name],
            team_name.lower(),
        ),
    )

    rows: List[Dict[str, Any]] = []

    for rank, team_name in enumerate(
        ordered_team_names,
        start=1,
    ):
        weekly_values: List[Dict[str, Any]] = []

        for week_number in week_numbers:
            value = float(weeks[week_number][team_name])

            weekly_values.append(
                {
                    "value": value,
                    "is_high": value == weekly_highs[week_number],
                    "is_low": value == weekly_lows[week_number],
                }
            )

        rows.append(
            {
                "rank": rank,
                "team_name": team_name,
                "total": total_points_for[team_name],
                "weekly_values": weekly_values,
            }
        )

    return rows


def build_mamba_audit_rows(
    weekly_mamba_points: Dict[str, Dict[str, float]],
    week_numbers: List[str],
    standings: List[Any],
) -> List[Dict[str, Any]]:
    """Build the cumulative Mamba Points audit table."""

    standing_by_team = {
        standing.team_name: standing
        for standing in standings
    }

    weekly_highs = {
        week_number: max(
            float(points)
            for points in weekly_mamba_points[week_number].values()
        )
        for week_number in week_numbers
    }

    weekly_lows = {
        week_number: min(
            float(points)
            for points in weekly_mamba_points[week_number].values()
        )
        for week_number in week_numbers
    }

    ordered_team_names = sorted(
        standing_by_team.keys(),
        key=lambda team_name: (
            -float(standing_by_team[team_name].mamba_points),
            -float(standing_by_team[team_name].points_for),
            team_name.lower(),
        ),
    )

    rows: List[Dict[str, Any]] = []

    for rank, team_name in enumerate(
        ordered_team_names,
        start=1,
    ):
        weekly_values: List[Dict[str, Any]] = []

        for week_number in week_numbers:
            value = float(
                weekly_mamba_points[week_number][team_name]
            )

            weekly_values.append(
                {
                    "value": value,
                    "is_high": value == weekly_highs[week_number],
                    "is_low": value == weekly_lows[week_number],
                }
            )

        rows.append(
            {
                "rank": rank,
                "team_name": team_name,
                "total": float(
                    standing_by_team[team_name].mamba_points
                ),
                "weekly_values": weekly_values,
            }
        )

    return rows


def add_previous_week_rank_changes(
    *,
    weeks: Dict[str, Dict[str, float]],
    weekly_mamba_points: Dict[str, Dict[str, float]],
    yahoo_teams: Dict[str, Dict[str, Any]],
    week_numbers: List[str],
    standings: List[Any],
    yahoo_rows: List[Dict[str, Any]],
    points_for_rows: List[Dict[str, Any]],
    mamba_rows: List[Dict[str, Any]],
) -> None:
    """Annotate each table row with movement versus the prior cumulative week.

    Positive values mean the team moved up in that table (for example, +2 means
    #5 last week to #3 this week). Negative values mean the team moved down.
    Week 1 has no prior comparison, so movement is left as ``None``.
    """

    for standing in standings:
        setattr(standing, "rank_change", None)
    for row in yahoo_rows:
        row["rank_change"] = None
    for row in points_for_rows:
        row["rank_change"] = None
    for row in mamba_rows:
        row["rank_change"] = None

    if len(week_numbers) <= 1:
        return

    previous_week_numbers = week_numbers[:-1]
    previous_weeks = {
        week_number: weeks[week_number]
        for week_number in previous_week_numbers
    }
    previous_mamba_points = {
        week_number: weekly_mamba_points[week_number]
        for week_number in previous_week_numbers
    }

    previous_yahoo_rows, previous_yahoo_ranks = build_yahoo_snapshot(
        yahoo_teams=yahoo_teams,
        weeks=previous_weeks,
        week_numbers=previous_week_numbers,
    )
    previous_standings = build_hybrid_standings(
        weeks=previous_weeks,
        yahoo_ranks=previous_yahoo_ranks,
    )
    previous_points_rows = build_points_for_audit_rows(
        weeks=previous_weeks,
        week_numbers=previous_week_numbers,
        standings=previous_standings,
    )
    previous_mamba_rows = build_mamba_audit_rows(
        weekly_mamba_points=previous_mamba_points,
        week_numbers=previous_week_numbers,
        standings=previous_standings,
    )

    previous_hybrid_rank = {
        row.team_name: int(row.final_rank)
        for row in previous_standings
    }
    previous_yahoo_rank = {
        row["team_name"]: int(row["rank"])
        for row in previous_yahoo_rows
    }
    previous_points_rank = {
        row["team_name"]: int(row["rank"])
        for row in previous_points_rows
    }
    previous_mamba_rank = {
        row["team_name"]: int(row["rank"])
        for row in previous_mamba_rows
    }

    for standing in standings:
        old_rank = previous_hybrid_rank.get(standing.team_name)
        if old_rank is not None:
            standing.rank_change = old_rank - int(standing.final_rank)

    for row in yahoo_rows:
        old_rank = previous_yahoo_rank.get(row["team_name"])
        if old_rank is not None:
            row["rank_change"] = old_rank - int(row["rank"])

    for row in points_for_rows:
        old_rank = previous_points_rank.get(row["team_name"])
        if old_rank is not None:
            row["rank_change"] = old_rank - int(row["rank"])

    for row in mamba_rows:
        old_rank = previous_mamba_rank.get(row["team_name"])
        if old_rank is not None:
            row["rank_change"] = old_rank - int(row["rank"])


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    week: Optional[int] = Query(default=None, ge=1),
):
    season_data = load_historical_season()
    yahoo_wl_data = load_yahoo_wl_season()

    all_weeks = season_data["weeks"]
    all_weekly_mamba_points = season_data[
        "expected_weekly_mamba_points"
    ]
    yahoo_teams = yahoo_wl_data["teams"]

    all_week_numbers = sorted(
        all_weeks.keys(),
        key=int,
    )

    yahoo_week_numbers = [
        str(week_number)
        for week_number in range(
            1,
            int(yahoo_wl_data["through_week"]) + 1,
        )
    ]

    if all_week_numbers != yahoo_week_numbers:
        raise ValueError(
            "Historical score weeks and Yahoo W/L weeks do not "
            "match. Historical weeks: "
            f"{all_week_numbers}; Yahoo weeks: "
            f"{yahoo_week_numbers}."
        )

    historical_team_names = set(
        next(iter(all_weeks.values())).keys()
    )
    yahoo_team_names = set(yahoo_teams.keys())

    validate_team_sets(
        historical_teams=historical_team_names,
        yahoo_teams=yahoo_team_names,
    )

    validate_historical_mamba_points(
        weeks=all_weeks,
        expected_weekly_points=all_weekly_mamba_points,
    )

    maximum_week = max(
        int(week_number)
        for week_number in all_week_numbers
    )

    selected_week = week if week is not None else maximum_week
    selected_week = min(selected_week, maximum_week)

    week_numbers = [
        week_number
        for week_number in all_week_numbers
        if int(week_number) <= selected_week
    ]

    weeks = {
        week_number: all_weeks[week_number]
        for week_number in week_numbers
    }

    weekly_mamba_points = {
        week_number: all_weekly_mamba_points[week_number]
        for week_number in week_numbers
    }

    yahoo_rows, yahoo_ranks = build_yahoo_snapshot(
        yahoo_teams=yahoo_teams,
        weeks=weeks,
        week_numbers=week_numbers,
    )

    if selected_week == maximum_week:
        stored_yahoo_ranks = {
            team_name: int(team_data["official_yahoo_rank"])
            for team_name, team_data in yahoo_teams.items()
        }

        if yahoo_ranks != stored_yahoo_ranks:
            raise ValueError(
                "Calculated Yahoo ranks do not match the stored "
                "official ranks for the final historical week."
            )

    standings = build_hybrid_standings(
        weeks=weeks,
        yahoo_ranks=yahoo_ranks,
    )

    points_for_rows = build_points_for_audit_rows(
        weeks=weeks,
        week_numbers=week_numbers,
        standings=standings,
    )

    mamba_rows = build_mamba_audit_rows(
        weekly_mamba_points=weekly_mamba_points,
        week_numbers=week_numbers,
        standings=standings,
    )

    add_previous_week_rank_changes(
        weeks=weeks,
        weekly_mamba_points=weekly_mamba_points,
        yahoo_teams=yahoo_teams,
        week_numbers=week_numbers,
        standings=standings,
        yahoo_rows=yahoo_rows,
        points_for_rows=points_for_rows,
        mamba_rows=mamba_rows,
    )

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "page_title": "Mamba Fantasy",
            "season": season_data["season"],
            "available_seasons": [season_data["season"]],
            "available_weeks": [
                int(week_number)
                for week_number in all_week_numbers
            ],
            "current_week": selected_week,
            "maximum_week": maximum_week,
            "standings": standings,
            "week_numbers": week_numbers,
            "yahoo_rows": yahoo_rows,
            "points_for_rows": points_for_rows,
            "mamba_rows": mamba_rows,
        },
    )
