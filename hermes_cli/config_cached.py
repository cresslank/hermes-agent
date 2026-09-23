"""Fresh-cache-only reads for optional control paths holding execution fences."""
import time
from typing import Any, cast


def current_config_readonly(*, deadline=None):
    """Return the canonical cache only while its files/expansions are current.

    A cold/changed/unreadable config or contended loader denies optional control;
    only an ordinary loader outside the control path may refresh it. In
    particular, last-known-good fallback after a parse failure is not a grant.
    Never wait for the config lock: its owner may be waiting for our caller.
    """
    from hermes_cli import config as c
    if deadline is not None and time.monotonic() >= deadline:
        return None
    if not c._CONFIG_LOCK.acquire(blocking=False):
        return None
    try:
        path = c.get_config_path()
        user_sig, sig = c._load_config_cache_sig(path)
        cached = cast(Any, c._LOAD_CONFIG_CACHE.get(str(path)))
        failure = c._CONFIG_PARSE_FAILURES.get(str(path))
        if (sig is None or cached is None or cached[:8] != sig
                or (failure is not None and failure[:4] == user_sig)
                or any(c._env_ref_lookup(k) != v for k, v in cached[9].items())
                or (deadline is not None and time.monotonic() >= deadline)):
            return None
        return cached[8]
    except OSError:
        return None
    finally:
        c._CONFIG_LOCK.release()
