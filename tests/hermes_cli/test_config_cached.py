"""Optional control authority never refreshes config under its execution fences."""
import json

import pytest

from hermes_cli import config
from hermes_cli.config_cached import current_config_readonly


@pytest.mark.parametrize('change', ['file', 'environment', 'managed', 'cold', 'malformed'])
def test_control_cache_requires_current_files_and_expansion(tmp_path, monkeypatch, change):
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('CONTROL_TEST_ROOT', str(home))
    managed = tmp_path / 'managed'
    managed.mkdir()
    monkeypatch.setenv('HERMES_MANAGED_DIR', str(managed))
    path = home / 'config.yaml'
    content = json.dumps({'terminal': {'cwd': '${CONTROL_TEST_ROOT}'}})
    path.write_text(content)
    assert current_config_readonly() is None  # no loader, mkdir or fallback
    first = config.load_config_readonly()
    assert current_config_readonly() is first
    if change == 'file':
        path.write_text(json.dumps({'terminal': {'cwd': str(tmp_path)}}))
    elif change == 'environment':
        monkeypatch.setenv('CONTROL_TEST_ROOT', str(tmp_path))
    elif change == 'managed':
        (managed / 'config.yaml').write_text(json.dumps({'terminal': {'cwd': str(tmp_path)}}))
    elif change == 'cold':
        monkeypatch.delitem(config._LOAD_CONFIG_CACHE, str(path))
    else:
        path.write_text('terminal: [broken')
        config.load_config_readonly()  # LKG cached under corrupt signature
    assert current_config_readonly() is None
    if change == 'malformed':
        path.write_text(content)
    # An ordinary source-owned reload outside the effect path restores freshness,
    # rather than caching the original permission forever or disabling forever.
    refreshed = config.load_config_readonly()
    assert current_config_readonly() is refreshed
    assert refreshed is not first
