from __future__ import annotations

import pytest

from argus.auth import StaticTokenVerifier


@pytest.mark.asyncio
async def test_correct_token_verifies():
    verifier = StaticTokenVerifier("correct-token")
    access = await verifier.verify_token("correct-token")
    assert access is not None
    assert access.token == "correct-token"


@pytest.mark.asyncio
async def test_wrong_token_is_rejected():
    verifier = StaticTokenVerifier("correct-token")
    assert await verifier.verify_token("wrong-token") is None


@pytest.mark.asyncio
async def test_empty_token_is_rejected():
    verifier = StaticTokenVerifier("correct-token")
    assert await verifier.verify_token("") is None
