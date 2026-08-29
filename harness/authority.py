"""Outcome-based authorization for Collie actions.

The old gate answers a tool-shaped question: ``is browser_click external?``.
Authority v2 answers the user-shaped question: ``what effect will this exact call
have, and did the user already authorize that result?``.  Tool risk remains useful
input (and unknown tools still fail closed), but it is no longer the approval unit.

Only authenticated user text and stored grants create authority.  Page text, MCP
results, model prose, and other untrusted content must never be passed to
``RequestAuthority.compile``.  This invariant is intentionally enforced at the
surface/Harness boundary instead of accepting a generic list of messages here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Optional


class Effect(str, Enum):
    OBSERVE = "observe"
    PREPARE = "prepare"
    ACT = "act"
    COMMIT = "commit"
    RESTRICTED = "restricted"


class AuthorityDecision(str, Enum):
    ALLOW_SILENT = "allow_silent"
    ALLOW_NOTIFY = "allow_notify"
    ASK = "ask"
    DENY = "deny"
    NEEDS_PERSON = "needs_person"


class GrantScope(str, Enum):
    ONCE = "once"
    MISSION = "mission"
    WORKFLOW = "workflow"
    PROJECT = "project"
    CONNECTION = "connection"


@dataclass(frozen=True)
class ActionIntent:
    """A bounded description of the result of one proposed call."""

    action: str
    effect: Effect
    target: str = ""
    account: str = ""
    recipients: tuple[str, ...] = ()
    resource: str = ""
    connection_id: str = ""
    amount: Optional[float] = None
    currency: str = ""
    reversible: bool = False
    confidence: float = 1.0
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)

    def bounded(self) -> "ActionIntent":
        """Return a serialization-safe, size-bounded form for policy and audit."""
        def short(value: Any, limit: int = 300) -> str:
            return str(value or "").strip()[:limit]

        amount = self.amount
        if isinstance(amount, bool):
            amount = None
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError, OverflowError):
            amount = None
        if amount is not None and not (-1e12 < amount < 1e12):
            amount = None
        return ActionIntent(
            action=short(self.action, 100), effect=Effect(self.effect),
            target=short(self.target), account=short(self.account, 200),
            recipients=tuple(short(x, 200) for x in self.recipients[:100] if short(x, 200)),
            resource=short(self.resource), connection_id=short(self.connection_id, 160),
            amount=amount, currency=short(self.currency, 12).upper(),
            reversible=bool(self.reversible),
            confidence=max(0.0, min(1.0, float(self.confidence or 0.0))),
            reason=short(self.reason, 500), metadata={})


@dataclass(frozen=True)
class AuthorizationGrant:
    id: str
    scope: GrantScope
    action: str
    project: str = ""
    mission_id: str = ""
    workflow_id: str = ""
    connection_id: str = ""
    target: str = ""
    account: str = ""
    recipients: tuple[str, ...] = ()
    max_amount: Optional[float] = None
    currency: str = ""
    expires_at: int = 0
    uses_left: int = 0
    source: str = "user"
    created_at: int = 0
    revoked_at: int = 0


_ACTION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("send", (
        r"\b(send|forward|deliver)\b", r"发送", r"發送", r"发给", r"發給", r"寄给", r"寄給",
    )),
    ("publish", (
        r"\b(publish|post|release|deploy)\b", r"发布", r"發佈", r"发帖", r"發帖", r"上线", r"上線",
    )),
    ("submit", (
        r"\bsubmit\b", r"\bapply\b", r"提交", r"申请", r"申請", r"报名", r"報名",
    )),
    ("merge", (r"\bmerge\b", r"合并", r"合併")),
    ("invite", (r"\binvite\b", r"邀请", r"邀請")),
    ("delete", (
        r"\b(delete|remove|trash|uninstall)\b", r"删除", r"刪除", r"移除", r"卸载", r"解除安装",
    )),
    ("upload", (r"\b(upload|attach)\b", r"上传", r"上傳", r"附件", r"附上")),
    ("connect", (
        r"\b(connect|authorize|link|sign[ -]?in|log[ -]?in)\b", r"连接", r"連接", r"授权", r"授權",
        r"登录", r"登入",
    )),
    ("register", (r"\b(register|sign[ -]?up|create\s+(?:an?\s+)?account)\b", r"注册", r"註冊", r"创建账号", r"建立帳號")),
    ("verify", (r"\b(verify|verification|otp|one[ -]?time code)\b", r"验证", r"驗證", r"验证码", r"驗證碼")),
    ("purchase", (
        r"\b(buy|purchase|pay|checkout|subscribe|order)\b", r"购买", r"購買", r"付款", r"支付", r"订阅", r"訂閱", r"下单", r"下單",
    )),
    ("grant_access", (
        r"\b(grant|approve|allow)\s+(?:access|permission|admin)\b", r"授予权限", r"授予權限", r"管理员", r"管理員",
    )),
    ("security_change", (
        r"\b(change|reset|remove)\s+(?:password|passkey|mfa|2fa|security)\b", r"修改密码", r"修改密碼", r"重置密码", r"重設密碼", r"安全设置", r"安全設定",
    )),
)


def _plain_user_text(value: Any) -> str:
    """Extract only explicit text blocks from one authenticated user message."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts = []
    for block in value:
        if isinstance(block, dict) and block.get("type") in ("text", "input_text"):
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _email_addresses(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(x.lower() for x in re.findall(
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])", text or "")))


@dataclass(frozen=True)
class RequestAuthority:
    """The authority expressed by one authenticated user request."""

    actions: frozenset[str] = frozenset()
    recipients: tuple[str, ...] = ()
    request_sha256: str = ""
    explicit: bool = False

    @classmethod
    def compile(cls, user_message: Any) -> "RequestAuthority":
        text = _plain_user_text(user_message).strip()
        normalized = text.casefold()
        actions = set()
        for action, patterns in _ACTION_PATTERNS:
            if any(re.search(pattern, normalized, re.I) for pattern in patterns):
                actions.add(action)
        return cls(actions=frozenset(actions), recipients=_email_addresses(text),
                   request_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
                   explicit=bool(text))

    def allows(self, intent: ActionIntent) -> bool:
        if intent.action not in self.actions:
            return False
        # If both sides name recipients, the action may not silently introduce another one.
        if self.recipients and intent.recipients:
            requested = set(self.recipients)
            if not set(x.casefold() for x in intent.recipients).issubset(requested):
                return False
        return True


@dataclass
class AuthorityContext:
    request: RequestAuthority = field(default_factory=RequestAuthority)
    project: str = ""
    mission_id: str = ""
    workflow_id: str = ""
    mode: str = "hands_off"


@dataclass(frozen=True)
class AuthorityResult:
    decision: AuthorityDecision
    reason: str
    basis: str = ""
    grant_id: str = ""


def _same_or_unset(bound: str, actual: str) -> bool:
    return not bound or bool(actual and bound.casefold() == actual.casefold())


def grant_matches(grant: AuthorizationGrant, intent: ActionIntent,
                  context: AuthorityContext, now: Optional[int] = None) -> bool:
    now = int(time.time()) if now is None else int(now)
    if grant.revoked_at or (grant.expires_at and grant.expires_at <= now):
        return False
    if grant.uses_left < 0 or grant.action != intent.action:
        return False
    if not _same_or_unset(grant.project, context.project):
        return False
    if not _same_or_unset(grant.mission_id, context.mission_id):
        return False
    if not _same_or_unset(grant.workflow_id, context.workflow_id):
        return False
    if not _same_or_unset(grant.connection_id, intent.connection_id):
        return False
    if not _same_or_unset(grant.target, intent.target):
        return False
    if not _same_or_unset(grant.account, intent.account):
        return False
    if grant.recipients:
        allowed = {x.casefold() for x in grant.recipients}
        actual_recipients = intent.recipients or context.request.recipients
        if not actual_recipients or not {
                x.casefold() for x in actual_recipients}.issubset(allowed):
            return False
    if grant.max_amount is not None:
        if intent.amount is None or intent.amount > grant.max_amount:
            return False
        if grant.currency and grant.currency.casefold() != intent.currency.casefold():
            return False
    return True


class AuthorityStore:
    """Small durable grant ledger. Secrets and raw prompts never enter this DB."""

    def __init__(self, path: Optional[str] = None):
        state = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
        self.path = path or os.path.join(state, "authority.db")
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.execute("""CREATE TABLE IF NOT EXISTS authority_grants(
            id TEXT PRIMARY KEY, scope TEXT NOT NULL, action TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '', mission_id TEXT NOT NULL DEFAULT '',
            workflow_id TEXT NOT NULL DEFAULT '', connection_id TEXT NOT NULL DEFAULT '',
            target TEXT NOT NULL DEFAULT '', account TEXT NOT NULL DEFAULT '',
            recipients_json TEXT NOT NULL DEFAULT '[]', max_amount REAL,
            currency TEXT NOT NULL DEFAULT '', expires_at INTEGER NOT NULL DEFAULT 0,
            uses_left INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'user',
            created_at INTEGER NOT NULL, revoked_at INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS authority_grants_match ON authority_grants(action, project, revoked_at)")
        self.db.commit()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def add(self, *, scope: GrantScope, action: str, project: str = "",
            mission_id: str = "", workflow_id: str = "", connection_id: str = "",
            target: str = "", account: str = "", recipients: Iterable[str] = (),
            max_amount: Optional[float] = None, currency: str = "", expires_at: int = 0,
            uses_left: int = 0, source: str = "user") -> AuthorizationGrant:
        now = int(time.time())
        grant = AuthorizationGrant(
            id=uuid.uuid4().hex, scope=GrantScope(scope), action=str(action or "")[:100],
            project=str(project or "")[:200], mission_id=str(mission_id or "")[:200],
            workflow_id=str(workflow_id or "")[:200],
            connection_id=str(connection_id or "")[:200], target=str(target or "")[:300],
            account=str(account or "")[:200],
            recipients=tuple(dict.fromkeys(str(x)[:200] for x in recipients if str(x))),
            max_amount=max_amount, currency=str(currency or "")[:12].upper(),
            expires_at=int(expires_at or 0), uses_left=int(uses_left or 0),
            source=str(source or "user")[:80], created_at=now)
        if not grant.action:
            raise ValueError("grant action is required")
        with self._lock:
            self.db.execute("""INSERT INTO authority_grants(
                id,scope,action,project,mission_id,workflow_id,connection_id,target,account,
                recipients_json,max_amount,currency,expires_at,uses_left,source,created_at,revoked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (grant.id, grant.scope.value, grant.action, grant.project, grant.mission_id,
                 grant.workflow_id, grant.connection_id, grant.target, grant.account,
                 json.dumps(grant.recipients), grant.max_amount, grant.currency,
                 grant.expires_at, grant.uses_left, grant.source, grant.created_at, 0))
            self.db.commit()
        return grant

    @staticmethod
    def _row(row: sqlite3.Row) -> AuthorizationGrant:
        try:
            recipients = tuple(json.loads(row["recipients_json"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            recipients = ()
        return AuthorizationGrant(
            id=row["id"], scope=GrantScope(row["scope"]), action=row["action"],
            project=row["project"], mission_id=row["mission_id"],
            workflow_id=row["workflow_id"], connection_id=row["connection_id"],
            target=row["target"], account=row["account"], recipients=recipients,
            max_amount=row["max_amount"], currency=row["currency"],
            expires_at=row["expires_at"], uses_left=row["uses_left"], source=row["source"],
            created_at=row["created_at"], revoked_at=row["revoked_at"])

    def matching(self, intent: ActionIntent, context: AuthorityContext) -> Optional[AuthorizationGrant]:
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM authority_grants WHERE action=? AND revoked_at=0 ORDER BY created_at DESC",
                (intent.action,)).fetchall()
            for row in rows:
                grant = self._row(row)
                if not grant_matches(grant, intent, context):
                    continue
                if grant.scope is GrantScope.ONCE:
                    # A one-shot grant starts with uses_left=1.  Zero means unlimited for every
                    # other scope, but means exhausted for ONCE.
                    if grant.uses_left != 1:
                        continue
                    cur = self.db.execute(
                        "UPDATE authority_grants SET uses_left=0 WHERE id=? AND uses_left=1",
                        (grant.id,))
                    if cur.rowcount != 1:
                        continue
                    self.db.commit()
                return grant
        return None

    def revoke(self, grant_id: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE authority_grants SET revoked_at=? WHERE id=? AND revoked_at=0",
                                  (int(time.time()), str(grant_id or "")))
            self.db.commit()
            return cur.rowcount == 1

    def list(self, include_revoked: bool = False) -> list[AuthorizationGrant]:
        where = "" if include_revoked else " WHERE revoked_at=0"
        with self._lock:
            rows = self.db.execute("SELECT * FROM authority_grants" + where +
                                   " ORDER BY created_at DESC").fetchall()
        return [self._row(row) for row in rows]


_DEFAULT_STORES: dict[str, AuthorityStore] = {}
_DEFAULT_STORES_LOCK = threading.Lock()


def default_store() -> AuthorityStore:
    """One process-local connection per state directory for user-facing gates."""
    state = os.path.abspath(os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"))
    path = os.path.join(state, "authority.db")
    with _DEFAULT_STORES_LOCK:
        store = _DEFAULT_STORES.get(path)
        if store is None:
            store = AuthorityStore(path)
            _DEFAULT_STORES[path] = store
        return store


class AuthorityEngine:
    def __init__(self, store: Optional[AuthorityStore] = None):
        self.store = store

    def decide(self, intent: ActionIntent, context: AuthorityContext) -> AuthorityResult:
        intent = intent.bounded()
        if intent.effect is Effect.OBSERVE:
            return AuthorityResult(AuthorityDecision.ALLOW_SILENT, "observational action")
        if context.mode == "review":
            return AuthorityResult(AuthorityDecision.ASK, "review mode examines every external change")
        if intent.effect is Effect.PREPARE:
            return AuthorityResult(AuthorityDecision.ALLOW_SILENT, "reversible preparation")
        if intent.effect is Effect.ACT:
            return AuthorityResult(AuthorityDecision.ALLOW_NOTIFY, "scoped reversible action",
                                   basis="task scope")
        if self.store is not None:
            grant = self.store.matching(intent, context)
            if grant is not None:
                return AuthorityResult(AuthorityDecision.ALLOW_NOTIFY,
                                       "allowed by stored %s grant" % grant.scope.value,
                                       basis="stored grant", grant_id=grant.id)
        if intent.effect is Effect.COMMIT and context.request.allows(intent):
            return AuthorityResult(AuthorityDecision.ALLOW_NOTIFY,
                                   "the authenticated user explicitly requested this result",
                                   basis="user request:%s" % context.request.request_sha256[:12])
        if intent.effect is Effect.RESTRICTED:
            return AuthorityResult(AuthorityDecision.NEEDS_PERSON,
                                   intent.reason or "identity, legal, security, or spending boundary")
        return AuthorityResult(AuthorityDecision.ASK,
                               "the result was not explicitly authorized by the user or a stored policy")


_COMMIT_WORDS = re.compile(
    r"(?:^|\b)(send|publish|post|submit|merge|invite|save|confirm|create\s+(?:account|page)|"
    r"register|sign\s*up|authorize|approve|delete|remove|unsubscribe)(?:\b|$)|"
    r"发送|發送|发布|發佈|发帖|發帖|提交|合并|合併|邀请|邀請|确认|確認|注册|註冊|删除|刪除|退订|退訂",
    re.I)
_PURCHASE_WORDS = re.compile(
    r"(?:^|\b)(pay|buy|purchase|checkout|place\s+order|subscribe)(?:\b|$)|"
    r"付款|支付|购买|購買|结账|結帳|下单|下單|订阅|訂閱", re.I)
_PERSON_WORDS = re.compile(
    r"captcha|recaptcha|hcaptcha|biometric|face\s*id|touch\s*id|passkey|security\s*key|"
    r"hardware\s*key|kyc|legal\s+signature|verify\s+you(?:'re|\s+are)\s+human|"
    r"验证码图片|圖形驗證碼|人脸|人臉|指纹|指紋|生物识别|生物識別|实名认证|實名認證|法律签名|法律簽名",
    re.I)
_SECURITY_WORDS = re.compile(
    r"change\s+password|reset\s+password|remove\s+(?:mfa|2fa)|grant\s+admin|"
    r"修改密码|修改密碼|重置密码|重設密碼|关闭双重验证|關閉雙重驗證|授予管理员|授予管理員",
    re.I)


def _semantic_text(args: dict) -> str:
    values = []
    for key in ("text", "label", "name", "title", "selector", "action", "operation",
                "option", "key", "target", "href", "url"):
        value = args.get(key)
        if isinstance(value, (str, int, float)):
            values.append(str(value))
    return " ".join(values)[:2000]


def intent_for(tool_name: str, args: Optional[dict], *, risk: str = "", target: str = "",
               tool: Any = None) -> ActionIntent:
    """Host-owned deterministic intent classifier.

    Tools may provide ``_collie_intent(args)`` only when they are host classes already
    admitted by the registry.  Arbitrary plugin declarations are never trusted to lower
    an effect; malformed declarations fall through to the conservative classifier.
    """
    args = args if isinstance(args, dict) else {}
    resolver = getattr(tool, "_collie_intent", None)
    if callable(resolver):
        try:
            proposed = resolver(args)
            if isinstance(proposed, ActionIntent):
                if target and not proposed.target:
                    proposed = replace(proposed, target=target)
                return proposed.bounded()
        except Exception:
            pass
    if risk == "read":
        return ActionIntent("observe", Effect.OBSERVE, target=target, reversible=True)
    if risk in ("write_local", "exec"):
        return ActionIntent("local_change", Effect.ACT, target=target, reversible=True)

    semantic = _semantic_text(args)
    lower_name = str(tool_name or "").casefold()
    if lower_name == "browser_open":
        return ActionIntent("navigate", Effect.PREPARE, target=target, reversible=True)
    if lower_name in ("browser_advance", "browser_hover"):
        return ActionIntent("navigate", Effect.PREPARE, target=target, reversible=True)
    if lower_name in ("browser_type", "desktop_type"):
        if _PERSON_WORDS.search(semantic):
            return ActionIntent("person_verification", Effect.RESTRICTED, target=target,
                                reason="this verification method requires the person")
        if bool(args.get("submit")):
            return ActionIntent("submit", Effect.COMMIT, target=target)
        return ActionIntent("enter_data", Effect.PREPARE, target=target, reversible=True)
    if lower_name in ("browser_pick", "browser_hover", "desktop_focus", "desktop_menu"):
        return ActionIntent("prepare", Effect.PREPARE, target=target, reversible=True)
    if lower_name == "browser_press":
        key = str(args.get("key") or "").casefold()
        if key in ("enter", "return", "space"):
            return ActionIntent("submit", Effect.COMMIT, target=target,
                                reason="the focused control may commit the form")
        return ActionIntent("navigate", Effect.PREPARE, target=target, reversible=True)
    if lower_name == "browser_upload":
        return ActionIntent("upload", Effect.COMMIT, target=target,
                            resource=str(args.get("path") or args.get("paths") or "")[:300])
    if lower_name in ("browser_drag", "desktop_drag"):
        if _PERSON_WORDS.search(semantic):
            return ActionIntent("person_verification", Effect.RESTRICTED, target=target,
                                reason="CAPTCHA and human verification cannot be delegated")
        return ActionIntent("external_change", Effect.COMMIT, target=target)
    if lower_name in ("browser_eval", "browser_script", "desktop_script"):
        return ActionIntent("external_change", Effect.COMMIT, target=target,
                            reason="a script can combine multiple external effects")
    if lower_name in ("browser_click", "desktop_click", "desktop_uia", "desktop_win32",
                      "desktop_mouse", "desktop_key", "desktop_window"):
        if _PERSON_WORDS.search(semantic):
            return ActionIntent("person_verification", Effect.RESTRICTED, target=target,
                                reason="this control appears to require the person")
        if _PURCHASE_WORDS.search(semantic):
            return ActionIntent("purchase", Effect.RESTRICTED, target=target,
                                reason="spending requires an explicit bounded grant")
        if _SECURITY_WORDS.search(semantic):
            return ActionIntent("security_change", Effect.RESTRICTED, target=target,
                                reason="account security changes require the person")
        if _COMMIT_WORDS.search(semantic):
            action = "external_change"
            for candidate in ("send", "publish", "submit", "merge", "invite", "delete", "register", "grant_access"):
                if candidate in RequestAuthority.compile(semantic).actions:
                    action = candidate
                    break
            return ActionIntent(action, Effect.COMMIT, target=target)
        # A semantic click with no final-action vocabulary is routine UI work. Exact-ref
        # clicks with no semantic material stay a commit until the browser adapter resolves
        # the ref; this prevents a bare ``e7`` from silently meaning Send.
        has_semantic_target = any(str(args.get(k) or "").strip() for k in
                                  ("text", "label", "name", "title", "selector", "action", "operation"))
        return ActionIntent("external_change", Effect.ACT if has_semantic_target else Effect.COMMIT,
                            target=target, reversible=has_semantic_target,
                            confidence=0.8 if has_semantic_target else 0.3,
                            reason="snapshot ref needs semantic resolution" if not has_semantic_target else "")
    if lower_name == "mcpctl_connect":
        return ActionIntent("connect", Effect.COMMIT, target=target,
                            connection_id=str(args.get("name") or ""))
    if lower_name in ("mcpctl_add", "mcpctl_remove"):
        return ActionIntent("connect", Effect.COMMIT, target=target,
                            connection_id=str(args.get("name") or ""))
    if lower_name == "mcpctl_set_enabled":
        enabled = args.get("enabled")
        return ActionIntent("connect", Effect.COMMIT if enabled is not False else Effect.ACT,
                            target=target, reversible=True,
                            connection_id=str(args.get("name") or ""))
    if lower_name.startswith("mcp__"):
        annotations = getattr(tool, "_annotations", {}) if tool is not None else {}
        if isinstance(annotations, dict) and annotations.get("readOnlyHint") is True and \
                getattr(tool, "_authority_manifest_approved", False):
            return ActionIntent("observe", Effect.OBSERVE, target=target,
                                connection_id=str(getattr(tool, "_server", "")), reversible=True)
        destructive = isinstance(annotations, dict) and annotations.get("destructiveHint") is True
        return ActionIntent("external_change", Effect.COMMIT if not destructive else Effect.RESTRICTED,
                            target=target, connection_id=str(getattr(tool, "_server", "")),
                            reason="MCP effect manifest has not been approved" if not destructive
                            else "MCP declares this action destructive")
    if lower_name == "delegate":
        return ActionIntent("delegate", Effect.ACT, target=target, reversible=True)
    if lower_name == "enable_capability":
        return ActionIntent("grant_access", Effect.COMMIT, target=target)
    # Unknown external tools remain a commit and therefore need explicit authority.
    return ActionIntent("external_change", Effect.COMMIT, target=target, confidence=0.0,
                        reason="unknown external effect")


def intent_dict(intent: ActionIntent) -> dict:
    out = asdict(intent.bounded())
    out["effect"] = intent.effect.value
    return out
