from harness.core import HarnessError
from harness.model_gateway import ModelProfile

from .models import ContextPolicy


def input_budget(profile: ModelProfile, policy: ContextPolicy) -> dict:
    limits = profile.limits
    output = min(policy.output_reserve, limits.output_limit) if limits.output_limit else policy.output_reserve
    candidates = []
    if limits.context_window is not None:
        candidates.append(limits.context_window - output)
    if limits.input_limit is not None:
        candidates.append(limits.input_limit)
    if not candidates:
        raise HarnessError(
            "unknown_context_limit", "Model input limit is unknown; configure verified limits before a run"
        )
    upper = min(candidates) - policy.safety_tokens
    if upper <= 0:
        raise HarnessError(
            "context_budget_invalid", "Output reservation and safety margin leave no input budget"
        )
    return {
        "input_budget": upper,
        "output_reserve": output,
        "safety_tokens": policy.safety_tokens,
        "context_window": limits.context_window,
        "input_limit": limits.input_limit,
        "soft_threshold": int(upper * policy.soft_threshold),
        "compaction_target": int(upper * policy.compaction_target),
    }
