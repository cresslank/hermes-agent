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

from agent.supervision_catalog import Catalog, SkillCandidate, authorized_tool_schemas, fingerprint, tool_id
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
        self.lock = threading.RLock()
        self.closed = False
        self.generation = 0
        self.cancel_status = None
        self.status = None
        self.dispatch = None
        self.catalog_seen = None

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
            self.generation += 1
            pending, self.pending = self.pending, {}
            self.catalog_seen = None
            self.views.reset(scope_key(self.runtime))
            self.views.defaults.update({q: replace(d, scope=self.views.scope) for q, d in defaults.items()})
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
                 revision=None, deadline=None, asynchronous=False, validate=None):
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
        spec = (future, expected, generation, action, revision, validate, deadline)
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
        snapshot = runtime.observe(event, facts, target_id=target, actions=(action,),
            evidence_refs=tuple(refs), deadline=deadline, owner='native_views',
            candidates=tuple(candidates), required_ids=tuple(required), relations=tuple(relations),
            completeness=Completeness('enumerated_items', True), data_class='task_text',
            required_obligations=tuple(required))
        if snapshot is None:
            with self.lock:
                self.pending.pop(target, None)
            return None
        if asynchronous:
            return future
        runtime._wait_for(target, deadline, expected)
        return self._settle(target, spec)

    def _settle(self, target, spec):
        _, expected, generation, action, revision, validate, deadline = spec
        runtime = self.runtime
        with runtime.lock:
            if (self.closed or generation != self.generation or expected != runtime.revision
                    or runtime.clock() >= deadline):
                return None
            entry = runtime._take(target, {action})
            if not entry:
                return None
            proposal, registration = entry
            with registration.fence:
                failure = runtime._validate(proposal, registration)
                answer = None if failure else validate(proposal)
                if answer is None:
                    runtime._settle(proposal, *(failure or ('rejected', 'view_contract')))
                else:
                    runtime.incidents.add(proposal.incident_id)
                    runtime._settle(proposal, 'applied', 'native_view')
                    answer = {'revision': revision, **answer}
                runtime.closed_targets.add(target)
                return answer

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
        rows = [{**b, 'text': b['excerpt'], 'neighbors_complete': True} for b in f['candidates']]
        ids = tuple(b['id'] for b in rows)
        facts = dict(question=task[1], source_ref=f['source_ref'], oversized=True, structured=True,
            source_immutable=True, critical_fields_complete=True, mandatory_ids=f['required_ids'],
            baseline_ids=ids, candidates=rows, omitted_count=0)
        return self._request('oversized_structured_result', facts, action=Action.SELECT_WINDOWS,
            refs=(f['source_ref'],), candidates=ids, required=f['required_ids'],
            revision=request['revision'], deadline=request['deadline'],
            validate=lambda p: self._selection(p, 'F16', ids, f['required_ids']))

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
                if p.feature_id == 'F18' and p.relation in ('progress_only', 'duplicate'):
                    return {'relation': p.relation}
            return self._request(request['domain'], facts, action=Action.SUPPRESS_STATUS, refs=(ref,),
                relations=('progress_only', 'duplicate'), revision=request['revision'],
                deadline=request['deadline'], asynchronous=True, validate=validate)
        if request['domain'] != 'clarification_proposed':
            return None
        default = self.views.defaults.get(f['question'])
        if not default or not default.interpretations:
            return None
        candidate = 'default:' + fingerprint((default.evidence_ref, default.value))
        facts = dict(user_ref=default.evidence_ref, slot=default.question, user_wording=default.source_text,
            interpretations=default.interpretations, permission_ui=False, secret_ui=False,
            authorization_missing=False, safe_default=True,
            resolution_candidates={'default_defined': {'id': candidate, 'ref': default.evidence_ref,
                'description': default.value, 'authorized': True}})
        def validate(p):
            if (p.feature_id == 'F19' and p.metadata.get('candidate_id') == candidate
                    and p.metadata.get('resolution') == 'default_defined'
                    and self.views.defaults.get(default.question) is default):
                return {'relation': 'use_authorized_default'}
        return self._request(request['domain'], facts, action=Action.CLARIFY_DEFAULT,
            refs=(default.evidence_ref,), revision=request['revision'], deadline=request['deadline'], validate=validate)

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
        if not isinstance(data, dict) or set(data) != {'output_format'}:
            return
        value = data['output_format']
        if value not in ('markdown', 'plain_text', 'json'):
            return
        question = 'Output format?'
        choices = ('markdown', 'plain_text', 'json')
        self.views.defaults[question] = AuthorizedDefault(question, value, self.views.scope,
            origin.message_id, low_stakes=True, authorized=True, source_text=raw,
            interpretations=tuple({'id': c, 'text': c,
                'consequence': 'Render the same answer content in ' + c} for c in choices))

    def prepare_catalogs(self):
        """One metadata ambiguity check per accepted scope, at request assembly."""
        if self.views.scope != scope_key(self.runtime):
            self.reset(preserve_defaults=True)
        if self.closed or self.catalog_seen == self.views.scope:
            return
        self.catalog_seen = self.views.scope
        task = self._task()
        agent = self.runtime.agent()
        if not task or agent is None:
            return
        ref, text = task
        words = _words(text)
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
        self._skills(ref, text, words)

    def _skills(self, ref, text, words):
        from tools.skills_tool import skills_list, skill_view
        try:
            rows = json.loads(skills_list()).get('skills', [])
            # Explicit names are mandatory matches: ordinary focused-skill rules win.
            if any(r['name'] in text for r in rows):
                return
            rows = [r for r in rows if words & _words(r.get('description', ''))]
            if not 2 <= len(rows) <= 8:
                return
            candidates: list[dict[str, Any]] = [{'id': r['name'], 'description': r['description'], 'covers': r['description'],
                'does_not_cover': 'Unknown: this catalog supplies no separate exclusions; inspect full content before use.',
                'authorized': True} for r in rows]
            facts = dict(task_ref=ref, task=text, rule_scope='Existing focused-skill rules; suggestions never load skills.',
                ambiguous=True, mandatory_match=False, mandatory_ids=[], stage='metadata', candidates=candidates)
            ids = tuple(r['id'] for r in candidates)
            result = self._request('catalog_ambiguity', facts, action=Action.SELECT_SKILLS, refs=(ref,), candidates=ids,
                validate=lambda p: self._selection(p, 'F12', ids))
            if not result or result['metadata'].get('phase') != 'shortlist':
                return
            details = []
            for selected in result['selected_ids']:
                # Native reader, no preprocessing/execution or usage bump. Full bodies
                # above the bounded contract stay unknown rather than fake-complete.
                detail = json.loads(skill_view(selected, preprocess=False))
                body = detail.get('content')
                if not detail.get('success') or not isinstance(body, str) or len(body) > 1200 or '[SKILL_PRUNED]' in body:
                    return
                details.append({**next(r for r in candidates if r['id'] == selected),
                    'content': body, 'excerpt_complete': True, 'pruned': False})
            if not 1 <= len(details) <= 3:
                return
            result = self._request('catalog_ambiguity', {**facts, 'stage': 'detail', 'candidates': details},
                action=Action.SELECT_SKILLS, refs=(ref,), candidates=tuple(r['id'] for r in details),
                validate=lambda p: self._selection(p, 'F12', tuple(r['id'] for r in details)))
            if result and result['metadata'].get('phase') == 'ready':
                revision = self.views.skills.catalog(tuple(SkillCandidate(r['id'], r['description']) for r in candidates), self.views.scope)
                self.views.skills.rank(tuple(result['selected_ids']), revision=revision, plugin_id='native_views', ambiguous=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            return
