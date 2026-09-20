"""The break-glass store behind a repository interface.

Break-glass requests live in Postgres with the rest of what
argus owns. Routes and the provider depend on `BreakGlassRepository`, never on a database:
`SqlBreakGlass` implements it with SQLAlchemy, and tests use `tests.fakes.InMemoryBreakGlass`.
`tests/db/test_breakglass_contract.py` runs one suite against both.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from argus.db.models import BreakGlassRequestRow

PENDING = "pending"
APPROVED = "approved"
DENIED = "denied"


@dataclass(frozen=True)
class BreakGlassRequest:
    id: str
    created_at: str
    reason: str
    target_host: str
    evidence: str
    proposed_objective: str
    status: str


class BreakGlassRepository(Protocol):
    async def create_request(
        self, *, reason: str, target_host: str, evidence: str, proposed_objective: str
    ) -> BreakGlassRequest: ...

    async def list_requests(self, *, status: str | None = None) -> list[BreakGlassRequest]:
        """Newest first, optionally only those with this status."""

    async def get_request(self, request_id: str) -> BreakGlassRequest | None: ...

    async def set_request_status(self, request_id: str, status: str) -> BreakGlassRequest:
        """Raises `KeyError` for an unknown id."""


def _request(row: BreakGlassRequestRow) -> BreakGlassRequest:
    return BreakGlassRequest(
        id=row.id,
        created_at=row.created_at.isoformat(),
        reason=row.reason,
        target_host=row.target_host,
        evidence=row.evidence,
        proposed_objective=row.proposed_objective,
        status=row.status,
    )


class SqlBreakGlass:
    def __init__(self, engine: AsyncEngine) -> None:
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def create_request(
        self, *, reason: str, target_host: str, evidence: str, proposed_objective: str
    ) -> BreakGlassRequest:
        row = BreakGlassRequestRow(
            id=secrets.token_hex(4),  # short: it ends up in URLs and session labels
            reason=reason,
            target_host=target_host,
            evidence=evidence,
            proposed_objective=proposed_objective,
            status=PENDING,
        )
        async with self._session.begin() as session:
            session.add(row)
            await session.flush()
            await session.refresh(row)  # the server-side created_at
        return _request(row)

    async def list_requests(self, *, status: str | None = None) -> list[BreakGlassRequest]:
        query = select(BreakGlassRequestRow).order_by(
            BreakGlassRequestRow.created_at.desc(), BreakGlassRequestRow.id.desc()
        )
        if status is not None:
            query = query.where(BreakGlassRequestRow.status == status)
        async with self._session() as session:
            return [_request(row) for row in (await session.scalars(query)).all()]

    async def get_request(self, request_id: str) -> BreakGlassRequest | None:
        async with self._session() as session:
            row = await session.get(BreakGlassRequestRow, request_id)
        return _request(row) if row else None

    async def set_request_status(self, request_id: str, status: str) -> BreakGlassRequest:
        statement = (
            update(BreakGlassRequestRow)
            .where(BreakGlassRequestRow.id == request_id)
            .values(status=status)
            .returning(BreakGlassRequestRow)
        )
        async with self._session.begin() as session:
            row = (await session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise KeyError(f"No break-glass request with id '{request_id}'")
        return _request(row)
