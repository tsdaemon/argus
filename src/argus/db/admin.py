"""The single admin account behind a repository interface.

One account guards the whole app (see `argus.api.auth`) and the break-glass pages. It lives in
Postgres with the rest of what argus owns; tests use `tests.fakes.InMemoryAdmin`, and
`tests/db/test_admin_contract.py` runs one suite against both.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from argus.db.models import AdminAccountRow


@dataclass(frozen=True)
class AdminAccount:
    username: str
    password_hash: str
    session_secret: str


class AdminRepository(Protocol):
    async def get_admin(self) -> AdminAccount | None: ...

    async def create_admin_if_absent(self, account: AdminAccount) -> bool:
        """Store the account unless one exists; True only for the call that stored it."""


class SqlAdmin:
    def __init__(self, engine: AsyncEngine) -> None:
        self._session = async_sessionmaker(engine, expire_on_commit=False)

    async def get_admin(self) -> AdminAccount | None:
        async with self._session() as session:
            row = await session.get(AdminAccountRow, 1)
        if row is None:
            return None
        return AdminAccount(row.username, row.password_hash, row.session_secret)

    async def create_admin_if_absent(self, account: AdminAccount) -> bool:
        statement = (
            insert(AdminAccountRow)
            .values(
                id=1,
                username=account.username,
                password_hash=account.password_hash,
                session_secret=account.session_secret,
            )
            .on_conflict_do_nothing(index_elements=[AdminAccountRow.id])
            .returning(AdminAccountRow.id)
        )
        async with self._session.begin() as session:
            return (await session.execute(statement)).scalar_one_or_none() is not None
