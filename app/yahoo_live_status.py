from typing import Optional

from fastapi import APIRouter, Query, Request

from app.yahoo_live_cache import load_cached_yahoo_dashboard_data


live_status_router = APIRouter(
    prefix="/api/yahoo",
    tags=["yahoo-live-status"],
)


@live_status_router.get("/refresh-status")
def yahoo_refresh_status(
    request: Request,
    season: int = Query(..., ge=2000, le=2100),
    week: Optional[int] = Query(default=None, ge=1, le=18),
):
    """Refresh shared Yahoo data when stale and return lightweight status."""

    data = load_cached_yahoo_dashboard_data(
        request=request,
        season=season,
        requested_week=week,
    )
    meta = data.get("_refresh_meta", {})

    return {
        "season": int(data.get("season") or season),
        "current_week": int(data.get("current_week") or week or 1),
        "refreshed_at": float(meta.get("refreshed_at") or 0),
        "data_version": str(meta.get("signature") or ""),
        "refresh_interval_seconds": int(meta.get("refresh_interval_seconds") or 300),
        "stale": bool(meta.get("stale")),
        "refreshing": bool(meta.get("refreshing")),
        "error": meta.get("error"),
    }
