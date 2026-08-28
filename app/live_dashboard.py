from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.routes import (
    add_previous_week_rank_changes,
    build_mamba_audit_rows,
    build_points_for_audit_rows,
    build_yahoo_snapshot,
    templates,
)
from app.scoring import build_hybrid_standings
from app.yahoo_dashboard import HYBRID_START_SEASON
from app.yahoo_live_cache import load_cached_yahoo_dashboard_data
from app.yahoo_seasons import discover_mamba_seasons


live_dashboard_router = APIRouter()


def _latest_available_season(request: Request) -> int:
    try:
        seasons = discover_mamba_seasons(request)
        if seasons:
            return max(int(item["season"]) for item in seasons)
    except Exception as exc:
        print(f"WARNING: Yahoo season discovery failed; using current year: {exc}")
    return max(2025, datetime.now(timezone.utc).year)


def _add_yahoo_previous_week_rank_changes(
    *,
    yahoo_rows: List[Dict[str, Any]],
    yahoo_teams: Dict[str, Dict[str, Any]],
    weeks: Dict[str, Dict[str, float]],
    week_numbers: List[str],
) -> None:
    """Add Yahoo standings movement for seasons without Mamba scoring."""
    for row in yahoo_rows:
        row["rank_change"] = None

    if len(week_numbers) <= 1:
        return

    previous_week_numbers = week_numbers[:-1]
    previous_weeks = {
        week_number: weeks[week_number]
        for week_number in previous_week_numbers
    }
    previous_rows, _ = build_yahoo_snapshot(
        yahoo_teams=yahoo_teams,
        weeks=previous_weeks,
        week_numbers=previous_week_numbers,
    )
    previous_rank = {
        row["team_name"]: int(row["rank"])
        for row in previous_rows
    }

    for row in yahoo_rows:
        old_rank = previous_rank.get(row["team_name"])
        if old_rank is not None:
            row["rank_change"] = old_rank - int(row["rank"])


@live_dashboard_router.get("/", response_class=HTMLResponse)
def live_dashboard_home(
    request: Request,
    season: Optional[int] = Query(default=None, ge=2000, le=2100),
    week: Optional[int] = Query(default=None, ge=1, le=18),
):
    selected_season = season if season is not None else _latest_available_season(request)
    data = load_cached_yahoo_dashboard_data(
        request=request, season=selected_season, requested_week=week
    )

    refresh_meta = data.get("_refresh_meta", {})
    current_calendar_season = datetime.now(timezone.utc).year
    hybrid_scoring_enabled = selected_season >= HYBRID_START_SEASON

    common_context = {
        "request": request,
        "page_title": "Mamba Fantasy",
        "season": selected_season,
        "available_weeks": data["available_weeks"],
        "current_week": data["current_week"],
        "maximum_week": data["maximum_week"],
        "yahoo_source": True,
        "league_name": data.get("league_name") or "The Mamba League",
        "league_key": data.get("league_key"),
        "yahoo_refresh_epoch": float(refresh_meta.get("refreshed_at") or 0),
        "yahoo_data_version": str(refresh_meta.get("signature") or ""),
        "yahoo_refresh_interval_seconds": int(
            refresh_meta.get("refresh_interval_seconds") or 300
        ),
        "yahoo_refresh_stale": bool(refresh_meta.get("stale")),
        "yahoo_refresh_error": refresh_meta.get("error"),
        "live_refresh_enabled": (
            selected_season == current_calendar_season and week is None
        ),
        "live_refresh_requested_week": week,
        "hybrid_scoring_enabled": hybrid_scoring_enabled,
    }

    if data["mode"] == "matchups":
        return templates.TemplateResponse(
            "matchups.html",
            {**common_context, "matchups": data.get("matchups", [])},
        )

    week_numbers = data["week_numbers"]
    weeks = data["weeks"]
    yahoo_teams = data["yahoo_teams"]

    yahoo_rows, yahoo_ranks = build_yahoo_snapshot(
        yahoo_teams=yahoo_teams,
        weeks=weeks,
        week_numbers=week_numbers,
    )

    if data["mode"] == "yahoo_only" or not hybrid_scoring_enabled:
        _add_yahoo_previous_week_rank_changes(
            yahoo_rows=yahoo_rows,
            yahoo_teams=yahoo_teams,
            weeks=weeks,
            week_numbers=week_numbers,
        )

        return templates.TemplateResponse(
            "yahoo_only.html",
            {
                **common_context,
                "week_numbers": week_numbers,
                "yahoo_rows": yahoo_rows,
                "matchups": data.get("matchups", []),
                "show_bottom_matchups": True,
            },
        )

    weekly_mamba_points = data["weekly_mamba_points"]
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
            **common_context,
            "standings": standings,
            "week_numbers": week_numbers,
            "yahoo_rows": yahoo_rows,
            "points_for_rows": points_for_rows,
            "mamba_rows": mamba_rows,
            "matchups": data.get("matchups", []),
            "show_bottom_matchups": True,
        },
    )