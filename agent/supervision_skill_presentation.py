"""Complete, read-only skill snapshots at the new request-message boundary.

Selection owns future presentation, never old rows or the system prefix. Once a
snapshot was sent, retain its exact suffix while its anchor remains in context;
retiring a selection cannot rewrite that cached prefix. Canonical history stays
untouched. Compression may remove anchors as it removes other request context.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from agent.supervision_catalog import fingerprint

MAX_SKILL_CHARS = 65536


@dataclass(frozen=True)
class SkillContent:
    name: str
    content: str
    source: str


def read_skill_content(name, *, description=None, max_chars=MAX_SKILL_CHARS):
    """Use the ordinary local resolver gates, without setup or shell rendering.

    Plugin-qualified and dynamically rendered skills remain on skill_view. No
    secret lookup, readiness action, environment registration or tool runs here.
    An oversized or pruned skill is ineligible, never a partial instruction.
    """
    from tools.skills_tool import (
        _is_skill_disabled, _locate_skill, _read_skill_text, _safe_frontmatter,
        _skill_lookup_path_error, _skill_search_dirs, skill_matches_platform,
    )
    if not isinstance(name, str) or ':' in name or _skill_lookup_path_error(name):
        return None
    try:
        project_dirs, search_dirs, _ = _skill_search_dirs()
        error, _, path = _locate_skill(name, None, project_dirs, search_dirs)
        if error or path is None or path.stat().st_size > max_chars * 4:
            return None
        body = _read_skill_text(path)
        metadata = _safe_frontmatter(content=body)
        if (not isinstance(body, str) or not 0 < len(body) <= max_chars
                or '[SKILL_PRUNED]' in body or '!`' in body or '${HERMES_' in body
                or (description is not None and metadata.get('description') != description)
                or not skill_matches_platform(metadata)
                or _is_skill_disabled(metadata.get('name', name))):
            return None
        return SkillContent(name, body, str(path))
    except (OSError, ValueError, TypeError):
        return None


class SkillPresentation:
    def __init__(self):
        self.seen = set()
        self.suffixes = {}

    def apply(self, messages, contents):
        counts, keys = Counter(), []
        for row in messages:
            digest = fingerprint(row)
            keys.append((digest, counts[digest]))
            counts[digest] += 1
        live = set(keys)
        self.suffixes = {k: v for k, v in self.suffixes.items() if k in live}
        result = [dict(row) if key in self.suffixes else row for row, key in zip(messages, keys)]
        for row, key in zip(result, keys):
            if key in self.suffixes:
                row['content'] += self.suffixes[key]
        # Only the actual new trailing message is eligible. Never scan backward
        # to amend an earlier user instruction after a tool round or retry.
        if (keys and keys[-1] not in self.seen and messages[-1].get('role') in ('user', 'tool')
                and isinstance(messages[-1].get('content'), str)):
            visible = [r.get('content', '') for r in result if isinstance(r.get('content'), str)]
            additions = []
            for skill in contents:
                if not skill.content or any(skill.content in text for text in visible):
                    continue
                additions.append('\n\n[Native skill content: ' + skill.name + '\nSource: ' + skill.source
                    + '\nComplete read-only snapshot; existing focused-skill and explicit instructions still apply.\n'
                    + skill.content + '\nEnd native skill content]')
                visible.append(skill.content)
            if additions:
                suffix = ''.join(additions)
                self.suffixes[keys[-1]] = suffix
                result[-1] = {**result[-1], 'content': result[-1]['content'] + suffix}
        self.seen = live
        return result
