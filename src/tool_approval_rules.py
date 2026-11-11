"""M5.3 increment 2: narrowly scoped approval rules ("Always allow").

One shared Gateway-owned rule store behind the four approval decisions. A rule
is created ONLY by an explicit user decision ("Always allow this specific
rule") on a genuine pending approval, and it suppresses only the automatic
approval PROMPT for the exact reviewed operation — never any other security
gate: admission, tool policy, request authority, resource/CWD binding, sealed
exact-operation validation, read-before-edit and derived-artifact publication
all still apply to every execution.

Rule shape (deliberately narrow):

* Owner-scoped; a session rule additionally binds the exact session id and is
  never visible to another session or owner.
* Binds the internal handler id AND its canonical model-visible name through
  the existing normalization boundary (``policy_spellings``), so either
  spelling matches and MCP names are never translated.
* Binds the EXACT reviewed operation content (the sealed input shown on the
  approval card) and the effective workspace (realpath) — a changed command,
  changed arguments, a different tool or a different CWD requires a fresh
  approval. There are NO wildcard or executable-prefix rules in this store:
  compound commands, substitutions and delegated subprocesses are never
  authorized by one matching component.
* Carries the operation's capability fingerprint (effects + result integrity);
  a policy change that alters the classified capabilities of the operation
  makes the rule inert (fail-closed).
* Records provenance (the origin approval id) and timestamps; persistent
  rules survive restarts through the trusted application data store and stay
  revocable at any time. Session rules expire with the pending-approval TTL.
* Malformed, unknown-version, revoked or expired rules never match.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "ScopedApprovalRule",
    "RULE_VERSION",
    "scoped_approval_rules",
    "bind_scoped_approval_rules",
    "persist_scoped_rule",
    "find_matching_scoped_rule",
    "list_scoped_rules",
    "revoke_scoped_rule",
    "RULES_MAX_PER_OWNER",
]

RULE_VERSION = 1
DEFAULT_SESSION_RULE_TTL_SECONDS = 10 * 60  # mirrors the pending-approval TTL
RULES_MAX_PER_OWNER = 256
_MAX_RULE_CONTENT_CHARS = 4000


@dataclass(frozen=True)
class ScopedApprovalRule:
    rule_id: str
    owner: str
    session_id: str  # "" = persistent (all future sessions of this owner)
    tool_name: str  # internal handler id
    canonical_name: str  # model-visible name
    content: str  # the exact reviewed operation content
    operation_digest: str  # canonical digest over the whole binding payload
    workspace: str  # realpath of the effective CWD when approved
    effects: tuple[str, ...]
    result_integrity: str
    origin_approval_id: str
    created_at: float
    updated_at: float
    expires_at: Optional[float]  # None = until revoked (persistent rules only)
    version: int = RULE_VERSION
    revoked: bool = False

    def public_payload(self) -> dict:
        """Inspectable, client-safe projection (display copy, never authority)."""
        return {
            "rule_id": self.rule_id,
            "owner": self.owner,
            "session_scoped": bool(self.session_id),
            "session_id": self.session_id or None,
            "tool": self.canonical_name or self.tool_name,
            "operation": self.content[:400],
            "workspace": self.workspace or None,
            "effects": list(self.effects),
            "result_integrity": self.result_integrity,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "revoked": self.revoked,
            "version": self.version,
        }


def _operation_digest(
    *, owner: str, session_id: str, tool_name: str, content: str, workspace: str,
    effects: tuple[str, ...], result_integrity: str,
) -> str:
    payload = {
        "owner": owner,
        "session_id": session_id,
        "tool_name": tool_name,
        "content": content,
        "workspace": workspace,
        "effects": list(effects),
        "result_integrity": result_integrity,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    import hashlib

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _rules_path() -> str:
    from src.constants import DATA_DIR

    return os.path.join(DATA_DIR, "approval_rules.json")


class _ScopedRuleStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._rules: dict[str, ScopedApprovalRule] = {}
        self._loaded = False

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        path = _rules_path()
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # A malformed store fails closed: no rules load; nothing is
            # silently reinterpreted as a permission.
            logger.warning("approval_rules.json unreadable; ignoring saved rules")
            return
        if not isinstance(raw, dict) or raw.get("version") != RULE_VERSION:
            logger.warning("approval_rules.json has an unknown version; ignoring")
            return
        items = raw.get("rules")
        if not isinstance(items, list):
            return
        now = time.time()
        for item in items:
            try:
                rule = ScopedApprovalRule(
                    rule_id=str(item["rule_id"]),
                    owner=str(item["owner"]),
                    session_id=str(item.get("session_id") or ""),
                    tool_name=str(item["tool_name"]),
                    canonical_name=str(item.get("canonical_name") or ""),
                    content=str(item["content"]),
                    operation_digest=str(item["operation_digest"]),
                    workspace=str(item.get("workspace") or ""),
                    effects=tuple(str(e) for e in item.get("effects") or ()),
                    result_integrity=str(item.get("result_integrity") or ""),
                    origin_approval_id=str(item.get("origin_approval_id") or ""),
                    created_at=float(item["created_at"]),
                    updated_at=float(item["updated_at"]),
                    expires_at=(
                        float(item["expires_at"]) if item.get("expires_at") is not None else None
                    ),
                    version=int(item.get("version") or RULE_VERSION),
                    revoked=bool(item.get("revoked") or False),
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("approval rule entry malformed; skipping it")
                continue
            if rule.version != RULE_VERSION:
                continue
            if rule.expires_at is not None and rule.expires_at <= now:
                continue
            self._rules[rule.rule_id] = rule

    def _save_locked(self) -> None:
        path = _rules_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        live = [r for r in self._rules.values() if not r.revoked]
        payload = {
            "version": RULE_VERSION,
            "rules": [asdict(rule) for rule in live],
        }
        temp = f"{path}.tmp-{os.getpid()}"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
        os.replace(temp, path)

    def persist(
        self, *, owner: str, session_id: str, tool_name: str, canonical_name: str,
        content: str, workspace: str, effects: tuple[str, ...], result_integrity: str,
        origin_approval_id: str, session_scoped: bool,
    ) -> ScopedApprovalRule:
        normalized_owner = str(owner or "").strip().casefold()
        content = str(content or "")[:_MAX_RULE_CONTENT_CHARS]
        now = time.time()
        with self._lock:
            self._load_locked()
            # Deduplicate: the identical rule (same binding payload) replaces
            # the previous entry instead of accumulating.
            digest = _operation_digest(
                owner=normalized_owner, session_id=session_id if session_scoped else "",
                tool_name=tool_name, content=content, workspace=workspace,
                effects=effects, result_integrity=result_integrity,
            )
            for existing in self._rules.values():
                if existing.operation_digest == digest and not existing.revoked:
                    self._rules.pop(existing.rule_id, None)
                    break
            owner_rule_count = sum(
                1 for rule in self._rules.values()
                if rule.owner == normalized_owner and not rule.revoked
            )
            if owner_rule_count >= RULES_MAX_PER_OWNER:
                raise ValueError("Too many saved approval rules for this owner")
            rule = ScopedApprovalRule(
                rule_id=f"rule-{os.urandom(8).hex()}",
                owner=normalized_owner,
                session_id=session_id if session_scoped else "",
                tool_name=tool_name,
                canonical_name=canonical_name,
                content=content,
                operation_digest=digest,
                workspace=workspace,
                effects=effects,
                result_integrity=result_integrity,
                origin_approval_id=str(origin_approval_id or ""),
                created_at=now,
                updated_at=now,
                expires_at=(
                    now + DEFAULT_SESSION_RULE_TTL_SECONDS if session_scoped else None
                ),
            )
            self._rules[rule.rule_id] = rule
            self._save_locked()
        return rule

    def find_matching(
        self, *, owner: str, session_id: Any, tool_name: str, content: str,
        workspace: str, capabilities,
    ) -> Optional[ScopedApprovalRule]:
        normalized_owner = str(owner or "").strip().casefold()
        requested_workspace = os.path.realpath(os.path.expanduser(workspace)) if (
            isinstance(workspace, str) and workspace.strip()
        ) else ""
        now = time.time()
        with self._lock:
            self._load_locked()
            for rule in self._rules.values():
                if rule.revoked or rule.owner != normalized_owner:
                    continue
                if rule.expires_at is not None and rule.expires_at <= now:
                    continue
                if rule.session_id:
                    # A session rule answers only inside its own session.
                    if str(session_id or "") != rule.session_id:
                        continue
                # Tool identity through the existing normalization boundary:
                # handler id and canonical name are the same tool; MCP and
                # inherited names are never translated.
                from src.tool_registry import policy_spellings

                if tool_name not in policy_spellings(rule.tool_name) and (
                    tool_name not in policy_spellings(rule.canonical_name)
                ):
                    continue
                if str(content or "") != rule.content:
                    continue
                if requested_workspace != rule.workspace:
                    continue
                effects = tuple(sorted(effect.value for effect in capabilities.effects))
                if effects != rule.effects or (
                    capabilities.result_integrity.value != rule.result_integrity
                ):
                    # Policy/classification changed since the rule was saved:
                    # fail closed instead of approving the new shape.
                    continue
                return rule
        return None

    def list_rules(self, owner: str, *, include_session: bool = True) -> list[dict]:
        normalized_owner = str(owner or "").strip().casefold()
        now = time.time()
        with self._lock:
            self._load_locked()
            items = []
            for rule in self._rules.values():
                if rule.revoked or rule.owner != normalized_owner:
                    continue
                if rule.expires_at is not None and rule.expires_at <= now:
                    continue
                if rule.session_id and not include_session:
                    continue
                items.append(rule.public_payload())
        items.sort(key=lambda item: item["created_at"], reverse=True)
        return items

    def revoke(self, owner: str, rule_id: str) -> bool:
        normalized_owner = str(owner or "").strip().casefold()
        with self._lock:
            self._load_locked()
            rule = self._rules.get(str(rule_id or ""))
            if rule is None or rule.owner != normalized_owner:
                return False
            self._rules[rule.rule_id] = ScopedApprovalRule(
                **{**asdict(rule), "revoked": True, "updated_at": time.time()}
            )
            self._save_locked()
        return True

    def reset_for_tests(self) -> None:
        with self._lock:
            self._rules.clear()
            self._loaded = False


_active_rules = _ScopedRuleStore()
_rules_binding: ContextVar[Optional[_ScopedRuleStore]] = ContextVar(
    "scoped_approval_rules", default=None,
)


def scoped_approval_rules() -> _ScopedRuleStore:
    """The active rule store (a bound test store or the process singleton)."""
    return _rules_binding.get() or _active_rules


def bind_scoped_approval_rules(store: _ScopedRuleStore):
    """Bind a disposable store (isolated test fixtures)."""
    token = _rules_binding.set(store)

    def _reset():
        _rules_binding.reset(token)

    return _reset


def persist_scoped_rule(**kwargs) -> ScopedApprovalRule:
    return scoped_approval_rules().persist(**kwargs)


def find_matching_scoped_rule(**kwargs) -> Optional[ScopedApprovalRule]:
    return scoped_approval_rules().find_matching(**kwargs)


def list_scoped_rules(owner: str, **kwargs) -> list[dict]:
    return scoped_approval_rules().list_rules(owner, **kwargs)


def revoke_scoped_rule(owner: str, rule_id: str) -> bool:
    return scoped_approval_rules().revoke(owner, rule_id)
