from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.routes import router
from app.week_selector import HistoricalWeekSelectorMiddleware


app = FastAPI(
    title="Mamba Fantasy",
    description="Hybrid Fantasy Football Rankings",
)

app.add_middleware(HistoricalWeekSelectorMiddleware)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(router)
