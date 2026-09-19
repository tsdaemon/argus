"""Real API and agent graph for Playwright; deterministic model, fake Docker, in-memory stores.

No Postgres and no model API key are needed, and no operational provider ever connects to
a daemon.
"""

import asyncio
import os
import tempfile
from unittest.mock import MagicMock, patch

import uvicorn
from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from argus.api.app import build_app
from argus.config import AgentConfig, ArgusConfig, ProviderEntry
from tests.api.test_approvals import ToolCallingModel, restart_call
from tests.fakes import InMemoryHistory


class BrowserModel(ToolCallingModel):
    def _generate(self, messages, stop=None, **kwargs):
        last_user = next(
            i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)
        )
        question = str(messages[last_user].content)
        results = [m for m in messages[last_user + 1 :] if isinstance(m, ToolMessage)]
        if results:
            response = AIMessage(content=f"Operation reviewed. {results[-1].content}")
        elif "restart" in question.lower():
            names = (
                ["example-service", "second-service"] if "both" in question else ["example-service"]
            )
            response = AIMessage(
                content="I’ll ask for your approval before making changes.",
                tool_calls=[restart_call(name) for name in names],
            )
        else:
            response = AIMessage(content=f"Read-only review complete: {question}")
        return ChatResult(generations=[ChatGeneration(message=response)])

    async def _agenerate(self, messages, stop=None, **kwargs):
        await asyncio.sleep(0.1)
        return self._generate(messages, stop=stop, **kwargs)


register_harness_profile(
    "browsermodel",
    HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
)


async def serve():
    operations = []
    docker = MagicMock()
    docker.containers.get.side_effect = lambda name: MagicMock(
        restart=lambda **_: operations.append(name)
    )
    with (
        tempfile.TemporaryDirectory(prefix="argus-ui-") as workspace,
        patch("argus.agent.graph._build_model", lambda *_: BrowserModel(tool_calls=[])),
        patch("argus.providers.docker_provider.docker.DockerClient", lambda **_: docker),
    ):
        config = ArgusConfig(
            agent=AgentConfig(workspace_root=workspace),
            providers={"docker": ProviderEntry(enabled=True)},
        )
        app = build_app(config, InMemorySaver(), InMemoryHistory())

        @app.get("/__test__/operations")
        async def observed_operations():
            return operations

        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8422)).serve()


if __name__ == "__main__":
    os.environ.pop("ARGUS_MCP_TOKEN", None)
    asyncio.run(serve())
