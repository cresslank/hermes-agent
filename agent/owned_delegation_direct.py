"""Versioned decisive-stop lifecycle policy and bounded native input identities.

A replacement worker is not a completed obligation. These rules grant neither
execution nor result acceptance, and are never inferred from task prose.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

VERSION = "supervision.direct-control.v2"
CONTRACT = "supervision.child-relevance.v2"
POLICY = dict(version=VERSION, enabled=True, interval_seconds=2,
              cancellation_rule="decisive-stop.v1", choice_min=.80, confidence_min=.80)
RELATIONS = frozenset({"current", "superseded", "duplicate", "no_remaining_consumer", "insufficient"})


def enabled(policy):
    value = policy.get("direct_control") if isinstance(policy, dict) else None
    return type(value) is dict and value == POLICY and all(type(value[k]) is type(v) for k, v in POLICY.items())


def input_identity(roots, *, budget=.5, max_files=4096, max_bytes=64 * 1024 * 1024):
    """Hash actual local content, including dirty/untracked changes at the same HEAD.

    Git supplies an ignore-aware bounded candidate census, not freshness evidence.
    Non-repositories use a bounded walk. Symlinks, races, oversized inputs and
    timeouts are unknown; no inferred partial fingerprint is ever authoritative.
    No shell, network, file contents or absolute paths reach the provider.
    """
    deadline = time.monotonic() + budget
    def census(root):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        # Bound stdout on disk, not in an unbounded PIPE. A non-git directory
        # falls back to the same file/count/time bounded local census.
        if (root / ".git").exists():
            import tempfile
            with tempfile.TemporaryFile() as out:
                result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                    stdout=out, stderr=subprocess.DEVNULL, timeout=remaining, check=False)
                if result.returncode:
                    raise OSError("input census unavailable")
                if out.tell() > 1024 * 1024:
                    raise ValueError("input census oversized")
                out.seek(0)
                names = set(os.fsdecode(x) for x in out.read().split(b"\0") if x)
                if len(names) > max_files:
                    raise ValueError("input census oversized")
                return sorted(names)
        names = []
        for base, dirs, files in os.walk(root, followlinks=False):
            if time.monotonic() >= deadline:
                raise TimeoutError
            dirs[:] = sorted(d for d in dirs if d != ".git")
            if any((Path(base) / d).is_symlink() for d in dirs):
                raise ValueError("symlink input")
            names.extend(str((Path(base) / f).relative_to(root)) for f in files)
            if len(names) > max_files:
                raise ValueError("input census oversized")
        return sorted(names)
    try:
        digest, total, count = hashlib.sha256(), 0, 0
        for raw in roots:
            root = Path(raw).resolve(strict=True)
            if not root.is_dir():
                return None
            names = census(root)
            digest.update(str(root).encode())
            if (root / ".git").exists():
                result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    timeout=max(.001, deadline - time.monotonic()), check=False)
                if result.returncode or len(result.stdout) > 128:
                    return None
                digest.update(result.stdout)
            for name in names:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                path = root / name
                if path.is_symlink() or root not in path.resolve().parents:
                    return None
                try:
                    before = path.stat()
                except FileNotFoundError:
                    digest.update(json.dumps([name, "deleted"]).encode())
                    continue
                if not stat.S_ISREG(before.st_mode):
                    return None
                total += before.st_size
                count += 1
                if total > max_bytes or count > max_files:
                    return None
                digest.update(json.dumps([name, before.st_mode]).encode())
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        digest.update(chunk)
                after = path.stat()
                fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
                if any(getattr(after, key) != getattr(before, key) for key in fields):
                    return None
            if census(root) != names:
                return None
        return "input:sha256:" + digest.hexdigest()
    except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
        return None


def control_projection(snapshot):
    record = snapshot.get("supervisor_control")
    if not isinstance(record, dict):
        return None
    result = {k: record.get(k) for k in ("actor", "feature_id", "action", "reason_code", "decision_id", "state")}
    result.update(original_input_ref=snapshot.get("original_input_ref"), current_input_ref=snapshot.get("current_input_ref"),
                  obligation_ref=snapshot.get("obligation_ref"), obligation_state=snapshot.get("obligation_state", "unknown"),
                  partial_refs=list(snapshot.get("partial_refs", ()))[:8],
                  worker_finished=snapshot.get("worker_finished", False),
                  processes_stopped=snapshot.get("processes_stopped", False),
                  effects_reconciled=snapshot.get("effects_reconciled", False))
    return result
