import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.models import Base, engine, get_db, User

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    logger.info("Database initialized")

    yield

    logger.info("Shutting down application")

app = FastAPI(title="ChatApp", lifespan=lifespan)
router = APIRouter(prefix="/api/v1")

#Request Response Schemas
class CreateUserRequest(BaseModel):
    username: str
    email: str

class CreateUserResponse(BaseModel):
    id: str
    username: str
    email: str
    created_at: str



#Routes

@app.get("/")
async def health():
    return {"status": "ok"}

@router.post("/register", response_model=CreateUserResponse, status_code=201)
def create_user(body: CreateUserRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(
        (User.email == body.email)
    ).first()

    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(username=body.username, email=body.email)
    db.add(user)
    db.commit()
    db.refresh(user)

    return CreateUserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        created_at=user.created_at.isoformat()
    )

app.include_router(router=router)