import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.models import Base, engine
from app.routes import router
from app.websockets import websocket_endpoint

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized")

    yield

    logger.info("Shutting down application")

app = FastAPI(title="ChatApp", lifespan=lifespan)


#Routes

@app.get("/")
async def health():
    return {"status": "ok"}

app.include_router(router=router)
app.add_api_websocket_route("/ws", websocket_endpoint)