import json
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import (
    UUID4,
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
)

MAX_FRAME = 64 * 1024
# Older clients validate messages against 4,000 characters, so the relay only
# delivers longer text to chats that advertise "long_messages".
LEGACY_TEXT = 4_000
MAX_TEXT = 16_000
# A turn carries a request and a response. Two encoded messages plus framing
# must fit in one 64 KiB WebSocket frame.
MAX_TEXT_BYTES = 30_000
MAX_WAIT = 20
QUEUE_TIMEOUT = 300
LEASE = 600
MAX_RUN = 3_600
MAX_ATTACHMENT = 1024 * 1024
RELAY_FEATURES = (
    "attachments",
    "peer_capabilities",
    "activity_watch",
    "leases",
    "long_messages",
    "history",
    "claim_ids",
)
Username = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")]

Address = Annotated[str, Field(pattern=r"^@[a-z0-9][a-z0-9_-]{0,31}/[a-z0-9][a-z0-9_-]{0,31}$")]
address = TypeAdapter(Address)


def encoded_size(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode())


def message_problem(text: str, limit: int = MAX_TEXT) -> str | None:
    size = encoded_size(text)
    if text and len(text) <= limit and size <= MAX_TEXT_BYTES:
        return None
    return (
        f"The message has {len(text):,} characters and encodes to {size:,} bytes. "
        f"The limit is {limit:,} characters and {MAX_TEXT_BYTES:,} encoded bytes. "
        "Split it into several turns or share a link."
    )


def check_content(text: str) -> str:
    if problem := message_problem(text):
        raise ValueError(problem)
    return text


Content = Annotated[str, Field(min_length=1, max_length=MAX_TEXT), AfterValidator(check_content)]
Status = Literal["queued", "running", "completed", "failed", "cancelled", "timed_out"]
ACTIVE = ("queued", "running")
Repository = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}$")]


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Context(Frame):
    type: Literal["github_pr"]
    repo: Repository
    number: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]

    @field_validator("repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        if value.partition("/")[2] in {".", ".."}:
            raise ValueError("Use a GitHub owner and repository name.")
        return value


class GitHubReference(Frame):
    repo: Repository
    kind: Literal["pr", "diff", "commit", "file"]
    pr: Annotated[int, Field(ge=1, le=2**31 - 1)] | None = None
    commit: Annotated[str, Field(pattern=r"^[a-fA-F0-9]{40}$")] | None = None
    path: Annotated[str, Field(min_length=1, max_length=240)] | None = None

    def url(self) -> str:
        # Apply the same repository validation as existing PR context.
        try:
            repo = Context(type="github_pr", repo=self.repo, number=1).repo
        except ValueError:
            raise PigeonError("reference", "Use a GitHub owner and repository name.") from None
        base = f"https://github.com/{repo}"
        if self.kind in ("pr", "diff") and self.pr and not (self.commit or self.path):
            return f"{base}/pull/{self.pr}" + ("/files" if self.kind == "diff" else "")
        if self.kind == "commit" and self.commit and not (self.pr or self.path):
            return f"{base}/commit/{self.commit}"
        if self.kind == "file" and self.commit and self.path and not self.pr:
            if all(part not in {"", ".", ".."} for part in self.path.split("/")):
                return f"{base}/blob/{self.commit}/{quote(self.path)}"
        raise PigeonError("reference", "Supply a PR number or a commit SHA, with a path for files.")


class Submit(Frame):
    op: Literal["submit"] = "submit"
    message_id: UUID4
    conversation_id: UUID4
    to: Address
    message: Content
    context: Context | None = None
    # Seconds the request may wait before the recipient claims it.
    timeout: Annotated[int, Field(strict=True, ge=1, le=900)] = QUEUE_TIMEOUT


class Inspect(Frame):
    op: Literal["status"] = "status"
    conversation_id: UUID4
    message_id: UUID4 | None = None
    after: Status | None = None
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = 0


class Take(Frame):
    op: Literal["take"] = "take"
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = MAX_WAIT
    # Retrying with the same claim ID returns the turn it already claimed.
    claim_id: UUID4 | None = None


class Watch(Frame):
    op: Literal["watch"] = "watch"
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = MAX_WAIT


class ActivityWatch(Frame):
    op: Literal["activity_watch"] = "activity_watch"
    wait: Annotated[int, Field(strict=True, ge=0, le=MAX_WAIT)] = MAX_WAIT
    skip: Annotated[list[UUID4], Field(max_length=64)] = Field(default_factory=list)
    replies: Annotated[list[UUID4], Field(max_length=50)] = Field(default_factory=list)


class Capabilities(Frame):
    op: Literal["capabilities"] = "capabilities"
    conversation_id: UUID4 | None = None
    features: (
        Annotated[
            list[Literal["wake", "attachments", "websocket", "manual_inbox", "long_messages"]],
            Field(max_length=8),
        ]
        | None
    ) = None


class Finish(Frame):
    op: Literal["finish"] = "finish"
    message_id: UUID4
    status: Literal["completed", "failed", "timed_out"]
    content: Content


class Renew(Frame):
    op: Literal["renew"] = "renew"
    message_id: UUID4


class History(Frame):
    op: Literal["history"] = "history"
    conversation_id: UUID4
    before: Annotated[int, Field(strict=True, ge=1)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=50)] = 20


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
    | ActivityWatch
    | Capabilities
    | Renew
    | History
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
    def __init__(self, code: str, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data
