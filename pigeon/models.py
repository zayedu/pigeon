from typing import Annotated, Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, TypeAdapter, field_validator

MAX_FRAME = 64 * 1024
MAX_TEXT = 4_000
MAX_WAIT = 20
Username = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]

Address = Annotated[str, Field(pattern=r"^@[a-z0-9][a-z0-9_-]{0,31}/[a-z0-9][a-z0-9_-]{0,31}$")]
address = TypeAdapter(Address)
Content = Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]
Status = Literal["queued", "running", "completed", "failed", "cancelled", "timed_out"]
ACTIVE = ("queued", "running")


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Context(Frame):
    type: Literal["github_pr"]
    repo: Annotated[
        str,
        Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$"),
    ]
    number: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if value.partition("/")[2] in {".", ".."}:
            raise ValueError("Use a GitHub owner and repository name.")
        return value


class Submit(Frame):
    op: Literal["submit"] = "submit"
    message_id: UUID4
    conversation_id: UUID4
    to: Address
    message: Content
    context: Context | None = None
    timeout: Annotated[int, Field(strict=True, ge=1, le=900)] = 180


class Inspect(Frame):
    op: Literal["status"] = "status"
    conversation_id: UUID4
    message_id: UUID4 | None = None
    after: Status | None = None
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = 0


class Take(Frame):
    op: Literal["take"] = "take"
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = MAX_WAIT


class Watch(Frame):
    op: Literal["watch"] = "watch"
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = MAX_WAIT


class Finish(Frame):
    op: Literal["finish"] = "finish"
    message_id: UUID4
    status: Literal["completed", "failed", "timed_out"]
    content: Content


class Cancel(Frame):
    op: Literal["cancel"] = "cancel"
    conversation_id: UUID4
    message_id: UUID4


class Close(Frame):
    op: Literal["close"] = "close"
    conversation_id: UUID4


class ListCalls(Frame):
    op: Literal["list"] = "list"


class Ping(Frame):
    op: Literal["ping"] = "ping"


class Grant(Frame):
    op: Literal["allow", "deny"]
    sender: Address


class Signup(Frame):
    username: Username


class Share(Frame):
    op: Literal["share"] = "share"
    session_id: UUID4 | None = None
    session_token: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")] | None = None
    resume: bool = False


ShareKey = Annotated[str, Field(pattern=r"^pg_[A-Za-z0-9_-]{22}$")]


class Redeem(Frame):
    op: Literal["redeem"] = "redeem"
    key: ShareKey
    peer_session: bool = False


Command = (
    Submit
    | Inspect
    | Take
    | Finish
    | Cancel
    | Close
    | ListCalls
    | Ping
    | Grant
    | Share
    | Redeem
    | Watch
)
command = TypeAdapter(Annotated[Command, Field(discriminator="op")])


class Message(Frame):
    id: UUID4
    conversation_id: UUID4
    sender: Address
    recipient: Address
    content: Content
    created_at: float
    type: Literal["request", "response", "error"]


class Turn(Frame):
    request: Message
    response: Message | None
    status: Status
    deadline: float
    conversation_created_at: float
    context: Context | None
    detail: str | None
    follow_up: bool


class PigeonError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
