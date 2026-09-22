"""Exact, bounded request-coverage windows from host-owned accepted clauses.

A whole candidate is required for negative coverage judgments: its prefix cannot
establish that an item is missing. Chunk requirements, never decisive candidate
qualifiers. This producer does not certify actions, truth or whole-request intent.
"""
from __future__ import annotations

import json
import re

from agent.supervision_context import digest, exact_span

_MAX_WINDOW = 1200
_MAX_CANDIDATE = 2 * _MAX_WINDOW
_MAX_ROWS = 8
_MAX_FACT_BYTES = 6500
_ACTION = re.compile(
    r"^\s*(?:[-+*]\s+(?:\[[ xX]\]\s+)?|\d+[.)]\s+)"
    r"(?:write|save|create|edit|patch|delete|remove|run|execute|test|verify|install|"
    r"restart|deploy|publish|push|commit|send|upload)\b", re.IGNORECASE)


def coverage_windows(requirements, sources, text):
    """Yield finite changed requirement subsets; all omitted IDs stay explicit.

    Only whole, unchanged authenticated source spans qualify. Imperative action
    candidates never acquire a verified receipt from the final answer's prose.
    Larger/opaque candidates abstain instead of silently evaluating a prefix.
    """
    if not requirements or not isinstance(text, str) or not 0 < len(text) <= _MAX_CANDIDATE:
        return
    source_ref = "final:" + digest(text)
    spans = []
    for start in range(0, len(text), _MAX_WINDOW):
        span = exact_span(source_ref, digest(text), text, start,
                          min(len(text), start + _MAX_WINDOW), kind="final_candidate")
        spans.append({"id": span.id, "text": span.text, "ref": span.id})
    rows = []
    all_ids = tuple(r.id for r in requirements)
    for requirement in requirements:
        source = sources.get(requirement.source_message_id)
        if (not isinstance(source, str) or digest(source) != requirement.source_revision
                or source[requirement.start:requirement.end] != requirement.text
                or not 0 < len(requirement.text) <= _MAX_WINDOW
                or len(requirement.heading) > _MAX_WINDOW):
            continue
        rows.append({"id": requirement.id, "text": requirement.text,
                     "source_ref": requirement.id, "source_exact": True,
                     "source_message_id": requirement.source_message_id,
                     "start": requirement.start, "end": requirement.end,
                     "heading": requirement.heading,
                     "active": True, "mandatory": True,
                     "kind": "action" if _ACTION.match(requirement.text) else "content",
                     "receipt_verified": False, "spans": spans})
    while rows:
        chosen = []
        for row in rows[:_MAX_ROWS]:
            candidate = _facts([*chosen, row], all_ids)
            # Leave room for primitive instructions and the repeated exact span
            # in a provider request. Its own exact UTF-8 request cap is authoritative.
            if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > _MAX_FACT_BYTES:
                break
            chosen.append(row)
        if not chosen:
            # An indivisible item is left pending; never truncate its qualifiers.
            rows.pop(0)
            continue
        yield _facts(chosen, all_ids)
        rows = rows[len(chosen):]


def _facts(rows, all_ids):
    selected = {row["id"] for row in rows}
    return {"requirements": rows, "coverage_scope": "enumerated_items",
            "global_coverage": "unknown", "continuation_available": True,
            "omitted_requirement_ids": [rid for rid in all_ids if rid not in selected]}
