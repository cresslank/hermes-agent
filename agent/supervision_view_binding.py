"""Native, provider-neutral request-view owners.

Observations go through the registered supervision worker and native proposal queue.
Only execution owners or the bound presentation dispatcher settle proposals. No
provider imports, inferred disclosure grants, user turns, or inference-thread UI.
"""
from __future__ import annotations

from concurrent.futures import Future
import json
import re
import threading
import uuid
import weakref
from typing import Any

from agent.supervision_catalog import Catalog, SkillCandidate, SkillHint, authorized_tool_schemas, fingerprint, tool_id
from agent.supervision_types import Action, Completeness, project
from agent.supervision_views import AuthorizedDefault, SupervisionViews

_bindings = weakref.WeakSet()
_bindings_lock = threading.Lock()
_STOP = frozenset('the and for with from this that using please into have your'.split())
_CONTROLS = ('approval', 'safety', 'failure', 'cleanup', 'requested_update', 'correction',
             'changed_commitment', 'side_effect', 'required_result')


def _words(text):
    return set(re.findall(r'[a-z][a-z0-9_]{3,}', text.lower())) - _STOP


def scope_key(runtime):
    return fingerprint(project(runtime.revision))


def bind_presentation_loop(agent, loop):
    """Called by CLI/gateway/TUI with their existing UI loop; never creates one."""
    if loop is None or loop.is_closed():
        agent._supervision_presentation_callbacks = (None, None)
    else:
        from agent.notification_presentation import asyncio_owner_callbacks
        agent._supervision_presentation_callbacks = asyncio_owner_callbacks(loop)
    binding = getattr(agent, '_supervision_view_binding', None)
    if isinstance(binding, NativeViewsBinding):
        binding.bind_status()


def attach_views(runtime):
    agent = runtime.agent()
    if agent is None:
        return None
    old = getattr(agent, '_supervision_view_binding', None)
    if isinstance(old, NativeViewsBinding):
        old.close()
    binding = NativeViewsBinding(runtime)
    agent._supervision_view_binding = binding
    agent._supervision_views = binding.views
    binding.bind_status()
    with _bindings_lock:
        _bindings.add(binding)
    return binding


def reset_views(runtime, *, preserve_defaults=False):
    binding = getattr(runtime.agent(), '_supervision_view_binding', None)
    if isinstance(binding, NativeViewsBinding):
        binding.reset(preserve_defaults=preserve_defaults)


def close_views(runtime):
    binding = getattr(runtime.agent(), '_supervision_view_binding', None)
    if isinstance(binding, NativeViewsBinding):
        binding.close()


def registration_closed(profile):
    # Revoke pending UI closures on any provider generation change. Another
    # still-authorized provider can participate in a subsequent native request.
    with _bindings_lock:
        bindings = tuple(_bindings)
    for binding in bindings:
        if binding.runtime.revision.profile == profile:
            binding.reset()


def proposal_queued(runtime, target):
    binding = getattr(runtime.agent(), '_supervision_view_binding', None)
    if isinstance(binding, NativeViewsBinding):
        binding.notify(target)


class NativeViewsBinding:
    def __init__(self, runtime):
        self.runtime = runtime
        self.views = SupervisionViews(self, scope=scope_key(runtime), clock=runtime.clock,
                                      deadline_provider=runtime.shared_deadline)
        self.pending = {}
        self.details = {}
        self.detail_requests = {}
        self.skill_hints = {}
        self.local_skill_contents = ()
        self.local_skill_route = False
        self.skill_removals = {}
        self.skill_plan_origin = None
        # Skill eligibility belongs to the accepted task/phase, not per-tool
        # evidence refresh. Keep the last attempt (including a retired hint)
        # until a relevant task, native phase or catalog event reopens it.
        self.skill_scope = self._skill_task_scope()
        self.skill_phase = None
        self.skill_catalog_seen = None
        self.lock = threading.RLock()
        self.closed = False
        self.generation = 0
        self.cancel_status = None
        self.status = None
        self.dispatch = None
        self.catalog_seen = None
        self.clarification_slot = None

    def bind_status(self):
        from agent.notification_presentation import bind_status_owner
        callbacks = getattr(self.runtime.agent(), '_supervision_presentation_callbacks', (None, None))
        self.dispatch, later = callbacks
        self.cancel_status = bind_status_owner(self.runtime.agent(), self.views,
            profile_key=self.runtime.revision.profile, dispatch=self.dispatch, call_later=later)
        self.status = self.runtime.agent()._status_coalescer

    def reset(self, *, preserve_defaults=False):
        from dataclasses import replace
        with self.lock:
            defaults = dict(self.views.defaults) if preserve_defaults else {}
            slot = self.clarification_slot if preserve_defaults else None
            self.generation += 1
            pending, self.pending = self.pending, {}
            details, self.detail_requests = self.detail_requests, {}
            self.details.clear()
            skills = self.views.skills
            keep_skills = not self.closed and self.skill_scope == self._skill_task_scope()
            if keep_skills:
                # Registration revocation removes only that exact owner's hint.
                # An evidence reset must not erase another owner's suggestion.
                for plugin_id, (hint, _, _, registration) in tuple(self.skill_hints.items()):
                    if not registration.active:
                        if skills.owned_hints.get(plugin_id) is hint:
                            skills.owned_hints.pop(plugin_id)
                            if skills.hints.get(plugin_id) == hint.text:
                                skills.hints.pop(plugin_id)
                        self.skill_hints.pop(plugin_id)
            else:
                self.skill_hints.clear()
                self.local_skill_contents = ()
                self.local_skill_route = False
                self.skill_phase = None
                self.skill_catalog_seen = None
            self.skill_scope = self._skill_task_scope()
            self.skill_removals.clear()
            if self.skill_plan_origin and self.skill_plan_origin[0] != self._skill_task_scope():
                self.skill_plan_origin = None
            self.catalog_seen = None
            self.views.reset(scope_key(self.runtime))
            if keep_skills:
                self.views.skills = skills
            self.views.defaults.update({q: replace(d, scope=self.views.scope) for q, d in defaults.items()})
            self.clarification_slot = {**slot, 'scope': self.views.scope} if slot else None
        if self.status is not None:
            # Revoke old closures synchronously; cleanup can run after new events.
            cleanup = self.status.detach()
            if callable(self.dispatch):
                try:
                    self.dispatch(cleanup)
                except RuntimeError:  # closed loop; no rendering occurs in cleanup
                    cleanup()
            else:
                cleanup()
        for _, _, future in details.values():
            if not future.done():
                future.set_result(None)
        for future, *_ in pending.values():
            if not future.done():
                future.set_result(None)

    def close(self):
        self.closed = True
        self.reset()

    def _task(self):
        # Actual accepted source, never arbitrary model/tool text.
        if not self.runtime.sources:
            return None
        ref, text = next(reversed(self.runtime.sources.items()))
        return (ref, text) if text and len(text) <= 1200 else None

    def _request(self, event, facts, *, action, refs, candidates=(), required=(), relations=(),
                 revision=None, deadline=None, asynchronous=False, validate=None, skill_removal=None, recipient=None):
        runtime = self.runtime
        if self.closed or runtime.closed or not refs:
            return None
        if not asynchronous:
            runtime._assert_owner(tool_worker=True)
        expected = runtime.revision
        if self.views.scope != scope_key(runtime):
            self.reset(preserve_defaults=True)
        target = 'view:' + uuid.uuid4().hex
        deadline = runtime.shared_deadline(deadline)
        generation = self.generation
        future = Future()
        actions = tuple(action) if isinstance(action, tuple) else (action,)
        spec = (future, expected, generation, actions, revision, validate, deadline)
        if asynchronous:
            if not callable(self.dispatch):
                return None
            with self.lock:
                if len(self.pending) >= 32:
                    return None
                self.pending[target] = spec
            def forget(_):
                with self.lock:
                    self.pending.pop(target, None)
            future.add_done_callback(forget)
        if skill_removal is not None:
            self.skill_removals[target] = skill_removal
        if event == 'catalog_ambiguity':
            with self.lock:
                self.details[target] = {'facts': facts, 'expected': expected, 'deadline': deadline,
                                        'issued': runtime.decision_issued_at,
                                        'generation': generation, 'event_id': None}
        snapshot = runtime.observe(event, facts, target_id=target, actions=actions,
            evidence_refs=tuple(refs), deadline=deadline, owner='native_views',
            candidates=tuple(candidates), required_ids=tuple(required), relations=tuple(relations),
            completeness=Completeness('enumerated_items', True), data_class='task_text',
            required_obligations=tuple(required), recipient=recipient)
        if snapshot is None:
            with self.lock:
                self.pending.pop(target, None)
                self.details.pop(target, None)
                self.skill_removals.pop(target, None)
            return None
        if asynchronous:
            return future
        try:
            with self.lock:
                if target in self.details:
                    self.details[target]['event_id'] = snapshot.event_id
            # Dependent metadata acquisition is serviced by this existing owner,
            # not by an inference thread or a new filesystem executor.
            while runtime.clock() < deadline and runtime.revision == expected:
                with runtime.ready:
                    runtime.ready.wait_for(lambda: runtime.closed or runtime.revision != expected or
                        target in self.detail_requests or any(p.target_id == target for p, _ in runtime.pending),
                        timeout=max(0, deadline - runtime.clock()))
                with self.lock:
                    detail_job = self.detail_requests.pop(target, None)
                if detail_job is None:
                    break
                request, registration, detail_future = detail_job
                if detail_future.set_running_or_notify_cancel():
                    reply = self._read_skill_details(request, registration)
                    detail_future.set_result(reply)
            return self._settle(target, spec)
        finally:
            with self.lock:
                self.details.pop(target, None)
                self.skill_removals.pop(target, None)
                job = self.detail_requests.pop(target, None)
            if job is not None and not job[2].done():
                job[2].set_result(None)

    async def acquire_skill_details(self, request, registration):
        """Queue one dependent local read to the waiting native execution owner."""
        import asyncio
        import copy
        target = request.get('target_id')
        if not isinstance(target, str):
            return None
        future = Future()
        with self.runtime.ready:
            with self.lock:
                context = self.details.get(target)
                if (context is None or context.get('requested') or self.closed or
                        not registration.active or not {'observe', 'select_skills'} <= registration.grants or
                        'task_text' not in registration.data_policy or
                        context['expected'] != self.runtime.revision or
                        self.runtime.clock() >= context['deadline']):
                    return None
                context['requested'] = True
                self.detail_requests[target] = (copy.deepcopy(request), registration, future)
            self.runtime.ready.notify_all()
        return await asyncio.wrap_future(future)

    def _read_skill_details(self, request, registration):
        from agent.supervision_skill_presentation import read_skill_content
        runtime = self.runtime
        runtime._assert_owner(tool_worker=True)
        target = request['target_id']
        with self.lock:
            context = self.details.get(target)
        required_keys = {'version', 'event_id', 'owner', 'incident_id', 'target_id', 'expected',
                         'selected_ids', 'task_ref', 'data_policy', 'deadline', 'deadline_issued_at'}
        if (context is None or set(request) != required_keys or not registration.active or
                request['version'] != 'supervision.skill-details.v1' or request['owner'] != 'native_views' or
                request['event_id'] != context['event_id'] or request['incident_id'] != context['event_id'] or
                request['expected'] != project(context['expected']) or context['expected'] != runtime.revision or
                request['task_ref'] != context['facts']['task_ref'] or
                request['deadline'] != context['deadline'] or runtime.clock() >= context['deadline'] or
                request['deadline_issued_at'] != context['issued']):
            return None
        keys = set(context['facts']) | {'target_id'}
        policy = registration.egress_policy
        if not policy or not keys <= policy['fields'].keys():
            return None
        expected_policy = {**project(policy), 'fields': {k: policy['fields'][k] for k in keys},
                           'sources': {k: policy['sources'][k] for k in keys if k in policy['sources']}}
        if request['data_policy'] != expected_policy:
            return None
        selected = request['selected_ids']
        rows = {row['id']: row for row in context['facts']['candidates']}
        if (not isinstance(selected, list) or not 1 <= len(selected) <= 3 or
                any(not isinstance(i, str) or i not in rows for i in selected) or len(set(selected)) != len(selected)):
            return None
        details, snapshots = [], {}
        for selected_id in selected:
            with self.lock:
                if (self.details.get(target) is not context or self.closed or runtime.closed or
                        context['expected'] != runtime.revision or not registration.active or
                        runtime.clock() >= context['deadline']):
                    return None
            snapshot = read_skill_content(selected_id, description=rows[selected_id]['description'])
            if snapshot is None:
                return None
            snapshots[selected_id] = snapshot
            from tools.skills_tool import _parse_frontmatter
            metadata, instructions = _parse_frontmatter(snapshot.content)
            complete = len(snapshot.content) <= 700
            details.append({**rows[selected_id],
                'full_description': metadata.get('description') or rows[selected_id]['description'],
                'content': snapshot.content if complete else instructions[:700],
                'excerpt_complete': complete, 'pruned': False,
                'local_content': {'version': 'supervision.local-skill.v1',
                    'sha256': fingerprint(snapshot.content), 'chars': len(snapshot.content)}})
        with self.lock:
            if (self.details.get(target) is not context or context['expected'] != runtime.revision or
                    not registration.active or runtime.clock() >= context['deadline']):
                return None
            context['served_ids'] = tuple(selected)
            context['served_content'] = {name: snapshot.content for name, snapshot in snapshots.items()}
            context['local_snapshots'] = snapshots
            context['served_registration'] = registration
        return {**request, 'candidates': details}

    def _settle(self, target, spec):
        _, expected, generation, actions, revision, validate, deadline = spec
        runtime = self.runtime
        with runtime.lock:
            if (self.closed or generation != self.generation or expected != runtime.revision
                    or runtime.clock() >= deadline):
                return None
            entry = runtime._take(target, set(actions))
            if not entry:
                return None
            proposal, registration = entry
            with registration.fence:
                failure = runtime._validate(proposal, registration)
                if (not failure and proposal.feature_id == 'F12' and
                        proposal.metadata.get('feature_action') == 'rank_skill_ids'):
                    context = self.details.get(target, {})
                    served = context.get('served_ids', ())
                    if (proposal.metadata.get('phase') != 'ready' or not served or
                            context.get('served_registration') is not registration or
                            not set(proposal.candidate_ids) <= set(served)):
                        failure = ('rejected', 'skill_details_required')
                answer = None if failure else validate(proposal)
                if not failure and runtime.clock() >= deadline:
                    failure, answer = ('expired', 'view_deadline'), None
                skill_state = None
                if not failure and answer is not None and proposal.feature_id == 'F12':
                    skills = self.views.skills
                    skill_state = (skills.ranked_ids, dict(skills.hints), dict(skills.owned_hints),
                                   dict(self.skill_hints))
                    answer = self._apply_skill_proposal(proposal, registration, revision, answer, generation)
                if answer is None:
                    runtime._settle(proposal, *(failure or ('rejected', 'view_contract')))
                else:
                    receipt = runtime._settle(proposal, 'applied', 'native_view')
                    if receipt.status == 'applied':
                        runtime.incidents.add(proposal.incident_id)
                        answer = {'revision': revision, **answer}
                    else:
                        # Request-local hints are reversible. A failed durable
                        # settlement cannot retire or publish one as applied.
                        if skill_state is not None:
                            (skills.ranked_ids, skills.hints, skills.owned_hints,
                             self.skill_hints) = skill_state
                        answer = None
                runtime.closed_targets.add(target)
                return answer

    def _apply_skill_proposal(self, proposal, registration, revision, answer, generation):
        """Called under the runtime + registration fences, before an applied receipt."""
        with self.lock:
            if self.closed or self.generation != generation:
                return None
            skills = self.views.skills
            if (revision != skills.revision or not isinstance(skills.scope, str)
                    or self.views.scope != scope_key(self.runtime)):
                return None
            if proposal.metadata.get('feature_action') == 'rank_skill_ids':
                task = self._task()
                if not task or proposal.evidence_refs != (task[0],):
                    return None
                ids = tuple(answer['selected_ids'])
                from agent.supervision_skill_presentation import read_skill_content
                snapshots = self.details[proposal.target_id].get('local_snapshots', {})
                for name in ids:
                    if name in snapshots and read_skill_content(name) != snapshots[name]:
                        return None  # changed/disabled/relocated bodies are not the voted snapshot
                if self.runtime.clock() >= self.details[proposal.target_id]['deadline']:
                    return None
                if not skills.rank(ids, revision=revision, plugin_id=registration.plugin_id, ambiguous=True):
                    return None
                hint = SkillHint('hint:' + uuid.uuid4().hex, registration.plugin_id, registration.generation,
                    skills.scope, revision, ids[0], skills.hints[registration.plugin_id],
                    content=self.details[proposal.target_id]['served_content'][ids[0]],
                    mandatory=any(c.required and c.id == ids[0] for c in skills.candidates))
                # Bind the whole enumerated native phase, not a guessed skill/step link.
                # Unknown/empty/oversized plans still permit a hint, never its removal.
                plan = self._skill_plan()
                if self.skill_plan_origin != (self._skill_task_scope(), plan):
                    plan = None
                skills.owned_hints[hint.plugin_id] = hint
                self.skill_hints[registration.plugin_id] = (hint, task, plan, registration)
                return answer
            removal = self.skill_removals.get(proposal.target_id)
            if removal is None:
                return None
            entry, finished = removal
            hint, task, initial, owner = entry
            if (registration is not owner or registration.generation != hint.registration
                    or self.skill_hints.get(hint.plugin_id) is not entry
                    or self._task() != task or self._skill_plan() != finished
                    or not self._phase_finished(initial, finished)
                    or proposal.feature_id != 'F12' or proposal.action != Action.SELECT_SKILLS
                    or proposal.candidate_ids or proposal.evidence_refs != (task[0],)
                    or project(proposal.metadata) != {'feature_action': 'remove_own_hint', 'hint_id': hint.id}):
                return None
            if not skills.remove_own_hint(registration.plugin_id, hint=hint, registration=registration.generation):
                return None
            self.skill_hints.pop(hint.plugin_id)
            return answer

    def _skill_task_scope(self):
        r = self.runtime.revision
        return (r.profile, r.lineage, r.work_id, r.run_generation, r.instruction_event, r.requirements)

    def _skill_plan(self):
        from tools.todo_tool import TodoStore
        store = getattr(self.runtime.agent(), '_todo_store', None)
        if not isinstance(store, TodoStore):
            return None
        plan = store.snapshot()
        if not plan['todos'] or len(plan['todos']) > 8 or len(json.dumps(plan)) > 1200:
            return None
        return plan

    @staticmethod
    def _phase_finished(initial, finished):
        if not initial or not finished or finished['revision'] <= initial['revision']:
            return False
        before, after = initial['todos'], finished['todos']
        # Deletion, cancellation, rewriting or adding a step is not completion.
        return (any(t['status'] in ('pending', 'in_progress') for t in before)
                and all(t['status'] in ('pending', 'in_progress', 'completed') for t in before)
                and all(t['status'] == 'completed' for t in after)
                and [{k: v for k, v in t.items() if k != 'status'} for t in before]
                    == [{k: v for k, v in t.items() if k != 'status'} for t in after])

    @staticmethod
    def _phase_reopened(previous, current):
        before, after = previous['todos'], current['todos']
        # IDs/order cannot establish a new phase, especially after completion:
        # lost row lineage is not evidence of unfinished work. Only a positively
        # new/reopened eligible declaration can reopen selection. Count repeated
        # declarations without treating pending -> in_progress as new work.
        from collections import Counter
        def eligible(rows):
            return Counter(t['content'] for t in rows if t['status'] in ('pending', 'in_progress'))
        return bool(eligible(after) - eligible(before))

    def skill_phase_committed(self, messages):
        """A committed todo phase transition, not an every-turn semantic patrol.

        Only a ready optional suggestion bound to an enumerated native plan is
        eligible. Completion describes that plan, never global task completion.
        No loaded bodies, requirements, safety instructions or history are owned
        by this hint; their preservation does not depend on the semantic answer.
        """
        from agent.turn_iteration_prep import _previous_tool_round
        if self.closed:
            return
        expected = self.runtime.revision
        batch = _previous_tool_round(messages)
        finished = self._skill_plan()
        if not finished or any(t.get('result') is None for t in batch):
            return
        committed = False
        for call in batch:
            if call.get('name') != 'todo_list':
                continue
            try:
                result = json.loads(call['result'])
                args = json.loads(call['arguments']) if isinstance(call['arguments'], str) else call['arguments']
            except (TypeError, ValueError):
                continue
            if (isinstance(args, dict) and args.get('todos') is not None and isinstance(result, dict)
                    and all(result.get(k) == v for k, v in finished.items())):
                committed = True
        if not committed:
            return
        with self.runtime.lock:
            if self.closed or self.runtime.revision != expected:
                return
            previous = self.skill_plan_origin
            if (previous is None or previous[0] != self._skill_task_scope()
                    or self._phase_reopened(previous[1], finished)):
                self.skill_phase = fingerprint(finished)
            self.skill_plan_origin = (self._skill_task_scope(), finished)
        if not self.skill_hints or self.views.scope != scope_key(self.runtime):
            return
        for entry in tuple(self.skill_hints.values()):
            hint, task, initial, registration = entry
            if (not registration.active or not {'observe', 'select_skills'} <= registration.grants
                    or self._task() != task or not self.views.skills.owns_hint(hint)
                    or not self._phase_finished(initial, finished)):
                continue
            facts = dict(task_ref=task[0], task=task[1], stage='unload',
                rule_scope='Existing focused-skill rules remain mandatory; remove only the optional suggestion.',
                hint={'id': hint.id, 'skill_id': hint.skill_id, 'selected_content': hint.content,
                      'phase': finished, 'plugin_owned': True, 'mandatory': hint.mandatory,
                      'safety_or_cleanup': hint.safety_or_cleanup, 'phase_ended': True,
                      'unfinished_step': 'Native enumerated plan transitioned to all completed. '
                          'This is not proof of global task completion. Retain the suggestion if the task still needs it.'})
            self._request('task_revision', facts, action=Action.SELECT_SKILLS, refs=(task[0],),
                revision=hint.catalog_revision, skill_removal=(entry, finished), recipient=registration,
                validate=lambda p: {} if (p.feature_id == 'F12' and p.action == Action.SELECT_SKILLS
                    and p.metadata.get('feature_action') == 'remove_own_hint') else None)

    def release_status_future(self, future):
        with self.lock:
            for target, spec in tuple(self.pending.items()):
                if spec[0] is future:
                    self.pending.pop(target, None)

    def notify(self, target):
        with self.lock:
            spec = self.pending.get(target)
        if spec is None or not callable(self.dispatch):
            return
        def settle_on_owner():
            with self.lock:
                current = self.pending.pop(target, None)
            if current is spec:
                result = self._settle(target, spec)
                if not spec[0].done():
                    spec[0].set_result(result)
        self.dispatch(settle_on_owner)

    @staticmethod
    def _selection(proposal, feature, known, required=()):
        ids = proposal.candidate_ids
        feature_action = {'F12': 'rank_skill_ids', 'F13': 'apply_canonical_tool_view',
                          'F16': 'select_exact_spans_preserve_receipts'}[feature]
        if (proposal.feature_id != feature or proposal.metadata.get('feature_action') != feature_action
                or not ids or not set(ids) <= set(known)
                or not set(required) <= set(ids)):
            return None
        return {'selected_ids': list(ids), 'metadata': project(proposal.metadata)}

    def select_windows(self, request):
        f = request['facts']
        task = self._task()
        if not task or not f.get('source_ref'):
            return None
        source = self.views.result_source(f['source_ref'], scope=request['scope'], revision=request['revision'])
        if source is None or request['scope'] != scope_key(self.runtime):
            return None
        rows = [{**{k: v for k, v in b.items() if k != 'excerpt'},
                 'text': b['excerpt'], 'neighbors_complete': True} for b in f['candidates']]
        ids = tuple(b['id'] for b in rows)
        facts = dict(question=task[1], source_ref=f['source_ref'], oversized=True, structured=True,
            source_immutable=True, critical_fields_complete=True, mandatory_ids=f['required_ids'],
            baseline_ids=ids, candidates=rows, omitted_count=0)
        def validate(p):
            if (self.views.result_source(f['source_ref'], scope=request['scope'], revision=request['revision']) is not source or
                    p.evidence_refs != (f['source_ref'],) or p.metadata.get('source_ref') != f['source_ref']):
                return None
            return self._selection(p, 'F16', ids, f['required_ids'])
        return self._request('oversized_structured_result', facts, action=Action.SELECT_WINDOWS,
            refs=(f['source_ref'],), candidates=ids, required=f['required_ids'],
            revision=request['revision'], deadline=request['deadline'], validate=validate)

    def rank_candidates(self, request):
        return None  # Evidence ranking belongs to the evidence owner, not this adapter.

    def evaluate_relation(self, request):
        f = request['facts']
        if request['domain'] == 'filterable_notification_proposed':
            if not f.get('optional') or not f.get('complete'):
                return None
            old, new = f['previous_content'], f['content']
            # _emit_status_kind uses (kind, text); do not lose channel identity.
            if isinstance(old, tuple) and isinstance(new, tuple) and len(old) == len(new) == 2 and old[0] == new[0]:
                old, new = old[1], new[1]
            if not isinstance(old, str) or not isinstance(new, str) or old == new:
                return None
            ref = 'notification:' + fingerprint((request['scope'], old, new))
            facts = dict(notification_ref=ref, subject_id=f['subject'], update=new, previous=old,
                changed_fields=['content'], optional=True, filterable=True, observation_complete=True,
                obligations_clear=True, duplicate=False, known_progress_repeat=False,
                mandatory_controls={k: False for k in _CONTROLS})
            def validate(p):
                if (p.feature_id != 'F18' or p.metadata.get('subject_id') != f['subject'] or
                        p.metadata.get('original_channel_only') is not True or p.metadata.get('preserve_activity') is not True):
                    return None
                if (p.action == Action.SUPPRESS_STATUS and p.metadata.get('feature_action') == 'suppress_optional'
                        and p.relation in ('progress_only', 'duplicate')):
                    return {'relation': p.relation}
                if (p.action == Action.PRESENT_STATUS and p.metadata.get('feature_action') == 'present_material_once'
                        and p.relation in ('material_outcome', 'actionable_change')):
                    # The retained original closure runs once on its original channel.
                    return {'relation': p.relation}
            return self._request(request['domain'], facts, action=(Action.SUPPRESS_STATUS, Action.PRESENT_STATUS), refs=(ref,),
                relations=('progress_only', 'duplicate', 'material_outcome', 'actionable_change'), revision=request['revision'],
                deadline=request['deadline'], asynchronous=True, validate=validate)
        return None

    @staticmethod
    def _format_source(text):
        match = re.fullmatch(r'```hermes-defaults-v1\s*\n(.*?)\n```', text.strip(), re.S)
        if match is None:
            return None
        try:
            value = json.loads(match.group(1))
        except ValueError:
            return None
        if isinstance(value, dict) and set(value) == {'output_format'} and value['output_format'] in ('markdown', 'plain_text', 'json'):
            return value['output_format']
        return None

    def clarify_slot(self, question, choices, *, multi_select=False):
        """Resolve finite ordinary questions from current accepted instructions.

        Keep the exact local format/default route; otherwise Jev selects an exact
        offered identity in one semantic decision. No generated answer, permission
        grant, generic retrieval executor, or second main-model selector.
        """
        slot = self.clarification_slot
        if slot is None or question != 'Output format?':
            with self.runtime.decision_boundary():
                return self._semantic_clarification(question, choices, multi_select=multi_select)
        if (slot['scope'] != self.views.scope or multi_select or
                question != 'Output format?' or not isinstance(choices, list) or
                len(choices) != 3 or any(type(c) is not str for c in choices) or
                set(choices) != {'markdown', 'plain_text', 'json'}):
            return None
        if slot['mode'] != 'retrieve':
            return None  # ask means the existing UI, never an invented answer
        source = self.runtime.sources.get(slot['source_ref'])
        if source and fingerprint(source) == slot['source_pin']:
            value = self._format_source(source)
            if value in choices:
                return {'question': question, 'choices_offered': choices, 'user_response': '',
                        'resolution': 'retrieved', 'resolved_value': value, 'evidence_ref': slot['source_ref']}
        return None

    def clarification_identity(self):
        with self.runtime.lock:
            # A lazy view-scope refresh is not a change to the accepted context.
            return (self.runtime.revision, tuple(self.runtime.sources.items()))

    def _semantic_clarification(self, question, choices, *, multi_select=False):
        if (self.closed or self.runtime.closed or multi_select
                or not isinstance(question, str) or not 0 < len(question) <= 1200
                or not isinstance(choices, list) or not 2 <= len(choices) <= 4
                or any(type(c) is not str or not 0 < len(c) <= 1200 for c in choices)
                or len(set(choices)) != len(choices)):
            return None
        # Secret/approval tools own those flows, not clarify's semantic shortcut.
        # This lexical deny is only a conservative mechanical floor; Jev also
        # classifies nonliteral permission/commitment/material boundaries.
        protected = re.compile(
            r'\b(password|passcode|otp|2fa|mfa|cvc|cvv|secret|token|api[ _-]?key|'
            r'verification[ -]code|one[ -]time[ -]code|approv\w*|authoriz\w*|permission|'
            r'consent|confirm\w*|pay\w*|purchas\w*|buy|credit[ -]card)\b', re.I)
        if protected.search(' '.join([question, *choices])):
            return None
        runtime = self.runtime
        with runtime.lock:
            expected = runtime.revision
            sources = tuple(runtime.sources.items())
            if (not sources or runtime.completeness.omitted
                    or any(not isinstance(t, str) or not 0 < len(t) <= 1200 for _, t in sources)
                    or sum(len(t) for _, t in sources) > 6000
                    or any(re.search(r'\b(password|passcode|otp|2fa|mfa|cvc|cvv|secret|token|api[ _-]?key|'
                                     r'verification[ -]code|one[ -]time[ -]code)\b', t, re.I)
                           for _, t in sources)):
                return None
        offered = tuple(choices)
        refs = tuple(ref for ref, _ in sources)
        rows = [{'id': f'c{i}', 'text': text} for i, text in enumerate(offered)]
        by_id = {r['id']: r['text'] for r in rows}
        facts = dict(clarification_contract='supervision.clarification.v1', slot=question,
            accepted_history=[{'ref': ref, 'text': text} for ref, text in sources],
            interpretations=rows, permission_ui=False, secret_ui=False, authorization_missing=False)
        revision = fingerprint((project(expected), question, offered, sources))
        def current():
            return (runtime.revision == expected and tuple(runtime.sources.items()) == sources
                    and tuple(choices) == offered and not self.closed and not runtime.closed)
        def validate(proposal):
            meta = project(proposal.metadata)
            ids = proposal.candidate_ids
            if (not current() or proposal.feature_id != 'F19'
                    or proposal.evidence_refs != refs or len(ids) != 1 or ids[0] not in by_id
                    or meta != {'feature_action': 'use_authorized_default', 'selected_ids': list(ids),
                                'resolution': meta.get('resolution')}
                    or meta.get('resolution') not in {'already_stated', 'default_defined'}):
                return None
            return {'question': question, 'choices_offered': list(offered), 'user_response': '',
                    'resolution': 'already_stated' if meta['resolution'] == 'already_stated' else 'safe_default',
                    'resolved_value': by_id[ids[0]], 'evidence_refs': list(refs)}
        result = self._request('clarification_proposed', facts, action=Action.CLARIFY_DEFAULT,
            refs=refs, candidates=tuple(by_id), revision=revision, validate=validate)
        # _request validates under the registration/runtime fences. Recheck the
        # exact source and offered identities at the return boundary as well.
        if not isinstance(result, dict) or not current():
            return None
        result.pop('revision', None)
        return result

    def accept_defaults(self, origin):
        """Explicit user-only presentation contract; tool arguments cannot add defaults.

        Syntax: a hermes-defaults-v1 JSON fence with output_format=markdown|plain_text|json.
        This deliberately does not authorize action/target/permission/secret defaults.
        """
        from agent.supervision_context import is_accepted_origin
        if not is_accepted_origin(origin):
            return
        match = re.fullmatch(r'```hermes-defaults-v1\s*\n(.*?)\n```', origin.text.strip(), re.S)
        if match is None:
            return  # embedded/quoted/negated examples cannot grant a default
        raw = match.group(1)
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        slot = {'scope': self.views.scope, 'user_ref': origin.message_id, 'wording': raw}
        if set(data) == {'output_format_ref'} and isinstance(data['output_format_ref'], str):
            source_ref = data['output_format_ref']
            source = self.runtime.sources.get(source_ref)
            if source and self._format_source(source) is not None:
                self.clarification_slot = {**slot, 'mode': 'retrieve', 'source_ref': source_ref,
                                           'source_pin': fingerprint(source)}
            return
        if set(data) != {'output_format'}:
            return
        value = data['output_format']
        if value == 'ask':
            self.clarification_slot = {**slot, 'mode': 'ask'}
            return
        if value not in ('markdown', 'plain_text', 'json'):
            return
        question = 'Output format?'
        choices = ('markdown', 'plain_text', 'json')
        self.views.defaults[question] = AuthorizedDefault(question, value, self.views.scope,
            origin.message_id, low_stakes=True, authorized=True, source_text=raw,
            interpretations=tuple({'id': c, 'text': c,
                'consequence': 'Render the same answer content in ' + c} for c in choices))

    def prepare_catalogs(self):
        """One whole-roster skill selection per accepted scope at request assembly."""
        if self.views.scope != scope_key(self.runtime):
            self.reset(preserve_defaults=True)
        if self.closed:
            return
        task = self._task()
        agent = self.runtime.agent()
        if self.runtime.sources:
            self._local_skills(next(reversed(self.runtime.sources.values())))
        if not task or agent is None:
            return
        ref, text = task
        words = _words(text)
        if self.catalog_seen != self.views.scope:
            self.catalog_seen = self.views.scope
            self._tools(ref, text, words, agent)
        self._skills(ref, text)

    def _tools(self, ref, text, words, agent):
        try:
            schemas = authorized_tool_schemas(agent)
            required = tuple(tool_id(s) for s in schemas if tool_id(s) in re.findall(r'\b[\w_]+\b', text))
            required = tuple(sorted(set(required) | set(getattr(agent, '_supervision_required_tools', ()))))
            catalog = Catalog.tools(schemas, required_ids=required,
                                    rules_revision=getattr(agent, '_supervision_rules_revision', ''))
            self.views.required_tools = required
            explicit = bool(required)
            matches = [s for s in schemas if tool_id(s) not in catalog.required_ids and
                       words & _words(s.get('function', s).get('description', ''))]
            if catalog.discoverable and not explicit and 2 <= len(matches) <= 8:
                rows: list[dict[str, Any]] = [{'id': tool_id(s), 'description': s.get('function', s).get('description', ''),
                    'authorized': True, 'available': True, 'schema_hash': fingerprint(s)} for s in matches]
                if all(isinstance(r['description'], str) and 0 < len(r['description']) <= 1200 for r in rows):
                    facts = dict(operation_ref=ref, operation=text, contract=text, ambiguous=True,
                        explicit_tool=False, deterministic_route=False, candidates=rows,
                        mandatory_ids=sorted(catalog.required_ids),
                        discovery_ids=[i for i in catalog.ids if i in {'tool_search','hermes_tool_search','tool_describe','tool_call'}])
                    result = self._request('tool_capability_ambiguity', facts, action=Action.SELECT_TOOLS,
                        refs=(ref,), candidates=catalog.ids, revision=catalog.revision,
                        validate=lambda p: self._selection(p, 'F13', catalog.ids))
                    if result:
                        self.views.propose_tools(tuple(result['selected_ids']), catalog_revision=catalog.revision,
                                                 scope=self.views.scope, include_deferred=True)
        except (ValueError, TypeError, AttributeError):
            pass

    def _local_skills(self, text):
        from agent.supervision_skill_presentation import read_skill_content
        # Only an unambiguous literal instruction is a local explicit route.
        # Mentioning/quoting/negating a name is not permission to auto-load it.
        match = re.fullmatch(r'(?:Use|Load) (?:the )?skill (?:`([\w-]+)`|([\w-]+))[.!]?', text.strip())
        required = [c.id for c in self.views.skills.candidates if c.required]
        if match:
            required.append(match[1] or match[2])
        self.local_skill_route = bool(required)
        if not required:
            self.local_skill_contents = ()
            return
        from agent.supervision_skill_presentation import MAX_SKILL_CHARS
        names = tuple(dict.fromkeys(required))
        contents = [read_skill_content(name) for name in names] if len(names) <= 8 else []
        # Preserve all mandatory routes: partial admission is not permission to
        # choose a convenient subset. Ordinary skill_view remains available.
        complete = contents and all(contents) and sum(len(c.content) for c in contents) <= MAX_SKILL_CHARS
        self.local_skill_contents = tuple(contents) if complete else ()

    def skill_contents(self):
        from agent.supervision_skill_presentation import SkillContent
        if self.closed:
            return ()
        if self.local_skill_route:
            return self.local_skill_contents
        contents = []
        for hint, _, _, registration in self.skill_hints.values():
            if (registration.active and self.views.skills.owns_hint(hint)
                    and {'observe', 'select_skills'} <= registration.grants):
                contents.append(SkillContent(hint.skill_id, hint.content, 'native skill catalog: ' + hint.skill_id))
        # Focused-skill contract admits one optional match, not one per provider.
        return tuple(contents[:1])

    def _skills(self, ref, text):
        from tools.skills_tool import skills_list
        from agent.skill_utils import extract_skill_description
        # A semantic optional match cannot replace an explicit/mandatory route,
        # including a mandatory body that was too large or otherwise ineligible.
        if self.local_skill_route or any(c.required for c in self.views.skills.candidates):
            return
        try:
            rows = json.loads(skills_list()).get('skills', [])
            scope = fingerprint((self.skill_scope, self.skill_phase, self.runtime.revision.catalog))
            catalog_key = (scope, fingerprint(rows))
            if self.skill_catalog_seen == catalog_key:
                return
            self.skill_catalog_seen = catalog_key
            # Nonliteral explicit routes (including negated/quoted instructions)
            # stay with ordinary skill handling; mere name substrings are not routes.
            if re.search(r'\b(?:use|load)\s+(?:the\s+)?skill\b', text, re.I):
                return
            if any(re.search(r'\b(?:use|load)\s+(?:the\s+)?`?' + re.escape(r['name']) + r'`?(?![\w-])', text, re.I) for r in rows):
                return
            # Every available entry participates: no lexical prefilter or 2..8
            # eligibility gate. The provider rejects unsupported sizes as a whole.
            if not rows:
                return
            candidates: list[dict[str, Any]] = [{'id': r['name'],
                'description': extract_skill_description({'description': r['description']}),
                'authorized': True} for r in rows]
            facts = dict(task_ref=ref, task=text, rule_scope='Existing focused-skill rules and explicit user instructions take precedence; native request assembly supplies the selected complete content.',
                ambiguous=True, mandatory_match=False, mandatory_ids=[], stage='catalog', candidates=candidates)
            ids = tuple(r['id'] for r in candidates)
            revision = self.views.skills.catalog(tuple(SkillCandidate(r['id'], r['description']) for r in candidates), scope)
            self._request('catalog_ambiguity', facts, action=Action.SELECT_SKILLS, refs=(ref,), candidates=ids,
                revision=revision, validate=lambda p: self._selection(p, 'F12', ids))
        except (ValueError, TypeError, KeyError, AttributeError):
            return
