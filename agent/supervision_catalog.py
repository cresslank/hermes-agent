"""Local authorized catalogs. Selection changes exposure, never dispatcher authority.

Callers supply the already permission-filtered schemas and scoped skill metadata;
this module never enumerates the global registry or reads configuration/credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any

# These pins cannot be removed by a proposal. Additional policy pins are supplied
# by the host owner, not by the decision service.
DISCOVERY_PINS = frozenset({"tool_search", "hermes_tool_search", "tool_describe", "tool_call"})
SAFETY_PINS = frozenset({"clarify", "read_file", "skill_view", "skills_list", "process_manage"})


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def tool_id(schema: dict) -> str:
    fn = schema.get("function", schema)
    return fn.get("name", "") if isinstance(fn, dict) else ""


@dataclass(frozen=True)
class Catalog:
    revision: str
    ids: tuple[str, ...]
    schema_hashes: tuple[str, ...]
    required_ids: frozenset[str]
    discoverable: bool

    @classmethod
    def tools(cls, schemas: list[dict], *, required_ids=(), rules_revision="") -> "Catalog":
        ids = tuple(tool_id(s) for s in schemas)
        if any(not n for n in ids) or len(set(ids)) != len(ids):
            raise ValueError("noncanonical authorized tool catalog")
        # A discovery bridge cannot invoke non-deferrable core/surface tools.
        # Keep those ambient rather than exposing an unreachable escape route.
        from tools.tool_search import scoped_deferrable_names
        rediscoverable = scoped_deferrable_names(schemas)
        pins = frozenset(n for n in ids if n not in rediscoverable or n in SAFETY_PINS | DISCOVERY_PINS
                         or n.startswith("browser_vault_") or n.startswith("approval_"))
        pins |= frozenset(required_ids)
        if not pins.issubset(ids):
            raise ValueError("missing required tool")
        hashes = tuple(fingerprint(s) for s in schemas)
        return cls(fingerprint((ids, hashes, sorted(pins), rules_revision)), ids, hashes, pins,
                   bool({"tool_describe", "tool_call"}.issubset(ids)
                        and {"tool_search", "hermes_tool_search"}.intersection(ids)))

    def select(self, schemas: list[dict], ids: tuple[str, ...], revision: str) -> list[dict]:
        """Return original definitions verbatim, including unknown provider fields."""
        if (revision != self.revision or not isinstance(ids, (tuple, list)) or not ids
                or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids) or not set(ids).issubset(self.ids)):
            return schemas
        chosen = list(ids) + [n for n in self.ids if n in self.required_ids and n not in ids]
        # Without an escape route rank, but never hide an authorized capability.
        if not self.discoverable:
            chosen += [n for n in self.ids if n not in chosen]
        originals = dict(zip(self.ids, schemas))
        return [originals[n] for n in chosen]


def authorized_tool_schemas(agent):
    """Read the same scoped local catalog as tool_describe; never query connectors.

    Deferred definitions are eligible only after the native describe validator
    confirms them. This is a metadata read, not a permission/registry mutation.
    Call only on an explicit catalog-ambiguity event or a pending selection.
    """
    from model_tools import get_tool_definitions
    from tools.tool_search import dispatch_tool_describe, scoped_deferrable_names
    baseline = agent.tools
    current = get_tool_definitions(enabled_toolsets=agent.enabled_toolsets,
        disabled_toolsets=agent.disabled_toolsets, quiet_mode=True,
        skip_tool_search_assembly=True) or []
    allowed = scoped_deferrable_names(current)
    seen = {tool_id(s) for s in baseline}
    extras = [s for s in current if tool_id(s) in allowed and tool_id(s) not in seen
              and not tool_id(s).startswith("connectors_")]
    if not extras:
        return baseline
    # Native describe has a bounded per-call cap; metadata batches never use RPC.
    confirmed = set()
    for start in range(0, len(extras), 8):
        reply = json.loads(dispatch_tool_describe(
            {"names": [tool_id(s) for s in extras[start:start + 8]]}, current_tool_defs=current,
            connector_describe=lambda *a, **kw: {}))
        confirmed.update(reply.get("tools", {}))
    return list(baseline) + [s for s in extras if tool_id(s) in confirmed]


@dataclass(frozen=True)
class SkillCandidate:
    id: str
    description: str
    required: bool = False


@dataclass(frozen=True)
class SkillHint:
    """Exact ownership of an optional suggestion, never of loaded skill content."""
    id: str
    plugin_id: str
    registration: str
    scope: str
    catalog_revision: str
    skill_id: str
    text: str
    content: str = ""
    mandatory: bool = False
    must_keep: bool = False
    safety_or_cleanup: bool = False


class SkillView:
    """Own suggestions and read-only detail snapshots, never loaded history."""
    def __init__(self):
        self.scope = None
        self.revision = ""
        self.candidates = ()
        self.ranked_ids = ()
        self.hints: dict[str, str] = {}
        self.owned_hints: dict[str, SkillHint] = {}

    def catalog(self, candidates: tuple[SkillCandidate, ...], scope: str):
        if len({c.id for c in candidates}) != len(candidates):
            raise ValueError("duplicate skill ID")
        revision = fingerprint((scope, [(c.id, c.description, c.required) for c in candidates]))
        if revision != self.revision:
            self.hints.clear()
            self.owned_hints.clear()
            self.ranked_ids = tuple(c.id for c in candidates)
        self.scope, self.revision, self.candidates = scope, revision, candidates
        return revision

    def rank(self, ids: tuple[str, ...], *, revision: str, plugin_id: str, ambiguous=False) -> bool:
        known = {c.id for c in self.candidates}
        if (revision != self.revision or not isinstance(ids, (tuple, list)) or not ids
                or any(not isinstance(i, str) for i in ids)
                or len(set(ids)) != len(ids) or not set(ids) <= known):
            return False
        mandatory = tuple(c.id for c in self.candidates if c.required)
        self.ranked_ids = mandatory + tuple(i for i in ids if i not in mandatory) + tuple(
            c.id for c in self.candidates if c.id not in ids and c.id not in mandatory)
        self.owned_hints.pop(plugin_id, None)
        if ambiguous:
            self.hints[plugin_id] = "Optional skill candidate: " + ids[0] + ". Apply existing focused-skill rules."
        else:
            self.hints.pop(plugin_id, None)
        return True

    def owns_hint(self, hint: SkillHint) -> bool:
        return (self.owned_hints.get(hint.plugin_id) is hint
                and hint.scope == self.scope and hint.catalog_revision == self.revision
                and self.hints.get(hint.plugin_id) == hint.text
                and hint.skill_id in {c.id for c in self.candidates}
                and not any(c.required and c.id == hint.skill_id for c in self.candidates)
                and not (hint.mandatory or hint.must_keep or hint.safety_or_cleanup))

    def remove_own_hint(self, plugin_id: str, *, hint: SkillHint | None = None,
                        registration: str | None = None) -> bool:
        if hint is None and plugin_id in self.owned_hints:
            return False  # Native owned hints require exact registration provenance.
        if hint is not None and (hint.plugin_id != plugin_id or hint.registration != registration
                                 or not self.owns_hint(hint)):
            return False
        self.owned_hints.pop(plugin_id, None)
        return self.hints.pop(plugin_id, None) is not None
