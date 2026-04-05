from pydantic import BaseModel


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