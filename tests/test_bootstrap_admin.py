from __future__ import annotations

import pytest

from app.models.runtime_config import SystemSettings
from app.models.user import User
from deploy import bootstrap_admin


class FakeDatabase:
    def __init__(self, *, has_runtime=True, admin=None):
        self.has_runtime = has_runtime
        self.admin = admin
        self.added = []
        self.committed = False
        self.rolled_back = False

    async def scalar(self, query):
        model = query.column_descriptions[0]["entity"]
        if model is SystemSettings:
            return SystemSettings(id=1, revision=0, updated_at=0) if self.has_runtime else None
        if model is User:
            return self.admin
        raise AssertionError(model)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if isinstance(value, User):
                value.id = 77

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture(autouse=True)
def cheap_password_hash(monkeypatch):
    async def fake_hash(value):
        return "hashed-test-password"
    monkeypatch.setattr(bootstrap_admin, "hash_password", fake_hash)


@pytest.mark.asyncio
async def test_first_admin_is_created_with_hashed_password():
    db = FakeDatabase()
    user_id = await bootstrap_admin.create_first_admin(
        db, name="Admin", surname="User", email="ADMIN@example.test", password="fixture-password"
    )
    assert user_id == 77
    assert db.committed
    user = db.added[0]
    assert user.email == "admin@example.test"
    assert user.role == "admin"
    assert user.password == "hashed-test-password"
    assert user.password != "fixture-password"


@pytest.mark.asyncio
async def test_existing_admin_guard_does_not_modify_users():
    existing = User(id=1, name="Existing", surname="Admin", email="admin@example.test",
                    password="existing-hash", role="admin", added_at=1, updated_at=1)
    db = FakeDatabase(admin=existing)
    with pytest.raises(RuntimeError, match="administrator already exists"):
        await bootstrap_admin.create_first_admin(
            db, name="Other", surname="User", email="other@example.test", password="fixture-password"
        )
    assert db.added == []
    assert not db.committed


@pytest.mark.asyncio
async def test_admin_bootstrap_requires_initialized_runtime_row():
    db = FakeDatabase(has_runtime=False)
    with pytest.raises(RuntimeError, match="runtime configuration is not initialized"):
        await bootstrap_admin.create_first_admin(
            db, name="Admin", surname="User", email="admin@example.test", password="fixture-password"
        )
    assert db.added == []
    assert not db.committed
