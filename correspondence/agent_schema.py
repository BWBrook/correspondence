"""Discoverable input bounds and structured results for the agent interface."""
from typing import Annotated, Any, Literal

from pydantic import Field
from typing_extensions import TypedDict

PositiveID = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Checkpoint = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=500)]
ThreadLimit = Annotated[int, Field(strict=True, ge=1, le=1000)]
ByteBudget = Annotated[int, Field(strict=True, ge=1000, le=10_000_000)]
MessageIDs = Annotated[list[PositiveID], Field(min_length=1, max_length=50)]
Kind = Literal["note", "question", "proposal", "evidence", "decision", "counterexample"]
Participant = Literal["Barry", "Astra", "Fable"]
ReadMode = Literal["oldest", "latest", "unread"]
Status = Literal["open", "resolved"]


class Items(TypedDict):
    items: list[dict[str, Any]]


class BackwardPage(Items):
    has_more: bool
    next_before: int | None


class InboxPage(Items):
    has_more: bool
    next_after: int
    through: int
    total: int
    returned_bytes: int


class MessageBatch(TypedDict):
    messages: list[dict[str, Any]]
    missing_ids: list[int]
    omitted_ids: list[int]
    returned_bytes: int
    oversized: bool


class Updates(TypedDict):
    thread: dict[str, Any]
    messages: list[dict[str, Any]]
    has_more: bool
    next_after: int
    through: int
    returned_bytes: int
    oversized: bool
