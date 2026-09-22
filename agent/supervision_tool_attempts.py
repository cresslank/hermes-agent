"""Native read attempt receipts; serialized tool errors never supply authority.

The ordinary authorized dispatcher consumes an optional, exact profile declaration.
It describes retry/repetition intent, not permission to skip a main-agent operation.
Only the file owner can attach a typed failure while that dispatch is in flight.
The existing search_files route is offered verbatim, never generated or executed.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from agent.supervision_efficiency import Route, _pin

VERSION = "supervision.file-attempt.v1"
FLAGS = ("registered_poll", "registered_retry", "pagination", "user_repetition")
_capture: ContextVar[list | None] = ContextVar("native_file_attempt", default=None)


def publish_file_failure(result, classification):
    """File-owner-only receipt channel. JSON copies cannot re-create this channel."""
    capture = _capture.get()
    if capture is not None and type(classification) is tuple and classification in (
        ("not_regular", "regular_file_guard", False),
        ("not_found", "unicode_recovery_and_similar_files", True),
    ):
        capture.append((result, classification))
    return result


def _path_pin(path):
    value = Path(path)
    if not value.is_absolute() or str(value.resolve()) != path:
        return None
    try:
        info = value.stat()
    except FileNotFoundError:
        if not value.parent.is_dir():
            return None
        return _pin((path, "missing", _path_pin(str(value.parent))))
    return _pin((path, info.st_dev, info.st_ino, info.st_mode, info.st_size,
                 info.st_mtime_ns, info.st_ctime_ns))


def _policy(runtime, path):
    from hermes_cli.config import load_config_readonly
    from hermes_constants import hermes_home_key
    from agent.file_safety import get_read_block_error
    from agent.redact import _is_secret_file_arg
    from tools.registry import registry
    from tools.file_tools import _handle_search_files

    agent = runtime.agent()
    if (runtime.closed or agent is None or getattr(agent, "_interrupt_requested", False)
            or hermes_home_key() != runtime.revision.profile or runtime.completeness.omitted
            or not 0 < len(runtime.sources) < 12 or not runtime._registrations()):
        return None
    config = load_config_readonly() or {}
    supervision = config.get("supervision", {})
    if not isinstance(supervision, dict) or supervision.get("enabled") is not True:
        return None
    entries = supervision.get("plugins", {})
    registrations = runtime._registrations()
    if not isinstance(entries, dict):
        return None
    generations = []
    for registration in registrations:
        entry_policy = entries.get(registration.plugin_id, {})
        grants = entry_policy.get("grants", ()) if isinstance(entry_policy, dict) else ()
        if (not isinstance(grants, (list, tuple)) or not {"observe", "advise"}.issubset(grants)
                or not {"observe", "advise"}.issubset(registration.grants)):
            return None
        generations.append((registration.plugin_id, registration.generation))
    policies = supervision.get("file_attempts")
    if not isinstance(policies, list) or not 0 < len(policies) <= 16:
        return None
    matches = [p for p in policies if isinstance(p, dict) and p.get("path") == path]
    if len(matches) != 1:
        return None
    policy = matches[0]
    if (set(policy) != {"version", "path", "instructions_sha256", "alternative", *FLAGS}
            or policy["version"] != VERSION or any(type(policy[k]) is not bool for k in FLAGS)
            or policy["instructions_sha256"] != [hashlib.sha256(s.encode()).hexdigest() for s in runtime.sources.values()]):
        return None
    alternative = policy["alternative"]
    if (not isinstance(alternative, dict)
            or set(alternative) != {"tool", "arguments", "authorized", "privacy_allowed"}
            or alternative["tool"] != "search_files"
            or alternative["authorized"] is not True or alternative["privacy_allowed"] is not True):
        return None
    args = alternative["arguments"]
    if (not isinstance(args, dict) or set(args) != {"path", "pattern", "target", "limit", "offset", "order"}
            or args["path"] not in {path, str(Path(path).parent)}
            or args["target"] != "files" or args["order"] != "discovery"
            or type(args["pattern"]) is not str or not 0 < len(args["pattern"]) <= 200
            or type(args["limit"]) is not int or not 1 <= args["limit"] <= 50
            or type(args["offset"]) is not int or args["offset"] != 0):
        return None
    # The declared route is directory enumeration, not a shell command or an
    # implicit capability grant. An unavailable/overridden session tool is unknown.
    entry = registry.get_entry("search_files")
    if ("search_files" not in (getattr(agent, "valid_tool_names", None) or ())
            or entry is None or entry.handler is not _handle_search_files
            or get_read_block_error(path) or _is_secret_file_arg(path)
            or get_read_block_error(args["path"]) or _is_secret_file_arg(args["path"])
            or _path_pin(path) is None or _path_pin(args["path"]) is None
            or not Path(args["path"]).is_dir()):
        return None
    links = runtime.action_requirements({"path": path})
    if len(links) != 1:
        return None
    return policy, _pin((_path_pin(path), _path_pin(args["path"]), generations)), links[0], entry.handler


@dataclass(frozen=True)
class AttemptReceipt:
    arguments_pin: str
    result: str
    classification: tuple
    policy: dict
    route: Route
    current: object


def _prepare(owner, name, arguments, task_id):
    runtime = owner.runtime
    if name != "read_file" or not isinstance(arguments, dict):
        return None
    path = arguments.get("path")
    if not isinstance(path, str) or not 0 < len(path) <= 2400:
        return None
    qualified = _policy(runtime, path)
    if qualified is None or qualified[1] is None:
        return None
    from tools.file_tools import _get_file_ops
    from tools.file_operations import FileOperations
    from tools.environments.local import LocalEnvironment
    file_ops = _get_file_ops(task_id)
    if not isinstance(file_ops, FileOperations) or not isinstance(file_ops.env, LocalEnvironment):
        return None
    policy, snapshot, requirement, handler = qualified
    revision = runtime.revision
    # Serialize the declaration now; mutation of the caller/config cannot change
    # the captured authority. Original revision and source pin survive the wait.
    policy = json.loads(json.dumps(policy))
    def current():
        try:
            return (runtime.revision == revision and _get_file_ops(task_id) is file_ops
                    and isinstance(file_ops.env, LocalEnvironment)
                    and _policy(runtime, path) == (policy, snapshot, requirement, handler))
        except (OSError, ValueError, TypeError):
            return False
    alternative = policy["alternative"]
    route_id = "file-search:" + _pin((path, alternative))
    route = Route(route_id, json.dumps(alternative, sort_keys=True), path,
                  "Exact profile-authorized directory search and current session file tool",
                  _pin((snapshot, policy)), "Read-only filename enumeration; no command or content mutation",
                  "readonly", current)
    return policy, route, current


def run_attempt(agent, name, arguments, call_id, task_id, execute):
    """Wrap the actual dispatch, after scope/hooks/guardrails/effect fences.

    Receipt collection is optional; failures in supervision preserve execution.
    No tool-result dictionary or stderr string is parsed into owner policy.
    """
    from agent.supervision_efficiency import for_agent
    owner = None
    prepared = None
    try:
        owner = for_agent(agent)
        if owner is not None:
            with owner.lock:
                owner._sync()
                # Exact route dispatch is recorded even when the original failure
                # was not judged or when its recommendation has already been used.
                for route in tuple(owner.routes.values()):
                    if route.id.startswith("file-search:") and name == "search_files":
                        if json.loads(route.text)["arguments"] == arguments:
                            owner.route_attempted(route.id)
                prepared = _prepare(owner, name, arguments, task_id)
    except Exception:
        prepared = None
    if prepared is None or owner is None:
        return execute()
    captured = []
    token = _capture.set(captured)
    try:
        result = execute()
    finally:
        _capture.reset(token)
    try:
        policy, route, current = prepared
        if len(captured) == 1 and captured[0][0] == result and current():
            # A page request is intrinsically pagination even when the profile
            # declaration was written for the first page.
            flags = {k: policy[k] for k in FLAGS}
            if arguments.get("offset", 1) != 1:
                flags["pagination"] = True
            classification = captured[0][1]
            flags["deterministic_handler_available"] = classification[2]
            with owner.lock:
                owner._sync()
                if len(owner.native_attempts) < 32:
                    prior = owner.routes.get(route.id)
                    if prior is not None and prior.prerequisite_revision == route.prerequisite_revision and prior.available():
                        route = prior
                    else:
                        owner.register_route(route)
                    owner.native_attempts[call_id] = AttemptReceipt(
                        _pin(arguments), result, classification, flags, route, current)
    except Exception:
        pass  # Optional observation never changes the real return value.
    return result
