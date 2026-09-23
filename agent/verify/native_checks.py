"""Closed, owner-local verification contracts for the ordinary verify runner.

V1 only checks a bounded UTF-8 JSON object and up to three literal top-level keys.
It does not certify shell commands, imports, schemas with refs, clocks, networks,
workspace acceptance, or arbitrary code. The closed evaluator takes only frozen
bytes and literal keys; no ambient environment variables or external dependencies
are allowed. Unknown protection metadata permits execution, never receipt reuse.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from agent.supervision_efficiency import VerificationCheck, _pin

VERSION = "hermes.verify-check.v1"
MAX_BYTES = 65536
FLAGS = ("required", "external_write_readback", "acceptance_test", "independent_review", "time_sensitive", "explicit_user_check")
BASE_KEYS = {"version", "path", "kind", "keys", "description"}


def _file_state(info):
    # Access time changes caused by our own read are not a source mutation.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _bytes(path, limit=MAX_BYTES):
    # No symlink parent/final redirection or special files; exact absolute paths.
    path = Path(path)
    if not path.is_absolute() or str(path.resolve()) != str(path):
        raise ValueError("native_check_path")
    from agent.file_safety import get_read_block_error
    from agent.redact import _is_secret_file_arg
    if get_read_block_error(str(path)) or _is_secret_file_arg(str(path)):
        raise ValueError("native_check_path")
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK"):
        raise ValueError("native_check_platform")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("native_check_bounds")
        data = os.read(fd, limit + 1)
        after = os.fstat(fd)
        if (_file_state(before) != _file_state(after) or len(data) != before.st_size or len(data) > limit
                or _file_state(os.stat(path, follow_symlinks=False)) != _file_state(after)):
            raise ValueError("native_check_changed")
        return data
    finally:
        os.close(fd)


def _spec(raw, root):
    if (type(raw) is not dict or not BASE_KEYS <= raw.keys()
            or not raw.keys() <= BASE_KEYS | set(FLAGS) | {"fresh"} or raw["version"] != VERSION
            or raw["kind"] != "json_object_keys"
            or type(raw["path"]) is not str or not raw["path"]
            or type(raw["description"]) is not str or not 0 < len(raw["description"]) <= 600
            or type(raw["keys"]) is not list or len(raw["keys"]) > 3
            or any(type(k) is not str or not 0 < len(k) <= 100 for k in raw["keys"])
            or len(set(raw["keys"])) != len(raw["keys"])):
        raise ValueError("invalid_native_check")
    relative = Path(raw["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("native_check_path")
    return Path(root).resolve() / relative


def _dependencies():
    # Exact installed parser/interpreter bytes, not a package/version label.
    # This closed evaluator has no project imports, plugins, shell, refs or env.
    import _json
    import json.decoder
    import json.scanner
    files = [sys.executable, __file__, json.__file__, json.decoder.__file__, json.scanner.__file__]
    if "_json" not in sys.builtin_module_names:
        files.append(_json.__file__)  # static builds bind it through the interpreter bytes
    return _pin([(str(Path(f).resolve()), hashlib.sha256(
        _bytes(Path(f).resolve(), 32 * 1024 * 1024)).hexdigest()) for f in files])


def _environment():
    return _pin((sys.version, sys.implementation.name, sys.implementation.cache_tag,
                 sys.byteorder, sys.get_int_max_str_digits(), sys.getrecursionlimit()))


def _evaluate(data, keys):
    value = json.loads(data.decode("utf-8"))
    return type(value) is dict and all(key in value for key in keys)


def _capture_contract(raw, path, ident, *, plugin_owned):
    data = _bytes(path)
    # Missing/unknown flags remain unknown. Never derive them from names/prose.
    if any(type(raw.get(k)) is not bool for k in FLAGS) or type(raw.get("fresh", False)) is not bool:
        return data, None
    claims = tuple("json:" + _pin((str(path), assertion)) for assertion in
                   ("object", *("key:" + k for k in sorted(raw["keys"]))))
    try:
        dependency = _dependencies()
        environment = _environment()
    except (OSError, ValueError, AttributeError):
        return data, None
    from dataclasses import asdict
    from agent.supervision_facade import registrations_for_scope
    _, efficiency = _active()
    scope = None
    if efficiency is not None:
        revision = efficiency.runtime.revision
        scope = (asdict(revision), sorted((r.plugin_id, r.generation)
                 for r in registrations_for_scope(revision.profile)))
    check = VerificationCheck(
        id=ident, text=raw["description"] + "\n" + json.dumps({"path": str(path), "keys": raw["keys"]}),
        claim_ids=claims, snapshot=_pin((str(path), hashlib.sha256(data).hexdigest(), scope)),
        input_fingerprint=_pin(("utf-8", str(path), hashlib.sha256(data).hexdigest())),
        dependency_fingerprint=dependency, environment=environment,
        plugin_owned=plugin_owned, fresh=raw.get("fresh", False), **{k: raw[k] for k in FLAGS})
    return data, check


def _active():
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.supervision_efficiency import for_agent
    agent = get_active_subagent_parent()
    return agent, for_agent(agent) if agent is not None else None


def run_native_checks(root, specs, *, owner=None):
    """Run each check or return its still-valid earlier receipt with provenance."""
    if (type(specs) is not list or len(specs) > 8
            or owner is not None and not isinstance(owner, _OptionalOwner)):
        return [{"ok": False, "error": "invalid_native_checks"}]
    results = []
    for raw in specs:
        try:
            results.append(_run_one(root, raw, owner))
        except (OSError, ValueError, TypeError, RecursionError):
            results.append({"ok": False, "error": "native_check_unavailable"})
    return results


def _run_one(root, raw, owner):
    path = _spec(raw, root)
    if owner is not None and not owner.authorized(path, raw):
        return {"ok": False, "error": "native_check_not_authorized"}
    agent, efficiency = _active()
    ident = "verify:" + uuid.uuid4().hex
    revision = efficiency.runtime.revision if efficiency else None
    source = _pin(raw)

    def capture():
        if _pin(raw) != source or _spec(raw, root) != path:
            return None, None
        if efficiency and (efficiency.runtime.closed or efficiency.runtime.revision != revision
                           or getattr(agent, "_interrupt_requested", False)):
            # Optional authority may expire; a main check is still normal work.
            return (None, None) if owner is not None else (_bytes(path), None)
        if owner is not None and not owner.authorized(path, raw):
            return None, None
        return _capture_contract(raw, path, ident, plugin_owned=owner is not None)

    data, check = capture()
    if data is None:
        return {"ok": False, "error": "native_check_stale"}
    def current():
        return capture()[1]
    reuse = None
    if efficiency and check and (owner is None or owner.reuse_allowed()):
        from agent.verification_evidence import propose_verification_reuse
        def reuse_current():
            return current() if owner is None or owner.reuse_allowed() else None
        reuse = propose_verification_reuse(check, current=reuse_current, exact=True)
    if isinstance(reuse, dict):
        return {"ok": True, "reused": True, "executed": False, "receipt": reuse}
    if owner is not None and not owner.authorized(path, raw):
        return {"ok": False, "error": "native_check_not_authorized"}
    # Recapture after semantic wait: a stale proposal cannot cause stale execution.
    data, latest = capture()
    if data is None:
        return {"ok": False, "error": "native_check_stale"}
    try:
        ok = _evaluate(data, raw["keys"])
    except (ValueError, UnicodeError, RecursionError):
        ok = False
    final_data, final_check = capture()
    if final_data != data or final_check != latest:
        ok = False  # the normal runner must not return a changed-state success
        latest = None
    elif check != latest:
        latest = None  # recaptured after a stale semantic decision; do not reuse
    from agent.verification_evidence import record_verify_run
    receipt = record_verify_run(root=root, session_id=getattr(agent, "session_id", None) or os.environ.get("HERMES_SESSION_ID"),
        ok=ok, command="hermes verify native-check", supplemental=True,
        output=json.dumps({"path": str(path), "keys": raw["keys"], "ok": ok}),
        supervision_check=latest)
    return {"ok": ok, "reused": False, "receipt": receipt}


class _OptionalOwner:
    """Host-owned authority, re-read on every dispatch/reuse boundary."""
    def __init__(self, facade, policy, revision):
        self.facade, self.policy, self.revision = facade, policy, revision
        self.registration = facade._registration

    def authorized(self, path, raw):
        facade = self.facade
        rt, reg = facade._active_runtime(), facade._registration
        from hermes_constants import hermes_home_key
        if (rt is None or reg is None or reg is not self.registration or not reg.active or rt.closed
                or rt.revision != self.revision or hermes_home_key() != reg.scope
                or rt.revision.profile != reg.scope
                or getattr(rt.agent(), "_interrupt_requested", False)):
            return False
        if _policy(facade) != self.policy:
            return False
        from agent.verify.environment import manifest_path
        manifest = _bytes(manifest_path(Path(self.policy["root"])))
        if hashlib.sha256(manifest).hexdigest() != self.policy["recipe_sha256"]:
            return False
        doc = json.loads(manifest)
        recipe = doc.get("recipe", doc) if isinstance(doc, dict) else {}
        return (isinstance(recipe, dict) and isinstance(recipe.get("nativeChecks"), list)
                and raw in recipe["nativeChecks"]
                and path == _spec(raw, Path(self.policy["root"]))
                and len(rt.action_requirements({"path": str(path)})) == 1)

    def reuse_allowed(self):
        return "reuse_receipt" in self.facade._registration.grants


def _policy(facade):
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly() or {}
    supervision = config.get("supervision", {})
    if not isinstance(supervision, dict) or supervision.get("enabled") is not True:
        return None
    entries = supervision.get("plugins", {})
    entry = entries.get(facade._context.plugin_id, {}) if isinstance(entries, dict) else {}
    policy = entry.get("optional_verification") if isinstance(entry, dict) else None
    if (type(policy) is not dict or set(policy) != {"version", "purpose", "root", "recipe_sha256"}
            or policy["version"] != VERSION or policy["purpose"] != "supplemental_confirmation"
            or type(policy["root"]) is not str or not Path(policy["root"]).is_absolute()
            or str(Path(policy["root"]).resolve()) != policy["root"]
            or type(policy["recipe_sha256"]) is not str or len(policy["recipe_sha256"]) != 64):
        return None
    return dict(policy)


def request_configured_checks(facade, *, session_id, changed_paths):
    rt = facade._active_runtime()
    if (rt is None or rt.closed or session_id != getattr(rt.agent(), "session_id", None)
            or not isinstance(changed_paths, (list, tuple)) or len(changed_paths) > 200
            or any(type(path) is not str or len(path) > 4096 for path in changed_paths)):
        return False
    with rt.lock:
        requests = rt._native_verification_requests
        if len(requests) >= 8:
            return False
        requests[facade._context.plugin_id] = (facade, rt.revision, session_id, tuple(changed_paths))
        rt._native_verification_requests = requests
    return True


def drain_configured_checks(agent):
    from agent.supervision_policy import runtime_for_agent
    rt = runtime_for_agent(agent)
    if rt is None:
        return
    rt._assert_owner()
    with rt.lock:
        requests = rt._native_verification_requests
        rt._native_verification_requests = {}
    for facade, revision, session_id, changed_paths in requests.values():
        if rt.revision == revision and not rt.closed:
            run_configured_checks(facade, session_id=session_id, changed_paths=changed_paths)


def run_configured_checks(facade, *, session_id, changed_paths):
    """Ordinary pre_verify hook: exact profile opt-in, changed source, native runner.

    No continuation, shell command or workspace-green receipt is produced. The
    plugin cannot use this as an arbitrary read API or declare mandatory work optional.
    """
    rt = facade._active_runtime()
    if rt is None or session_id != getattr(rt.agent(), "session_id", None):
        return None
    rt._assert_owner(tool_worker=True)
    policy = _policy(facade)
    if policy is None:
        return None
    from agent.verify.environment import manifest_path
    from agent.verify.recipes import Recipe
    from agent.verify.runner import run_verify
    try:
        root = Path(policy["root"])
        source = _bytes(manifest_path(root))
        if hashlib.sha256(source).hexdigest() != policy["recipe_sha256"]:
            return None
        doc = json.loads(source)
        recipe = Recipe.from_dict(doc.get("recipe", doc)) if isinstance(doc, dict) else None
        if recipe is None or type(recipe.native_checks) is not list or not 0 < len(recipe.native_checks) <= 8:
            return None
        paths = [_spec(raw, root) for raw in recipe.native_checks]
        owner = _OptionalOwner(facade, policy, rt.revision)
        if not any(str(path) in changed_paths for path in paths) or not all(owner.authorized(p, raw) for p, raw in zip(paths, recipe.native_checks)):
            return None
        return run_verify(root, recipe, native_only=True, supervision_owner=owner).to_dict()
    except (OSError, ValueError, TypeError, RecursionError):
        return None
