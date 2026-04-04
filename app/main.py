import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.models import Base, engine

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized")

    yield

    logger.info("Shutting down application")

app = FastAPI(title="ChatApp", lifespan=lifespan)

@app.get("/")
async def health():
    return {"status": "ok"}
