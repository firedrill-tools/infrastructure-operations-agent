"""Exact original model arguments and fixed process command catalog."""

from .source import keys, revision, service_name, stable_version

TOOL_NAMES = (
    "deployment_status",
    "deployment_history",
    "deploy_version",
    "check_rollout_health",
    "rollback_version",
)
WRITE_TOOLS = frozenset(TOOL_NAMES[2:])
MAX_REQUESTS = 14


def arguments(name, value):
    if name not in TOOL_NAMES:
        raise ValueError("unknown infrastructure tool")
    expected = {"service"}
    if name == "deployment_status":
        expected.add("cached")
    elif name in WRITE_TOOLS:
        expected.add("expected_revision")
        if name == "deploy_version":
            expected.add("version")
    keys(value, expected)
    service_name(value["service"])
    if name == "deployment_status" and type(value["cached"]) is not bool:
        raise ValueError("exact original cached boolean required")
    if name in WRITE_TOOLS:
        revision(value["expected_revision"])
    if name == "deploy_version":
        stable_version(value["version"])
    return value


def command(name, value):
    arguments(name, value)
    if name == "deployment_status":
        return ["status", value["service"], *(["--cached"] if value["cached"] else [])]
    if name == "deployment_history":
        return ["history", value["service"]]
    prefix = {
        "deploy_version": "deploy",
        "check_rollout_health": "health",
        "rollback_version": "rollback",
    }[name]
    return [
        prefix,
        value["service"],
        *([value["version"]] if prefix == "deploy" else []),
        "--revision",
        str(value["expected_revision"]),
    ]
