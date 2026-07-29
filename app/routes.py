from typing import Any, Dict, List

from fastapi import APIRouter, Request
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
    """
    Display whole-number ranks without a decimal.

    Examples:
        1.0 -> "1"
        2.5 -> "2.5"
    """

    numeric_value = float(value)

    if numeric_value.is_integer():
        return str(int(numeric_value))

    return f"{numeric_value:.1f}"


def format_number(value: float) -> str:
    """
    Display whole numbers without a decimal and preserve one
    decimal when a tie produces a half-point.
    """

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
    """
    Confirm that both historical files contain the same teams.
    """

    missing_from_yahoo = (
        historical_teams - yahoo_teams
    )

    missing_from_historical = (
        yahoo_teams - historical_teams
    )

    if missing_from_yahoo:
        missing_text = ", ".join(
            sorted(missing_from_yahoo)
        )

        raise ValueError(
            "Teams missing from yahoo_wl_2025.json: "
            f"{missing_text}"
        )

    if missing_from_historical:
        missing_text = ", ".join(
            sorted(missing_from_historical)
        )

        raise ValueError(
            "Teams missing from historical_2025.json: "
            f"{missing_text}"
        )


def build_yahoo_audit_rows(
    yahoo_teams: Dict[str, Dict[str, Any]],
    week_numbers: List[str],
) -> List[Dict[str, Any]]:
    """
    Build the Yahoo W/L audit table.

    Teams are ranked by:
        1. Total wins, descending
        2. Total Points For, descending
        3. Team name, ascending

    The calculated ordering is validated against the official
    Yahoo rank stored in yahoo_wl_2025.json.
    """

    ordered_team_names = sorted(
        yahoo_teams.keys(),
        key=lambda team_name: (
            -int(
                yahoo_teams[
                    team_name
                ]["total_wins"]
            ),
            -float(
                yahoo_teams[
                    team_name
                ]["total_points_for"]
            ),
            team_name.lower(),
        ),
    )

    rows: List[Dict[str, Any]] = []

    for calculated_rank, team_name in enumerate(
        ordered_team_names,
        start=1,
    ):
        team_data = yahoo_teams[team_name]

        official_rank = int(
            team_data["official_yahoo_rank"]
        )

        if calculated_rank != official_rank:
            raise ValueError(
                "Yahoo ranking validation failed for "
                f"'{team_name}'. Calculated rank: "
                f"{calculated_rank}; stored official "
                f"rank: {official_rank}."
            )

        weekly_results = []

        for week_number in week_numbers:
            result = team_data[
                "weekly_results"
            ].get(week_number)

            if result is None:
                raise ValueError(
                    "Yahoo W/L data is missing "
                    f"Week {week_number} for "
                    f"'{team_name}'."
                )

            weekly_results.append(
                {
                    "week": int(week_number),
                    "result": result,
                    "is_win": result == "W",
                    "is_loss": result == "L",
                    "is_tie": result == "T",
                }
            )

        rows.append(
            {
                "rank": calculated_rank,
                "team_name": team_name,
                "total_wins": int(
                    team_data["total_wins"]
                ),
                "total_points_for": float(
                    team_data["total_points_for"]
                ),
                "weekly_results": weekly_results,
            }
        )

    return rows


def build_points_for_audit_rows(
    weeks: Dict[str, Dict[str, float]],
    week_numbers: List[str],
    standings: List[Any],
) -> List[Dict[str, Any]]:
    """
    Build the Points For audit table.

    Teams are ordered from highest total Points For to lowest.

    Each weekly value is flagged when it is the highest or lowest
    Points For score for that week. If multiple teams are tied for
    the weekly high or low, every tied value is flagged.
    """

    total_points_for = {
        standing.team_name: float(
            standing.points_for
        )
        for standing in standings
    }

    weekly_highs = {
        week_number: max(
            float(score)
            for score in weeks[
                week_number
            ].values()
        )
        for week_number in week_numbers
    }

    weekly_lows = {
        week_number: min(
            float(score)
            for score in weeks[
                week_number
            ].values()
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
        weekly_values: List[
            Dict[str, Any]
        ] = []

        for week_number in week_numbers:
            value = float(
                weeks[
                    week_number
                ][team_name]
            )

            weekly_values.append(
                {
                    "value": value,
                    "is_high": (
                        value
                        == weekly_highs[
                            week_number
                        ]
                    ),
                    "is_low": (
                        value
                        == weekly_lows[
                            week_number
                        ]
                    ),
                }
            )

        rows.append(
            {
                "rank": rank,
                "team_name": team_name,
                "total": total_points_for[
                    team_name
                ],
                "weekly_values": weekly_values,
            }
        )

    return rows


def build_mamba_audit_rows(
    weekly_mamba_points: Dict[
        str,
        Dict[str, float],
    ],
    week_numbers: List[str],
    standings: List[Any],
) -> List[Dict[str, Any]]:
    """
    Build the Mamba Points audit table.

    Teams are ordered by total Mamba Points. Points For is used as
    the tiebreaker, matching the scoring engine.

    Each weekly value is flagged when it is the highest or lowest
    Mamba Points award for that week.
    """

    standing_by_team = {
        standing.team_name: standing
        for standing in standings
    }

    weekly_highs = {
        week_number: max(
            float(points)
            for points in weekly_mamba_points[
                week_number
            ].values()
        )
        for week_number in week_numbers
    }

    weekly_lows = {
        week_number: min(
            float(points)
            for points in weekly_mamba_points[
                week_number
            ].values()
        )
        for week_number in week_numbers
    }

    ordered_team_names = sorted(
        standing_by_team.keys(),
        key=lambda team_name: (
            -float(
                standing_by_team[
                    team_name
                ].mamba_points
            ),
            -float(
                standing_by_team[
                    team_name
                ].points_for
            ),
            team_name.lower(),
        ),
    )

    rows: List[Dict[str, Any]] = []

    for rank, team_name in enumerate(
        ordered_team_names,
        start=1,
    ):
        weekly_values: List[
            Dict[str, Any]
        ] = []

        for week_number in week_numbers:
            value = float(
                weekly_mamba_points[
                    week_number
                ][team_name]
            )

            weekly_values.append(
                {
                    "value": value,
                    "is_high": (
                        value
                        == weekly_highs[
                            week_number
                        ]
                    ),
                    "is_low": (
                        value
                        == weekly_lows[
                            week_number
                        ]
                    ),
                }
            )

        rows.append(
            {
                "rank": rank,
                "team_name": team_name,
                "total": float(
                    standing_by_team[
                        team_name
                    ].mamba_points
                ),
                "weekly_values": weekly_values,
            }
        )

    return rows


@router.get(
    "/",
    response_class=HTMLResponse,
)
def home(request: Request):
    season_data = load_historical_season()
    yahoo_wl_data = load_yahoo_wl_season()

    weeks = season_data["weeks"]

    weekly_mamba_points = season_data[
        "expected_weekly_mamba_points"
    ]

    yahoo_teams = yahoo_wl_data["teams"]

    week_numbers = sorted(
        weeks.keys(),
        key=int,
    )

    yahoo_week_numbers = [
        str(week_number)
        for week_number in range(
            1,
            int(
                yahoo_wl_data[
                    "through_week"
                ]
            ) + 1,
        )
    ]

    if week_numbers != yahoo_week_numbers:
        raise ValueError(
            "Historical score weeks and Yahoo W/L "
            "weeks do not match. Historical weeks: "
            f"{week_numbers}; Yahoo weeks: "
            f"{yahoo_week_numbers}."
        )

    historical_team_names = set(
        next(iter(weeks.values())).keys()
    )

    yahoo_team_names = set(
        yahoo_teams.keys()
    )

    validate_team_sets(
        historical_teams=historical_team_names,
        yahoo_teams=yahoo_team_names,
    )

    yahoo_ranks = {
        team_name: int(
            team_data[
                "official_yahoo_rank"
            ]
        )
        for team_name, team_data in (
            yahoo_teams.items()
        )
    }

    historical_yahoo_ranks = season_data[
        "official_yahoo_ranks_after_week_13"
    ]

    if yahoo_ranks != historical_yahoo_ranks:
        raise ValueError(
            "Official Yahoo ranks do not match "
            "between historical_2025.json and "
            "yahoo_wl_2025.json."
        )

    validate_historical_mamba_points(
        weeks=weeks,
        expected_weekly_points=(
            weekly_mamba_points
        ),
    )

    standings = build_hybrid_standings(
        weeks=weeks,
        yahoo_ranks=yahoo_ranks,
    )

    current_week = max(
        int(week_number)
        for week_number in week_numbers
    )

    yahoo_rows = build_yahoo_audit_rows(
        yahoo_teams=yahoo_teams,
        week_numbers=week_numbers,
    )

    points_for_rows = (
        build_points_for_audit_rows(
            weeks=weeks,
            week_numbers=week_numbers,
            standings=standings,
        )
    )

    mamba_rows = build_mamba_audit_rows(
        weekly_mamba_points=(
            weekly_mamba_points
        ),
        week_numbers=week_numbers,
        standings=standings,
    )

    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "page_title": "Mamba Fantasy",
            "season": season_data["season"],
            "current_week": current_week,
            "standings": standings,
            "week_numbers": week_numbers,
            "yahoo_rows": yahoo_rows,
            "points_for_rows": points_for_rows,
            "mamba_rows": mamba_rows,
        },
    )