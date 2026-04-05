import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, APIRouter, Depends, HTTPException, WebSocket
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from app.models import Base, engine, get_db, User, Conversation, ConversationMember, SessionLocal, Message

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

class CreateConversationRequest(BaseModel):
    type: str
    name: str | None = None
    created_by: str
    member_ids: list[str]
    member_roles: dict[str, str] | None = None

class MemberResponse(BaseModel):
    user_id: str
    username: str
    role: str

class CreateConversationResponse(BaseModel):
    id: str
    type: str
    name: str | None
    created_by: str
    created_at: str
    members: list[MemberResponse]

class ConversationListItem(BaseModel):
    id: str
    type: str
    name: str | None
    created_at: str
    last_message_at: str | None
    members: list[MemberResponse]


# Websocket connection manager

WS_CLOSE_DUPLICATE_SESSION = 4000
WS_CLOSE_USER_NOT_FOUND = 4001

class ConnectionManager:

    def __init__(self):
        self._active_connections: dict[str, WebSocket] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        existing = self._active_connections.get(user_id)

        if existing:
            try:
                await existing.close(code=WS_CLOSE_DUPLICATE_SESSION, reason="Connected from another session")
            except Exception:
                pass

        await websocket.accept()
        self._active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected. Total: {len(self._active_connections)}")

    async def disconnect(self, user_id: str):
        ws = self._active_connections.pop(user_id, None)

        if ws:
            try:
                await ws.close()
            except Exception:
                pass  # connection might already be closed

        logger.info(f"User {user_id} disconnected. Total: {len(self._active_connections)}")

    async def send_to_user(self, user_id: str, message: dict):
        ws = self._active_connections[user_id]

        if ws:
            await ws.send_json(message)

    async def broadcast_to_conversation(self, conversation_id: str, message: dict, exclude_user: str | None = None, db: Session= None):
        if not db:
            return

        members = (
            db.query(ConversationMember)
            .filter(ConversationMember.conversation_id == conversation_id)
            .all()
        )

        for member in members:
            if member.user_id != exclude_user:
                await self.send_to_user(user_id=member.user_id, message=message)

manager = ConnectionManager()

async def handle_send_message(sender_id: str, data: dict):
    conversation_id = data.get("conversation_id")
    content_type = data.get("content_type", "text")
    body = data.get("body")
    artifact = data.get("artifact")

    db = SessionLocal()

    try:
        membership = (
            db.query(ConversationMember)
            .filter(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.user_id == sender_id,
            )
            .first()
        )

        if not membership:
            await manager.send_to_user(sender_id, {
                "event": "error",
                "code": "NOT_MEMBER",
                "message": "You are not a member of this conversation",
            })
            return

        if membership.role == "read":
            await manager.send_to_user(sender_id, {
                "event": "error",
                "code": "PERMISSION_DENIED",
                "message": "Read-only members cannot send messages",
            })
            return

        message = Message(
            conversation_id=conversation_id,
            sender_id=sender_id,
            content_type=content_type,
            body=body,
            file_url=artifact.get("file_url") if artifact else None,
            file_name=artifact.get("file_name") if artifact else None,
            mime_type=artifact.get("mime_type") if artifact else None,
            file_size_bytes=artifact.get("file_size_bytes") if artifact else None,
        )
        db.add(message)

        conversation = db.query(Conversation).filter(Conversation.id == conversation_id).first()
        conversation.last_message_at = message.created_at

        db.commit()
        db.refresh(message)

        sender = db.query(User).filter(User.id == sender_id).first()

        broadcast = {
            "event": "new_message",
            "conversation_id": conversation_id,
            "message": {
                "message_id": message.id,
                "sender_id": sender_id,
                "sender_name": sender.username,
                "content_type": message.content_type,
                "body": message.body,
                "file_url": message.file_url,
                "file_name": message.file_name,
                "created_at": message.created_at.isoformat(),
            },
        }

        await manager.broadcast_to_conversation(conversation_id, broadcast, db=db)

    finally:
        db.close()

async def handle_typing(sender_id: str, data: dict):
    conversation_id = data.get("conversation_id")

    db = SessionLocal()
    try:
        sender = db.query(User).filter(User.id == sender_id).first()

        broadcast = {
            "event": "user_typing",
            "conversation_id": conversation_id,
            "user_id": sender_id,
            "user_name": sender.username,
        }

        await manager.broadcast_to_conversation(
            conversation_id, broadcast, exclude_user=sender_id, db=db
        )
    finally:
        db.close()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            await websocket.close(code=4001, reason="User not found")
            return
    finally:
        db.close()

    await manager.connect(user_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            event = data.get("event")

            if event == "send_message":
                await handle_send_message(user_id, data)
            elif event == "typing_start":
                await handle_typing(user_id, data)
            else:
                await manager.send_to_user(user_id, {
                    "event": "error",
                    "code": "UNKNOWN_EVENT",
                    "message": f"Unknown event: {event}",
                })

    except WebSocketDisconnect:
        await manager.disconnect(user_id)



#Routes

@app.get("/")
async def health():
    return {"status": "ok"}

@router.get("/conversations", response_model=list[ConversationListItem])
def get_conversations(user_id: str, db: Session = Depends(get_db)):
    memberships = (
        db.query(ConversationMember)
        .filter(ConversationMember.user_id == user_id)
        .all()
    )

    if not memberships:
        return []

    conversation_ids = [m.conversation_id for m in memberships]
    conversations = (
        db.query(Conversation)
        .filter(Conversation.id.in_(conversation_ids))
        .order_by(Conversation.last_message_at.desc().nullslast())
        .all()
    )

    result = []
    for conv in conversations:
        members = [
            MemberResponse(
                user_id=m.user_id,
                username=m.user.username,
                role=m.role,
            )
            for m in conv.members
        ]
        result.append(ConversationListItem(
            id=str(conv.id),
            type=str(conv.type),
            name=str(conv.name),
            created_at=conv.created_at.isoformat(),
            last_message_at=conv.last_message_at.isoformat() if conv.last_message_at else None,
            members=members,
        ))

    return result

@router.get("/conversations/{conversation_id}/messages")
def get_messages(
    conversation_id: str,
    user_id: str,
    limit: int = 50,
    before: str | None = None,
    db: Session = Depends(get_db),
):
    # Check user is a member of this conversation
    membership = (
        db.query(ConversationMember)
        .filter(
            ConversationMember.conversation_id == conversation_id,
            ConversationMember.user_id == user_id,
        )
        .first()
    )
    if not membership:
        raise HTTPException(status_code=403, detail="Not a member of this conversation")

    from models import Message

    query = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
    )

    # paginated response before a cursor message
    if before:
        cursor_msg = db.query(Message).filter(Message.id == before).first()
        if cursor_msg:
            query = query.filter(Message.created_at < cursor_msg.created_at)

    # getting next cursor message
    messages = query.limit(limit + 1).all()

    has_more = len(messages) > limit
    messages = messages[:limit]

    return {
        "messages": [
            {
                "id": m.id,
                "sender_id": m.sender_id,
                "sender_name": m.sender.username,
                "content_type": m.content_type,
                "body": m.body,
                "file_url": m.file_url,
                "file_name": m.file_name,
                "mime_type": m.mime_type,
                "is_deleted": m.is_deleted,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "has_more": has_more,
        "next_cursor": messages[-1].id if has_more else None,
    }

@router.get("/users", response_model=CreateUserResponse)
def get_user(email: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return CreateUserResponse(
        id=str(user.id),
        username=str(user.username),
        email=str(user.email),
        created_at=user.created_at.isoformat(),
    )

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

@router.post("/conversations", response_model=CreateConversationResponse, status_code=201)
def create_conversation(body: CreateConversationRequest, db: Session = Depends(get_db)):
    # Validate creator exists
    creator = db.query(User).filter(User.id == body.created_by).first()
    if not creator:
        raise HTTPException(status_code=404, detail="Creator not found")

    # Validate conversation type
    if body.type not in ("direct", "group"):
        raise HTTPException(status_code=400, detail="Type must be 'direct' or 'group'")

    # Direct chats must have exactly 1 other member
    if body.type == "direct" and len(body.member_ids) != 1:
        raise HTTPException(status_code=400, detail="Direct chat must have exactly 1 other member")

    # Group chats need a name
    if body.type == "group" and not body.name:
        raise HTTPException(status_code=400, detail="Group conversations require a name")

    # Validate all members exist
    all_member_ids = body.member_ids + [body.created_by]
    users = db.query(User).filter(User.id.in_(all_member_ids)).all()
    found_ids = {str(u.id) for u in users}
    missing = set(all_member_ids) - found_ids
    if missing:
        raise HTTPException(status_code=404, detail=f"Users not found: {missing}")

    # Check if direct conversation already exists between these two users
    if body.type == "direct":
        other_id = body.member_ids[0]

        # Find direct conversations that the creator is part of
        creator_convos = (
            db.query(ConversationMember.conversation_id)
            .join(Conversation)
            .filter(
                Conversation.type == "direct",
                ConversationMember.user_id == body.created_by,
            )
        )

        # Check if the other user is also in any of those
        existing = (
            db.query(ConversationMember)
            .filter(
                ConversationMember.conversation_id.in_(creator_convos),
                ConversationMember.user_id == other_id,
            )
            .first()
        )

        if existing:
            raise HTTPException(status_code=409, detail="Direct conversation already exists")

    # Create the conversation
    conversation = Conversation(
        type=body.type,
        name=body.name,
        created_by=body.created_by,
    )
    db.add(conversation)
    db.flush()  # generates conversation.id without committing

    # Add creator as admin
    creator_member = ConversationMember(
        conversation_id=conversation.id,
        user_id=body.created_by,
        role="admin",
    )
    db.add(creator_member)

    # Add other members
    member_roles = body.member_roles or {}
    for member_id in body.member_ids:
        role = member_roles.get(member_id, "write")
        member = ConversationMember(
            conversation_id=conversation.id,
            user_id=member_id,
            role=role,
        )
        db.add(member)

    db.commit()
    db.refresh(conversation)

    # Build response with member details
    user_map = {u.id: u for u in users}
    members_response = []
    for m in conversation.members:
        members_response.append(MemberResponse(
            user_id=m.user_id,
            username=user_map[m.user_id].username,
            role=m.role,
        ))

    return CreateConversationResponse(
        id=conversation.id,
        type=conversation.type,
        name=conversation.name,
        created_by=conversation.created_by,
        created_at=conversation.created_at.isoformat(),
        members=members_response,
    )


app.include_router(router=router)