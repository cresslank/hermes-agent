"""Host-owned single-read fence for selected exact LCM history.

No reader callback, command, search arguments or new deadline is accepted. The
execution owner holds this lease only around its existing bounded local reader,
never around inference. Locks match effect settlement: revision then registration.
"""
from contextlib import contextmanager
from collections.abc import Mapping

from agent.supervision_types import Action


@contextmanager
def begin_history_expansion(facade, selection):
    runtime = facade._active_runtime()
    if (facade._context.plugin_id != "hermes-lcm" or runtime is None or
            runtime.revision.profile != facade._context._manager.scope_key or
            not isinstance(selection, Mapping)):
        yield False
        return
    runtime._assert_owner(tool_worker=True)
    with runtime.lock:
        receipt_id = selection.get("receipt_id")
        entry = runtime.owner_selections.get(receipt_id)
        if entry is None:
            yield False
            return
        proposal, registration, ids = entry
        if (proposal.owner != "lcm" or proposal.action != Action.EXPAND_ONE_OWNED_REF or
                proposal.target_id != "lcm:" + str(selection.get("request_id")) or
                tuple(selection.get("candidate_ids", ())) != ids or
                len(ids) != 1 or selection.get("candidate_id") != ids[0] or
                receipt_id in runtime.owner_reads):
            yield False
            return
        with registration.fence:
            failure = runtime._validate(proposal, registration, acknowledging=True)
            if failure:
                runtime.owner_selections.pop(receipt_id)
                runtime._settle(proposal, *failure)
                yield False
                return
            runtime.owner_reads.add(receipt_id)
            # Registration.close and authenticated instruction ingress cannot
            # cross this local-read boundary. Post-read validation still owns
            # source bytes, offsets, scope, currentness and original deadline.
            yield True
