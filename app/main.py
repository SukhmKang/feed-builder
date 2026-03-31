"""FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.database import create_tables
from app.routers import articles, feeds, subscriptions
from app.scheduler import start_scheduler, stop_scheduler
from app.services.push import get_public_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    get_public_key()  # generate VAPID keys on startup if not present
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Feed Builder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(feeds.router)
app.include_router(articles.router)
app.include_router(subscriptions.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
