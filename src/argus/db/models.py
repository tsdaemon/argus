"""SQLAlchemy models for the Argus-owned tables: History, and the break-glass store.

Deleting a thread cascades to everything that references it. Both layers are declared: the
ORM `cascade` (used when children are loaded in a session) and `ON DELETE CASCADE` on the
foreign keys, which `passive_deletes=True` defers to so the database does the work in one
statement. Alembic migrations are the source of truth for the actual schema; `alembic check`
fails if these models drift from them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _cascade_fk(target: str, *, nullable: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete="CASCADE"), nullable=nullable
    )


_children = {"cascade": "all, delete-orphan", "passive_deletes": True}


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (Index("threads_updated_at_idx", "updated_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="active")

    runs: Mapped[list[Run]] = relationship(back_populates="thread", **_children)
    messages: Mapped[list[Message]] = relationship(back_populates="thread", **_children)
    tool_calls: Mapped[list[ToolCall]] = relationship(back_populates="thread", **_children)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = _cascade_fk("threads.id")
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status: Mapped[str] = mapped_column(Text, server_default="running")
    error: Mapped[str | None] = mapped_column(Text)

    thread: Mapped[Thread] = relationship(back_populates="runs")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("thread_id", "agui_id", name="messages_thread_agui_key"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = _cascade_fk("threads.id")
    run_id: Mapped[uuid.UUID | None] = _cascade_fk("runs.id", nullable=True)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    agui_id: Mapped[str | None] = mapped_column(Text)
    chat_order: Mapped[int] = mapped_column(BigInteger, Identity())

    thread: Mapped[Thread] = relationship(back_populates="messages")


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    thread_id: Mapped[uuid.UUID] = _cascade_fk("threads.id")
    run_id: Mapped[uuid.UUID] = _cascade_fk("runs.id")
    tool_id: Mapped[str] = mapped_column(Text)
    tool_class: Mapped[str] = mapped_column(Text)
    args: Mapped[Any] = mapped_column(JSONB)
    result: Mapped[Any | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    requested_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    thread: Mapped[Thread] = relationship(back_populates="tool_calls")
    approvals: Mapped[list[Approval]] = relationship(back_populates="tool_call", **_children)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tool_call_id: Mapped[uuid.UUID] = _cascade_fk("tool_calls.id")
    tool_id: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    tool_call: Mapped[ToolCall] = relationship(back_populates="approvals")


class BreakGlassRequestRow(Base):
    __tablename__ = "breakglass_requests"
    __table_args__ = (Index("breakglass_requests_status_idx", "status", "created_at"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    reason: Mapped[str] = mapped_column(Text)
    target_host: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text)
    proposed_objective: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")


class AdminAccountRow(Base):
    """The single break-glass web admin: the one row is pinned to id 1."""

    __tablename__ = "admin_account"
    __table_args__ = (CheckConstraint("id = 1", name="admin_account_single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    session_secret: Mapped[str] = mapped_column(Text)
