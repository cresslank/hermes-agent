"""Disabling planning prose does not construct or wait on model opportunities."""
from types import SimpleNamespace

import pytest

from agent.supervision_planning import PlanningGraph


@pytest.mark.parametrize("setting", [False, None, "false", 0])
def test_disabled_planning_advisories_skip_loading_and_dispatch(monkeypatch, setting):
    monkeypatch.setattr("hermes_cli.config_cached.current_config_readonly",
                        lambda: {"supervision": {"planning_advisories": setting}})
    graph = PlanningGraph(SimpleNamespace(runtime=SimpleNamespace()))

    def forbidden():
        raise AssertionError("disabled planning must not load state or dispatch")

    monkeypatch.setattr(graph, "_load", forbidden)
    assert graph.advisory() is None
    assert not graph.requests


def test_legacy_planning_default_still_reaches_native_graph(monkeypatch):
    monkeypatch.setattr("hermes_cli.config_cached.current_config_readonly", lambda: {})
    graph = PlanningGraph(SimpleNamespace(runtime=SimpleNamespace()))
    loaded = []
    monkeypatch.setattr(graph, "_load", lambda: loaded.append(True))
    assert graph.advisory() is None
    assert loaded == [True]
