import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.routes import router
from app.week_selector import HistoricalWeekSelectorMiddleware
from app.yahoo_auth import yahoo_router
from app.yahoo_mamba import mamba_yahoo_router


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
app.include_router(router)
app.include_router(yahoo_router)
app.include_router(mamba_yahoo_router)
