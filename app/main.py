import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.live_dashboard import live_dashboard_router
from app.routes import router
from app.week_selector import HistoricalWeekSelectorMiddleware
from app.yahoo_auth import yahoo_router
from app.yahoo_history import history_router
from app.yahoo_legacy_candidates import legacy_candidate_router
from app.yahoo_live_status import live_status_router
from app.yahoo_mamba import mamba_yahoo_router
from app.yahoo_seasons import season_router
from app.yahoo_shared_auth import (
    install_shared_yahoo_auth,
    storage_status_router,
)


# Deployment trigger test: this comment has no effect on application behavior.
# Mamba uses one approved read-only Yahoo account for one private league. A
# successful Yahoo authorization is shared server-side so desktop, mobile, and
# other league viewers do not each need their own Yahoo login. Upstash provides
# persistence across Render spin-downs, restarts, and deploys when configured.
install_shared_yahoo_auth()


app = FastAPI(
    title="Mamba Fantasy",
    description="Hybrid Fantasy Football Rankings",
)

app.add_middleware(HistoricalWeekSelectorMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv(
        "SESSION_SECRET",
        "local-development-only-change-on-render",
    ),
    same_site="lax",
    https_only=os.getenv("RENDER", "").lower() == "true",
)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Register the multi-season dashboard before the original 2025 route so "/"
# is handled by the Yahoo-aware season router while the original function can
# still be called internally as the validated 2025 regression baseline.
app.include_router(live_dashboard_router)
app.include_router(router)
app.include_router(yahoo_router)
app.include_router(storage_status_router)
app.include_router(live_status_router)
app.include_router(mamba_yahoo_router)
app.include_router(history_router)
app.include_router(season_router)
app.include_router(legacy_candidate_router)
