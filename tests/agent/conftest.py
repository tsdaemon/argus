"""Skip `tests/agent/` gracefully when the `agent` extra isn't installed. Run for real
with `uv run --extra agent pytest tests/agent`."""

import pytest

pytest.importorskip("langchain_core")
