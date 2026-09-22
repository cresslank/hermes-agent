"""Isolated real-facade fixtures shared by supervision suites."""
import json
import threading
from types import SimpleNamespace

import pytest

from agent.interrupt_control import InterruptControlMixin
from agent.turn_explainers import TurnExplainersMixin
from agent.supervision_context import accepted_input_origin, accepted_origin_scope
from agent.supervision_policy import runtime_for_agent
from agent.supervision_types import Action
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from tools.todo_tool import TodoStore


class Agent(InterruptControlMixin, TurnExplainersMixin):
    def __init__(self):
        self.session_id = "session-A"
        self.platform = "cli"
        self._execution_thread_id = threading.get_ident()
        self._interrupt_requested = False
        self._pending_steer = None
        self._pending_steer_lock = threading.Lock()
        self._todo_store = TodoStore()
        self._turn_failed_file_mutations = {}
        self._turn_file_mutation_paths = set()
        self.iteration_budget = SimpleNamespace(remaining=5)

    def _conversation_root_id(self):
        return self.session_id


def accept(agent, text, *, message_id=None, continuation=False):
    origin = accepted_input_origin(text, kind="cli", message_id=message_id, continuation=continuation)
    cli = SimpleNamespace(conversation_history=[])
    with accepted_origin_scope(origin):
        CLIChatTurnMixin._chat_stage_user_message(cli, agent, text)
    return runtime_for_agent(agent)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config = {"supervision": {"enabled": True, "plugins": {"fixture-supervisor": {
        "grants": ["observe", *(a.value for a in Action)],
        "data_policy": ["task_text", "project_excerpt", "public_source"]}}}}
    (home / "config.yaml").write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(home))
    manifest = PluginManifest(name="fixture-supervisor")
    facade = PluginContext(manifest, manager).supervision
    events = []
    registration = facade.register(consumer=events.append, requested_grants=["observe", *(a.value for a in Action)])
    agent = Agent()
    from hermes_state import SessionDB
    db = agent._session_db = SessionDB(home / "state.db")
    agent._session_db.create_session(agent.session_id, "cli")
    yield SimpleNamespace(agent=agent, facade=facade, events=events, registration=registration,
                          home=home, manager=manager, config=config)
    facade.unregister()
    db.close()


def proposal(rig, event, *, action="advise", template: str | None = "review_action", incident="incident", refs=None, **overrides):
    facts = event["facts"]
    if refs is None:
        refs = facts.get("requirement_refs") or [r["id"] for r in facts.get("requirements", [])][:1]
    data = dict(proposal_id="proposal-" + incident, plugin_generation=rig.registration["generation"],
                feature_id="F09", incident_id=incident, expected=event["revision"], owner="agent",
                target_id=facts["target_id"], action=action, evidence_refs=refs,
                expires_at_monotonic=event["deadline"], template_id=template)
    return {**data, **overrides}


def commit_plan(rig, path, *, conflict=False):
    text = f"- Write `{path}`.\n- Check `target.py` and `next.py`."
    runtime = accept(rig.agent, text)
    from agent.subagent_lifecycle import bind_subagent_parent
    from tools.todo_tool import todo_tool
    with bind_subagent_parent(rig.agent):
        plan_result = json.loads(todo_tool([{"id": "step", "content": "Update `target.py` or `next.py`",
            "status": "in_progress"}], store=rig.agent._todo_store))
    source = plan_result["work_map_sources"]["requirements"][1]
    ref = {key: source[key] for key in ("source_message_id", "start", "end")}
    block = {"version": 1, "requirements": [ref],
             "steps": [{"todo_id": "step", "requirement_indexes": [0], "target_refs": ["target.py"]}], "claims": []}
    content = "# Plan\n```hermes-work-map-v1\n" + json.dumps(block) + "\n```\n"
    from tools.file_tools import write_file_tool
    result = write_file_tool(str(path), content)
    rig.agent._record_file_mutation_result("write_file", {"path": str(path), "content": content}, result, False)
    if conflict:
        from agent.subagent_lifecycle import bind_subagent_parent
        from tools.file_tools import patch_tool
        with bind_subagent_parent(rig.agent):
            changed = patch_tool(path=str(path), old_string='"target_refs": ["target.py"]',
                                 new_string='"target_refs": ["next.py"]')
        assert not json.loads(changed).get("error")
    return runtime, content, result
