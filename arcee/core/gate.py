"""The permission gate (spec sections 6 and 12): every tool call passes here before it runs.

The tier comes from the tool (which may raise it for risky arguments), then the
policy's overrides, then the task's "ask me for everything" flag and the model's
own request to confirm. Nothing the model writes can lower a tier. The policy can
move a tool between `auto` and `confirm` or ban it with `never`, but a call the
tool itself marks `never` is refused whatever the policy says.
"""

import asyncio
import itertools
from dataclasses import dataclass
from typing import Literal, Protocol

from arcee.core.policy import Policy, Tier
from arcee.core.protocol import ConfirmRequest
from arcee.tools.registry import ToolCall

CONFIRM_TIMEOUT_S = 120


class Confirmer(Protocol):
    """Asks the user. The CLI reads stdin; the daemon will raise a D-Bus signal."""

    async def confirm(self, request: ConfirmRequest) -> bool: ...


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    tier: Tier
    approved_by: Literal["policy", "user"] | None
    reason: str = ""


class Gate:
    def __init__(
        self, policy: Policy, confirmer: Confirmer, *, timeout_s: float = CONFIRM_TIMEOUT_S
    ) -> None:
        self.policy = policy
        self._confirmer = confirmer
        self._timeout_s = timeout_s
        self._ids = itertools.count(1)

    def tier(self, call: ToolCall, *, model_asked: bool = False) -> Tier:
        tier = call.tool.tier_for(call.args, self.policy)
        if tier is Tier.NEVER:
            return tier
        tier = self.policy.tier_overrides.get(call.tool.name, tier)
        if tier is Tier.AUTO and (self.policy.confirm_all or model_asked):
            tier = Tier.CONFIRM
        return tier

    async def check(self, call: ToolCall, task_id: str, *, model_asked: bool = False) -> Verdict:
        """Allow, ask the user, or refuse. A confirmation that times out counts as denied."""
        tier = self.tier(call, model_asked=model_asked)
        if tier is Tier.NEVER:
            return Verdict(False, tier, None, call.tool.refusal(call.args, self.policy))
        if tier is Tier.AUTO:
            return Verdict(True, tier, "policy")
        request = ConfirmRequest(
            request_id=f"c_{next(self._ids)}",
            task_id=task_id,
            tool=call.tool.name,
            sentence=call.tool.sentence(call.args),
            risk=call.tool.risk,
            args_preview=call.tool.preview(call.args),
            timeout_s=max(1, round(self._timeout_s)),
        )
        try:
            approved = await asyncio.wait_for(self._confirmer.confirm(request), self._timeout_s)
        except TimeoutError:
            return Verdict(False, tier, None, "the user did not answer in time")
        if not approved:
            return Verdict(False, tier, None, "the user declined")
        return Verdict(True, tier, "user")
