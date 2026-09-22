"""Exact request-local visibility; no semantic scanning or historical truth claims."""
from __future__ import annotations
import re


def capture_visibility(agent, messages):
    from agent.supervision_policy import runtime_for_agent
    runtime = runtime_for_agent(agent)
    if runtime is None:
        return
    runtime._assert_owner()
    texts, size = [], 0
    for message in messages:
        if message.get("role") == "system":
            continue
        content = message.get("content")
        parts = [content] if isinstance(content, str) else content
        if parts is None:
            continue
        if not isinstance(parts, list):
            runtime.history_visibility = None
            return
        for part in parts:
            if isinstance(part, dict):
                if part.get("type") not in {"text", "input_text", "output_text"}:
                    continue
                part = part.get("text")
            if not isinstance(part, str):
                runtime.history_visibility = None
                return
            size += len(part)
            if size > 1_000_000:
                runtime.history_visibility = None
                return
            texts.append(part)
    runtime.history_visibility = (runtime.revision, tuple(texts))


def source_absent(runtime, ref, excerpt):
    visibility = getattr(runtime, "history_visibility", None)
    if (visibility is None or visibility[0] != runtime.revision or runtime.closed or
            not isinstance(ref, str) or not re.fullmatch(r"lcm:[1-9][0-9]*:[0-9]+-[0-9]+", ref) or
            not isinstance(excerpt, str) or not 0 < len(excerpt) <= 1200):
        return None
    return not any(ref in text or excerpt in text for text in visibility[1])
