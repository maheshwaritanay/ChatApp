# Websocket connection manager
import logging

from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect, WebSocket

from app.models import User, SessionLocal, Conversation, Message, ConversationMember

logger = logging.getLogger(__name__)

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