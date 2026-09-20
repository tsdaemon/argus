"""One behaviour suite for every `AdminRepository`: the in-memory fake and `SqlAdmin`."""

from argus.db.admin import AdminAccount


async def test_the_admin_account_is_stored_once(admin_repo):
    assert await admin_repo.get_admin() is None
    first = AdminAccount("admin", "hash-one", "secret-one")
    second = AdminAccount("other", "hash-two", "secret-two")

    assert await admin_repo.create_admin_if_absent(first) is True
    assert await admin_repo.create_admin_if_absent(second) is False

    assert await admin_repo.get_admin() == first
