"""Router (Feature 1.4): picks the answer model by complexity ∩ tenant policy/budget.

Cheap model for simple prompts; stronger model for complex prompts — but only if
the tenant has budget headroom and a complex model is configured. Returns the
purpose key that gateway.complete() should use.
"""
from .. import store
from . import gateway


def _spent_this_period(tenant_id: str) -> float:
    return float(store.usage_summary(tenant_id).get("budget_spent_usd") or 0.0)


def pick_answer_purpose(tenant_id: str, complexity: str) -> str:
    """Return 'answer_complex' or 'answer_simple' based on complexity + budget.

    Resolution is purpose-aware (gateway.first_model sees BOTH the design-list roles —
    Simple vs Primary — and the legacy per-purpose configs): 'simple' only matters when
    answer_simple actually resolves to a DIFFERENT model than answer_complex; with just a
    Primary configured, both purposes hit the same model and we keep 'answer_complex'."""
    simple = gateway.first_model(tenant_id, "answer_simple")
    complex_ = gateway.first_model(tenant_id, "answer_complex")
    has_simple = simple is not None and simple != complex_
    has_complex = complex_ is not None
    if complexity != "complex" or not has_complex:
        return "answer_simple" if has_simple else "answer_complex"

    # complex prompt + complex model exists: honour budget headroom
    budget = store.get_budget(tenant_id)
    if budget is not None and _spent_this_period(tenant_id) >= budget:
        # over budget -> downgrade to the cheaper model if available
        return "answer_simple" if has_simple else "answer_complex"
    return "answer_complex"
