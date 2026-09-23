"""Lossless native control projection and mandatory-delivery markers.

This module has no stop or acceptance authority. The lifecycle owner arbitrates
completion versus cancellation; presentation must never infer stopped from status.
"""
from collections.abc import Mapping


def attach_supervisor_control(entry, child):
    from agent import owned_delegation
    # Older hosts and unowned children have no attribution. Never adopt a worker
    # from persisted text or from a child-authored result field.
    project = getattr(owned_delegation, "supervisor_control_for_child", None)
    control = project(child) if callable(project) else None
    if control is not None:
        entry["supervisor_control"] = control
    return entry


def protected_delivery(value):
    """Control, failure and cleanup information may not be semantically parked.

    Only inspect native result structure, not arbitrary text. A marker can only
    restrict optional suppression; it cannot grant authority or attest a result.
    """
    if not isinstance(value, Mapping):
        return False
    if ("supervisor_control" in value or value.get("error")
            or value.get("status") in {"failed", "error", "interrupted", "cancelled", "timeout", "unknown", "stalled"}
            or any(value.get(k) for k in ("cleanup_pending", "handed_off_processes", "orphaned_processes",
                                         "unread_completions", "safety", "approval", "required_obligation"))
            or value.get("obligation_state") in {"open", "outstanding", "required"}):
        return True
    worktree = value.get("worktree")
    if worktree:  # worktree preservation/cleanup contracts are not optional prose
        return True
    return any(protected_delivery(item) for item in (value.get("results") or ()) if isinstance(item, Mapping))


def supervisor_control_lines(entry):
    control = entry.get("supervisor_control")
    if not isinstance(control, Mapping):
        return []
    # Keep the finite native state verbatim. requested/signal_failed/already_finished
    # must never be rendered as a confirmed cancellation.
    fields = ("actor", "feature_id", "action", "reason_code", "state", "decision_id",
              "original_input_ref", "current_input_ref", "obligation_ref", "obligation_state", "replacement_ref")
    details = "; ".join(f"{key}={control[key]}" for key in fields if control.get(key) is not None)
    lines = ["[supervisor_control] " + details]
    if control.get("partial_refs"):
        lines.append("Preserved partial references: " + ", ".join(map(str, control["partial_refs"])))
    if control.get("obligation_state") in {"open", "outstanding", "required"}:
        lines.append("Current-target work remains outstanding; stopping this worker does not complete the obligation.")
    return lines
