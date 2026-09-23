"""Exact request-local visibility; no semantic scanning or historical truth claims."""
from __future__ import annotations
import json
import re

_EXACT = re.compile(r"(?<![\w:])lcm:[1-9][0-9]*:[0-9]+-[0-9]+(?![\w:-])")
_HIDDEN = {"reasoning", "reasoning_content", "thinking", "redacted_thinking", "encrypted_content"}


def capture_visibility(agent, messages):
    from agent.supervision_policy import runtime_for_agent
    runtime = runtime_for_agent(agent)
    if runtime is None:
        return
    runtime._assert_owner()
    texts, refs, size, nodes = [], set(), 0, 0
    # Bound container traversal as well as characters; failure means UNKNOWN,
    # never source absence. Tool arguments can be JSON strings or typed objects.
    revision = runtime.revision
    stack = [(messages, 0, False)]
    while stack:
        value, depth, payload = stack.pop()
        nodes += 1
        if nodes > 20_000 or depth > 64:
            runtime.history_visibility = None
            return
        if isinstance(value, str):
            size += len(value)
            if size > 1_000_000:
                runtime.history_visibility = None
                return
            texts.append(value)
            refs.update(_EXACT.findall(value))
            if payload and value.lstrip().startswith(("{", "[")):
                # Model-visible arguments may JSON-escape an exact handle or
                # excerpt. Inspect the decoded data under the same budgets.
                try:
                    decoded = json.loads(value)
                except json.JSONDecodeError:
                    pass  # Non-JSON tool output remains literal visible text.
                except (ValueError, RecursionError):
                    runtime.history_visibility = None
                    return
                else:
                    stack.append((decoded, depth + 1, True))
        elif isinstance(value, (list, tuple)):
            if len(stack) + len(value) > 20_000:
                runtime.history_visibility = None
                return
            stack.extend((item, depth + 1, payload) for item in value)
        elif isinstance(value, dict):
            if not payload and value.get("type") in _HIDDEN:
                continue
            if len(stack) + len(value) > 20_000:
                runtime.history_visibility = None
                return
            # A tool input named "reasoning" is still visible user data, not
            # the assistant's private reasoning field.
            keys = ("content", "tool_calls", "function_call", "arguments", "input",
                    "output", "text", "name") if depth == 1 else value
            stack.extend((value[k], depth + 1, payload or k in {"arguments", "input", "output"})
                         for k in keys if k in value and (payload or k not in _HIDDEN))
        elif value is not None and not isinstance(value, (bool, int, float)):
            runtime.history_visibility = None
            return
    with runtime.lock:
        runtime.history_visibility = ((revision, tuple(texts), frozenset(refs))
                                      if revision == runtime.revision else None)


def source_visibility(runtime, ref, excerpt):
    visibility = getattr(runtime, "history_visibility", None)
    if (visibility is None or visibility[0] != runtime.revision or runtime.closed or
            not isinstance(ref, str) or not re.fullmatch(r"lcm:[1-9][0-9]*:[0-9]+-[0-9]+", ref) or
            not isinstance(excerpt, str) or not 0 < len(excerpt) <= 1200):
        return None
    return {"explicit_ref_available": ref in visibility[2],
            "source_text_visible": any(excerpt in text for text in visibility[1])}


def source_absent(runtime, ref, excerpt):
    state = source_visibility(runtime, ref, excerpt)
    return None if state is None else not any(state.values())
