"""Argus-owned Postgres tables: `threads`, `runs`, `messages`, `tool_calls`, `approvals`
— the raw History log. Kept separate from LangGraph's own checkpoint tables (State),
which `langgraph-checkpoint-postgres` manages itself; never hand-edited here.
"""

from __future__ import annotations
