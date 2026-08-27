from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.routes import (
    build_mamba_audit_rows,
    build_points_for_audit_rows,
    build_yahoo_snapshot,
    home as historical_2025_home,
    templates,
)
from app.scoring import build_hybrid_standings
from app.yahoo_dashboard import MAMBA_SCORING_END_WEEK, load_yahoo_dashboard_data


live_dashboard_router = APIRouter()


@live_dashboard_router.get("/", response_class=HTMLResponse)
def live_dashboard_home(
    request: Request,
    season: int = Query(default=2025, ge=2000, le=2100),
    week: Optional[int] = Query(default=None, ge=1, le=18),
):
    """Render every Mamba League season in the established dashboard layout.

    The validated 2025 JSON test remains the source for Weeks 1-13 of 2025 so
    it continues to serve as our regression baseline. Every other season is
    read live from Yahoo. Week 14+ intentionally shows Yahoo matchups only.
    """

    if season == 2025 and (week is None or week <= MAMBA_SCORING_END_WEEK):
        return historical_2025_home(request=request, week=week)

    data = load_yahoo_dashboard_data(
        request=request,
        season=season,
        requested_week=week,
    )

    common_context = {
        "request": request,
        "page_title": "Mamba Fantasy",
        "season": season,
        "available_weeks": data["available_weeks"],
        "current_week": data["current_week"],
        "maximum_week": data["maximum_week"],
        "yahoo_source": True,
        "league_name": data.get("league_name") or "The Mamba League",
        "league_key": data.get("league_key"),
    }

    if data["mode"] == "matchups":
        return templates.TemplateResponse(
            "home.html",
            {
                **common_context,
                "show_scoring_dashboard": False,
                "standings": [],
                "week_numbers": [],
                "yahoo_rows": [],
                "points_for_rows": [],
                "mamba_rows": [],
                "matchups": data.get("matchups", []),
            },
        )

    week_numbers = data["week_numbers"]
    weeks = data["weeks"]
    weekly_mamba_points = data["weekly_mamba_points"]
    yahoo_teams = data["yahoo_teams"]

    yahoo_rows, yahoo_ranks = build_yahoo_snapshot(
        yahoo_teams=yahoo_teams,
        weeks=weeks,
        week_numbers=week_numbers,
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

    return templates.TemplateResponse(
        "home.html",
        {
            **common_context,
            "show_scoring_dashboard": True,
            "standings": standings,
            "week_numbers": week_numbers,
            "yahoo_rows": yahoo_rows,
            "points_for_rows": points_for_rows,
            "mamba_rows": mamba_rows,
            "matchups": [],
        },
    )
