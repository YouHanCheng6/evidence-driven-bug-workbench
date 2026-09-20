"""Execution budget shared by the main assistant's HTTP and IM entry points."""

MAIN_ASSISTANT_NAME = "personal-assistant"
MAIN_ASSISTANT_RECURSION_LIMIT = 100_000


def apply_main_assistant_run_policy(config, requested_config=None):
    sections = [config.get("context") or {}, config.get("configurable") or {}]
    if not any(section.get("agent_name") == MAIN_ASSISTANT_NAME for section in sections):
        return
    requested = (requested_config or {}).get("recursion_limit")
    if isinstance(requested, int) and not isinstance(requested, bool) and requested > 0:
        config["recursion_limit"] = min(requested, MAIN_ASSISTANT_RECURSION_LIMIT)
    else:
        config["recursion_limit"] = MAIN_ASSISTANT_RECURSION_LIMIT
