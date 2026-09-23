---
title: Public Subagent Lifecycle API
sidebar_label: Subagent lifecycle API
---

# Public Subagent Lifecycle API

Plugins can launch and supervise fresh Hermes child sessions without importing
`tools.delegate_tool`, gateway internals, TUI state, or `AIAgent` fields.
The service resolves its parent from the current agent turn, so it works in
CLI, gateway, non-interactive, and kanban-worker sessions. Launching outside an
active agent turn fails closed with `No active Hermes parent session`.

```python
from agent.subagent_lifecycle import SubagentLaunchRequest

def launch_review(ctx):
    # Call from a plugin tool or hook while an agent turn is active.
    service = ctx.subagent_lifecycle
    handle = service.launch(SubagentLaunchRequest(
        goal="Review this change for regressions.",
        context="Only inspect the supplied repository.",
        role="leaf",
        correlation_id="review-42",
        allowed_toolsets=("file",),
    ))
    # Persist handle.to_dict() if desired.
    if service.wait(handle, timeout_seconds=2).timed_out:
        return handle.to_dict()
    return service.result(handle)
```

`SubagentHandle` is serializable and carries a versioned, opaque capability.
Pass it back to `status`, `wait`, `cancel`, `result`, or `reconnect`; malformed
or forged handles return `UNKNOWN`/`UNKNOWN_HANDLE` and cannot access a child.

The stable states are `PENDING`, `STARTING`, `RUNNING`, `SUCCEEDED`, `FAILED`,
`INTERRUPTED`, `CANCEL_REQUESTED`, `CANCELLED`, and `UNKNOWN`.

`cancel(handle, reason=...)` is cooperative: it asks the child agent to
interrupt at its next safe boundary and returns `CANCEL_REQUESTED`; it never
claims completion until `wait` or `result` observes a terminal state. Terminal
results are immutable, idempotent, bounded to 32k characters, omit transcripts
and hidden reasoning, and include a stable result hash.

This API is lifecycle-managed asynchronous execution. Child construction and
completion use the same host-owned path as `delegate_task`, including parent
tool-resolution restoration, memory notification, serialized `subagent_stop`
hooks, resource cleanup, and child-cost rollup. It does not change the
synchronous `delegate_task` tool, batch delegation, or its gateway/TUI display.
The initial implementation retains metadata and terminal results in-process for
one hour.
After a process restart, `reconnect` returns `RECONNECT_UNAVAILABLE` and never
starts a replacement child. Running Python threads also cannot survive process
exit; callers must treat those handles as interrupted by process exit.

Requests are fail-closed: goal/context/metadata sizes are capped, unknown or
parent-broadening toolsets are rejected, and per-tool blocks, working-directory
overrides, and per-launch timeouts are explicitly rejected until Hermes can
support them without weakening isolation. Use `allowed_toolsets` to narrow a
child; Hermes's existing unsafe-tool block remains enforced.

## Owned read-only delegation accounting

Opted-in native delegation uses the existing canonical control row and CAS owner.
Optional semantic controls remain zero-wait and use their original deadline.
Authority reads and transaction admission/commit validate the SessionDB's recorded
main-file and required WAL/SHM identities without flushing accounting, repairing
storage, or adopting an unknown sidecar generation.

Mandatory dispatch exits and worker finish are separate from semantic authority.
Each admitted dispatch has a durable identity; the live owner retains the exact
exited identities when a zero-wait finalization transaction encounters SQLite
writer contention. Native finish, dispatch, status, or the host-only
`OwnedDelegationOwner.reconcile_lifecycle(handle)` retries this accounting with a
single nonwaiting CAS per ancestor. An exited dispatch is decremented only once;
worker finish alone never clears other live dispatches or nested handoffs.
Cleanup may finish after configuration/registration revocation, without restoring
permission for semantic actions. Completed tool results are not replaced by a
bookkeeping-contention error.

This is live-owner recovery, not crash recovery or an autonomous retry service.
If the final finish attempt collides, a later owner status/reconciliation call is
needed after the writer releases. Process loss loses the in-memory completion
facts, and database-generation loss, CAS conflict, or ambiguous storage failure
fences automatic reconciliation. The durable unsettled row remains unknown;
Hermes does not adopt work, replay tools, or fabricate settlement from it.
