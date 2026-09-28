"""Operation identities for scheduling and reading historical tool receipts.

The arxiv_* keys are internal compatibility identifiers, not advertised tools.
"""

ARXIV_ACTIONS = {
    "search": "arxiv_search",
    "lookup": "arxiv_lookup",
    "import": "arxiv_import_paper",
    "status": "arxiv_ingestion_status",
}


def tool_operation(name, arguments=None):
    if name == "arxiv":
        return ARXIV_ACTIONS.get(str((arguments or {}).get("action") or ""), "arxiv_invalid")
    return name


def observation_operation(observation):
    if isinstance(observation, dict):
        return tool_operation(observation.get("tool", ""), observation.get("arguments"))
    return tool_operation(observation.tool, observation.arguments)


def arxiv_argument_error(arguments):
    action = arguments.get("action")
    if not isinstance(action, str) or action not in ARXIV_ACTIONS:
        return "action must be search, lookup, import or status"
    if action == "lookup":
        ids = arguments.get("arxiv_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 20 or any(not isinstance(v, str) or not v.strip() for v in ids):
            return "lookup requires arxiv_ids: a list of 1 to 20 exact arXiv identifiers"
    else:
        key = {"search": "query", "import": "arxiv_id", "status": "job_id"}[action]
        if not isinstance(arguments.get(key), str) or not arguments[key].strip():
            return f"{action} requires {key}"
    if action == "import" and arguments.get("approval_mode", "risk") not in ("risk", "manual", "auto"):
        return "approval_mode must be risk, manual or auto"
    return ""
