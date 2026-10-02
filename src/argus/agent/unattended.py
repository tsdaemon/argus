"""Unattended runs cannot answer human approvals, including inside the fixed worker."""

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from argus.agent.classification import call_decision
from argus.policy import PolicyDecision


class UnattendedMiddleware(AgentMiddleware):
    def __init__(self, policy, specs):
        super().__init__()
        self.policy = policy
        self.specs = specs

    async def awrap_tool_call(self, request, handler):
        call = request.tool_call
        spec = self.specs.get(call["name"])
        if spec is not None:
            decision = (
                call_decision(self.policy, spec, request.state, call["id"])[0]
                if spec.classify is not None
                else self.policy.decide(spec.tool_id, spec.tool_class)
            )
            if decision is not PolicyDecision.ALLOW:
                return ToolMessage(
                    content=(
                        f"Refused: `{spec.tool_id}` requires human approval or is denied. "
                        "This run is unattended. Explain what needs approval; you may "
                        "file a break-glass request if appropriate."
                    ),
                    name=call["name"],
                    tool_call_id=call["id"],
                    status="error",
                )
        return await handler(request)
