import os.path
import uuid

from fastapi import Depends, HTTPException, APIRouter, File, UploadFile
from sqlalchemy.orm import Session

from app.schemas import ConversationListItem, MemberResponse, CreateUserResponse, CreateUserRequest, \
    CreateConversationResponse, CreateConversationRequest, FileUploadResponse
from app.models import get_db, ConversationMember, Conversation, User


router = APIRouter(prefix="/api/v1")

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

UPLOAD_DIR = "artifacts"
MAX_FILE_SIZE = 10 * 1024 * 1024 #max file size 10Mb
ALLOWED_TYPES = {
    "image/jpeg", "image/png",
    "audio/mpeg", "audio/wav", "audio/mp4",
    "video/mp4",
    "application/pdf",
}

@router.post("/uploads", response_model=FileUploadResponse, status_code=201)
async def upload_file(file: UploadFile = File(...)):

    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail=f"File type not supported: {file.content_type}")

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"File size was larger than 10Mb")

    ext = os.path.splitext(file.filename)[1] if file.filename else ""
    unique_name = f"{uuid.uuid4()}/{ext}"

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(UPLOAD_DIR, unique_name)
    with open(file_path, "wb") as f:
        f.write(content)

    return FileUploadResponse(
        file_url=f"/files/{unique_name}",
        file_name=file.filename or unique_name,
        mime_type=file.content_type or "application/octet-stream",
        file_size_bytes=str(len(content))
    )