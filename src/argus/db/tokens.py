"""Named, revocable credentials. Secrets are returned once and only hashes persist."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from argus.db.models import ApiTokenRow


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ApiToken:
    id: UUID
    name: str
    secret_hash: str
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    interface: str = "a2a"

    def public(self) -> dict:
        return {key: value for key, value in vars(self).items() if key != "secret_hash"}


class TokenRepository(Protocol):
    async def create(self, token: ApiToken) -> None: ...
    async def get(self, token_id: UUID) -> ApiToken | None: ...
    async def list(self) -> list[ApiToken]: ...
    async def revoke(self, token_id: UUID) -> bool: ...
    async def used(self, token_id: UUID) -> None: ...


class TokenAuth:
    def __init__(self, repository: TokenRepository):
        self.repository = repository

    async def issue(self, name: str, expires_at: datetime | None = None) -> tuple[ApiToken, str]:
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("Name must contain between 1 and 100 characters.")
        if expires_at is not None and (expires_at.tzinfo is None or expires_at <= now()):
            raise ValueError("Expiry must be a future time with a timezone.")
        token_id = uuid4()
        secret = secrets.token_urlsafe(32)
        token = ApiToken(
            token_id, name, hashlib.sha256(secret.encode()).hexdigest(), now(), expires_at
        )
        await self.repository.create(token)
        return token, f"argus_{token_id.hex}_{secret}"

    async def verify(self, value: str) -> ApiToken | None:
        try:
            prefix, token_id, secret = value.split("_", 2)
            if prefix != "argus" or len(value) > 200:
                return None
            token = await self.repository.get(UUID(token_id))
        except ValueError:
            return None
        if token is None or token.revoked_at or token.interface != "a2a":
            return None
        if token.expires_at is not None and token.expires_at <= now():
            return None
        if not hmac.compare_digest(token.secret_hash, hashlib.sha256(secret.encode()).hexdigest()):
            return None
        await self.repository.used(token.id)
        return token


class SqlTokens:
    def __init__(self, engine: AsyncEngine):
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def create(self, token: ApiToken) -> None:
        async with self._session.begin() as session:
            session.add(ApiTokenRow(**vars(token)))

    async def get(self, token_id: UUID) -> ApiToken | None:
        async with self._session() as session:
            row = await session.get(ApiTokenRow, token_id)
            return _token(row) if row else None

    async def list(self) -> list[ApiToken]:
        async with self._session() as session:
            rows = await session.scalars(
                select(ApiTokenRow).order_by(ApiTokenRow.created_at.desc())
            )
            return [_token(row) for row in rows]

    async def revoke(self, token_id: UUID) -> bool:
        async with self._session.begin() as session:
            result = await session.execute(
                update(ApiTokenRow).where(ApiTokenRow.id == token_id).values(revoked_at=now())
            )
            return result.rowcount > 0

    async def used(self, token_id: UUID) -> None:
        async with self._session.begin() as session:
            await session.execute(
                update(ApiTokenRow).where(ApiTokenRow.id == token_id).values(last_used_at=now())
            )


def _token(row: ApiTokenRow) -> ApiToken:
    return ApiToken(**{key: getattr(row, key) for key in ApiToken.__dataclass_fields__})
