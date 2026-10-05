"""Deterministic Gmail triage workflow with continuous behavioral learning.

The LLM may start a triage and summarize/suggest. It does not own workflow
state, message IDs, direct-user authorization, execution, validation, or
advancement to the next message.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from openjarvis.connectors.gmail import GmailConnector
from openjarvis.core.paths import get_config_dir
from openjarvis.tools.approval_store import (
    STATUS_APPROVED,
    STATUS_EXECUTED,
    STATUS_PENDING,
    TIER_HIGH,
    ApprovalStore,
)

_DEFAULT_DB_PATH = str(get_config_dir() / "gmail_triage.db")
_DEFAULT_SESSION_KEY = "default"
_MAX_SESSION_KEY = 128
_DEFAULT_BODY_CHARS = 3200
_DETAIL_BODY_CHARS = 9000
_LEARNING_DOMAIN_MIN = 3
_LEARNING_CATEGORY_MIN = 5
_LEARNING_CONFIDENCE_MIN = 0.75

_ACTION_ARCHIVE = "archive"
_ACTION_TRASH = "trash"
_ACTION_SKIP = "skip"
_ACTION_DETAILS = "details"
_ACTION_CANCEL = "cancel"

_STATUS_ACTIVE = "active"
_STATUS_COMPLETED = "completed"
_STATUS_CANCELLED = "cancelled"

_ITEM_PENDING = "pending"
_ITEM_ARCHIVED = "archived"
_ITEM_TRASHED = "trashed"
_ITEM_SKIPPED = "skipped"
_ITEM_ERROR = "error"


class GmailTriageError(RuntimeError):
    """Base error for deterministic Gmail triage."""


class NoActiveTriage(GmailTriageError):
    """Raised when a direct command has no active triage session."""


@dataclass(slots=True)
class TriageSession:
    session_key: str
    session_id: str
    query: str
    status: str
    current_position: int
    total: int
    last_command_fingerprint: str = ""
    last_response: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    lowered = unicodedata.normalize("NFKD", text.lower())
    ascii_text = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", ascii_text).strip(" .,!?:;\t\r\n")


def _sender_domain(sender: str) -> str:
    address = parseaddr(sender)[1].strip().lower()
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1]


def _category(labels: Iterable[str]) -> str:
    for label in labels:
        if label.startswith("CATEGORY_"):
            return label
    return ""


def _compact(text: str, limit: int) -> tuple[str, bool]:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= limit:
        return clean, False
    return clean[: max(0, limit - 1)].rstrip() + "…", True


def classify_direct_command(text: str) -> Optional[str]:
    """Map a short direct user command to a deterministic triage action."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > 100:
        return None

    exact = {
        "arquivar": _ACTION_ARCHIVE,
        "arquive": _ACTION_ARCHIVE,
        "arquive este": _ACTION_ARCHIVE,
        "arquive este email": _ACTION_ARCHIVE,
        "arquivar este": _ACTION_ARCHIVE,
        "arquivar este email": _ACTION_ARCHIVE,
        "lixeira": _ACTION_TRASH,
        "apagar": _ACTION_TRASH,
        "apague": _ACTION_TRASH,
        "excluir": _ACTION_TRASH,
        "exclua": _ACTION_TRASH,
        "mover para lixeira": _ACTION_TRASH,
        "mova para lixeira": _ACTION_TRASH,
        "mandar para lixeira": _ACTION_TRASH,
        "mande para lixeira": _ACTION_TRASH,
        "pular": _ACTION_SKIP,
        "pule": _ACTION_SKIP,
        "pular este": _ACTION_SKIP,
        "proximo": _ACTION_SKIP,
        "proximo email": _ACTION_SKIP,
        "mais detalhes": _ACTION_DETAILS,
        "detalhes": _ACTION_DETAILS,
        "ver mais": _ACTION_DETAILS,
        "mostrar mais": _ACTION_DETAILS,
        "conteudo completo": _ACTION_DETAILS,
        "cancelar triagem": _ACTION_CANCEL,
        "encerrar triagem": _ACTION_CANCEL,
    }
    return exact.get(normalized)


def infer_triage_query(text: str) -> str:
    """Infer only the mailbox scope; never infer an action."""
    normalized = _normalize(text)
    if any(token in normalized for token in ("nao lido", "nao lidos", "unread")):
        return "is:unread"
    return "in:inbox"


def is_sequential_triage_request(text: str) -> bool:
    """Recognize only strong, explicit requests for one-by-one email triage."""
    normalized = _normalize(text)
    if not normalized:
        return False
    email_signal = any(
        token in normalized
        for token in ("email", "e-mail", "emails", "e-mails", "caixa de entrada")
    )
    sequential_signal = any(
        token in normalized
        for token in ("um por vez", "um a um", "cada um", "cada email", "cada e-mail")
    )
    review_signal = any(
        token in normalized
        for token in (
            "olhar",
            "olhada",
            "ver",
            "revisar",
            "revise",
            "revisa",
            "analisar",
            "triagem",
            "o que fazer",
        )
    )
    return email_signal and sequential_signal and review_signal


def command_fingerprint(
    messages: Iterable[Any],
    *,
    session_key: str = _DEFAULT_SESSION_KEY,
) -> str:
    """Stable request fingerprint used to make direct actions idempotent."""
    serializable = []
    for message in messages:
        role = getattr(message, "role", "")
        content = getattr(message, "content", "") or ""
        serializable.append([str(role), str(content)])
    raw = json.dumps(
        {"session_key": session_key, "messages": serializable},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class GmailTriageStore:
    """SQLite state for snapshots, direct decisions, and behavioral evidence."""

    def __init__(self, db_path: str = "") -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        path = Path(self.db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS triage_sessions (
                    session_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_position INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    last_command_fingerprint TEXT NOT NULL DEFAULT '',
                    last_response TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS triage_items (
                    session_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    message_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL DEFAULT '',
                    sender TEXT NOT NULL DEFAULT '',
                    sender_domain TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT '',
                    message_date TEXT NOT NULL DEFAULT '',
                    snippet TEXT NOT NULL DEFAULT '',
                    labels_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending',
                    action TEXT NOT NULL DEFAULT '',
                    verified_at TEXT,
                    PRIMARY KEY (session_id, position)
                );
                CREATE INDEX IF NOT EXISTS idx_triage_items_message
                    ON triage_items (message_id);
                CREATE INDEX IF NOT EXISTS idx_triage_items_status
                    ON triage_items (session_id, status);

                CREATE TABLE IF NOT EXISTS triage_learning_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender_domain TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_triage_learning_domain
                    ON triage_learning_events (sender_domain, action);
                CREATE INDEX IF NOT EXISTS idx_triage_learning_category
                    ON triage_learning_events (category, action);
                """
            )
            self._conn.commit()

    @staticmethod
    def _clean_session_key(session_key: str) -> str:
        clean = (session_key or _DEFAULT_SESSION_KEY).strip()
        return clean[:_MAX_SESSION_KEY] or _DEFAULT_SESSION_KEY

    def start(
        self,
        session_key: str,
        query: str,
        items: list[Dict[str, Any]],
    ) -> TriageSession:
        key = self._clean_session_key(session_key)
        session_id = uuid.uuid4().hex[:16]
        now = _utc_now()
        status = _STATUS_ACTIVE if items else _STATUS_COMPLETED
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO triage_sessions
                    (session_key, session_id, query, status, current_position,
                     total, last_command_fingerprint, last_response,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, '', '', ?, ?)
                ON CONFLICT(session_key) DO UPDATE SET
                    session_id=excluded.session_id,
                    query=excluded.query,
                    status=excluded.status,
                    current_position=0,
                    total=excluded.total,
                    last_command_fingerprint='',
                    last_response='',
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at
                """,
                (key, session_id, query, status, len(items), now, now),
            )
            for position, item in enumerate(items):
                labels = list(item.get("labels", []))
                sender = str(item.get("sender", ""))
                self._conn.execute(
                    """
                    INSERT INTO triage_items
                        (session_id, position, message_id, thread_id, sender,
                         sender_domain, subject, message_date, snippet,
                         labels_json, status, action)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                    """,
                    (
                        session_id,
                        position,
                        str(item.get("message_id", "")),
                        str(item.get("thread_id", "")),
                        sender,
                        _sender_domain(sender),
                        str(item.get("subject", "")),
                        str(item.get("date", "")),
                        str(item.get("snippet", "")),
                        json.dumps(labels, ensure_ascii=False),
                        _ITEM_PENDING,
                    ),
                )
            self._conn.commit()
        return self.get_session(key)  # type: ignore[return-value]

    def get_session(self, session_key: str) -> Optional[TriageSession]:
        key = self._clean_session_key(session_key)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM triage_sessions WHERE session_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return TriageSession(
            session_key=row["session_key"],
            session_id=row["session_id"],
            query=row["query"],
            status=row["status"],
            current_position=int(row["current_position"]),
            total=int(row["total"]),
            last_command_fingerprint=row["last_command_fingerprint"],
            last_response=row["last_response"],
        )

    def active_session(self, session_key: str) -> Optional[TriageSession]:
        session = self.get_session(session_key)
        if session and session.status == _STATUS_ACTIVE:
            return session
        return None

    def current_item(self, session: TriageSession) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM triage_items
                WHERE session_id = ? AND position = ?
                """,
                (session.session_id, session.current_position),
            ).fetchone()
        if row is None:
            return None
        return {
            "position": int(row["position"]),
            "message_id": row["message_id"],
            "thread_id": row["thread_id"],
            "sender": row["sender"],
            "sender_domain": row["sender_domain"],
            "subject": row["subject"],
            "date": row["message_date"],
            "snippet": row["snippet"],
            "labels": json.loads(row["labels_json"] or "[]"),
            "status": row["status"],
            "action": row["action"],
        }

    def mark_item(
        self,
        session: TriageSession,
        *,
        status: str,
        action: str,
        verified: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE triage_items
                SET status = ?, action = ?, verified_at = ?
                WHERE session_id = ? AND position = ?
                """,
                (
                    status,
                    action,
                    _utc_now() if verified else None,
                    session.session_id,
                    session.current_position,
                ),
            )
            self._conn.commit()

    def advance(self, session: TriageSession) -> TriageSession:
        next_position = session.current_position + 1
        status = _STATUS_ACTIVE if next_position < session.total else _STATUS_COMPLETED
        with self._lock:
            self._conn.execute(
                """
                UPDATE triage_sessions
                SET current_position = ?, status = ?, updated_at = ?
                WHERE session_key = ? AND session_id = ?
                """,
                (
                    next_position,
                    status,
                    _utc_now(),
                    session.session_key,
                    session.session_id,
                ),
            )
            self._conn.commit()
        return self.get_session(session.session_key)  # type: ignore[return-value]

    def cancel(self, session: TriageSession) -> TriageSession:
        with self._lock:
            self._conn.execute(
                """
                UPDATE triage_sessions
                SET status = ?, updated_at = ?
                WHERE session_key = ? AND session_id = ?
                """,
                (
                    _STATUS_CANCELLED,
                    _utc_now(),
                    session.session_key,
                    session.session_id,
                ),
            )
            self._conn.commit()
        return self.get_session(session.session_key)  # type: ignore[return-value]

    def remember_response(
        self,
        session: TriageSession,
        *,
        fingerprint: str,
        response: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                UPDATE triage_sessions
                SET last_command_fingerprint = ?, last_response = ?, updated_at = ?
                WHERE session_key = ? AND session_id = ?
                """,
                (
                    fingerprint,
                    response,
                    _utc_now(),
                    session.session_key,
                    session.session_id,
                ),
            )
            self._conn.commit()

    def record_learning(
        self,
        session: TriageSession,
        item: Dict[str, Any],
        *,
        action: str,
        origin: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO triage_learning_events
                    (session_id, message_id, sender_domain, category,
                     action, origin, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    item["message_id"],
                    item.get("sender_domain", ""),
                    _category(item.get("labels", [])),
                    action,
                    origin,
                    _utc_now(),
                ),
            )
            self._conn.commit()

    def learned_suggestion(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        domain = item.get("sender_domain", "")
        category = _category(item.get("labels", []))
        candidates = [
            ("remetente", "sender_domain", domain, _LEARNING_DOMAIN_MIN),
            ("categoria", "category", category, _LEARNING_CATEGORY_MIN),
        ]
        with self._lock:
            for basis, column, value, minimum in candidates:
                if not value:
                    continue
                rows = self._conn.execute(
                    f"""
                    SELECT action, COUNT(*) AS n
                    FROM triage_learning_events
                    WHERE {column} = ?
                    GROUP BY action
                    ORDER BY n DESC, action
                    """,
                    (value,),
                ).fetchall()
                total = sum(int(row["n"]) for row in rows)
                if total < minimum or not rows:
                    continue
                top_action = str(rows[0]["action"])
                top_count = int(rows[0]["n"])
                confidence = top_count / total
                if confidence < _LEARNING_CONFIDENCE_MIN:
                    continue
                return {
                    "action": top_action,
                    "count": top_count,
                    "total": total,
                    "confidence": round(confidence, 2),
                    "basis": basis,
                    "value": value,
                }
        return None

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class GmailTriageService:
    """High-level deterministic workflow around GmailConnector."""

    def __init__(
        self,
        *,
        session_key: str = _DEFAULT_SESSION_KEY,
        connector: Optional[GmailConnector] = None,
        store: Optional[GmailTriageStore] = None,
        approval_store: Optional[ApprovalStore] = None,
    ) -> None:
        self.session_key = GmailTriageStore._clean_session_key(session_key)
        self.connector = connector or GmailConnector()
        self.store = store or GmailTriageStore()
        self.approval_store = approval_store or ApprovalStore()
        self._owns_store = store is None
        self._owns_approval = approval_store is None

    def has_active(self) -> bool:
        return self.store.active_session(self.session_key) is not None

    def cached_response(self, fingerprint: str) -> Optional[str]:
        """Return the last deterministic response for an identical retried request."""
        session = self.store.get_session(self.session_key)
        if (
            session
            and fingerprint
            and session.last_command_fingerprint == fingerprint
            and session.last_response
        ):
            return session.last_response
        return None

    def start(
        self,
        *,
        query: str = "is:unread",
        max_results: int = 20,
        force_restart: bool = False,
    ) -> Dict[str, Any]:
        existing = self.store.active_session(self.session_key)
        if existing and not force_restart:
            return self.current()

        items = self.connector.list_message_metadata(
            query=query,
            max_results=max_results,
        )
        session = self.store.start(self.session_key, query, items)
        if session.status == _STATUS_COMPLETED:
            return {
                "status": _STATUS_COMPLETED,
                "total": 0,
                "position": 0,
                "message": "Nenhum e-mail corresponde ao snapshot solicitado.",
            }
        return self.current()

    def current(self, *, details: bool = False) -> Dict[str, Any]:
        session = self.store.active_session(self.session_key)
        if session is None:
            latest = self.store.get_session(self.session_key)
            if latest and latest.status == _STATUS_COMPLETED:
                return {
                    "status": _STATUS_COMPLETED,
                    "total": latest.total,
                    "position": latest.total,
                    "message": "Triagem concluída.",
                }
            raise NoActiveTriage("Nenhuma triagem Gmail ativa.")

        item = self.store.current_item(session)
        if item is None:
            raise GmailTriageError("Snapshot inconsistente: item atual ausente.")

        full = self.connector.get_message_content(item["message_id"])
        body_limit = _DETAIL_BODY_CHARS if details else _DEFAULT_BODY_CHARS
        body, truncated = _compact(
            full.get("body") or item.get("snippet", ""),
            body_limit,
        )
        suggestion = self.store.learned_suggestion(item)
        return {
            "status": session.status,
            "session_id": session.session_id,
            "position": session.current_position + 1,
            "total": session.total,
            "remaining": max(0, session.total - session.current_position - 1),
            "sender": item["sender"],
            "subject": item["subject"],
            "date": item["date"],
            "content": body,
            "content_truncated": truncated,
            "suggestion": suggestion,
        }

    def handle_direct_user_command(
        self,
        command: str,
        *,
        fingerprint: str,
    ) -> Dict[str, Any]:
        session = self.store.active_session(self.session_key)
        if session is None:
            raise NoActiveTriage("Nenhuma triagem Gmail ativa.")

        if (
            fingerprint
            and session.last_command_fingerprint == fingerprint
            and session.last_response
        ):
            return {
                "status": "duplicate",
                "response": session.last_response,
                "duplicate": True,
            }

        action = classify_direct_command(command)
        if action is None:
            raise GmailTriageError("Comando direto não reconhecido.")

        if action == _ACTION_CANCEL:
            self.store.cancel(session)
            response = "Triagem de e-mails encerrada sem alterar o item atual."
            self.store.remember_response(
                session,
                fingerprint=fingerprint,
                response=response,
            )
            return {"status": _STATUS_CANCELLED, "response": response}

        if action == _ACTION_DETAILS:
            current = self.current(details=True)
            response = self.render_current(current, details=True)
            self.store.remember_response(
                session,
                fingerprint=fingerprint,
                response=response,
            )
            return {
                "status": _STATUS_ACTIVE,
                "response": response,
                "current": current,
            }

        item = self.store.current_item(session)
        if item is None:
            raise GmailTriageError("Snapshot inconsistente: item atual ausente.")

        if action == _ACTION_SKIP:
            self.store.mark_item(
                session,
                status=_ITEM_SKIPPED,
                action=_ACTION_SKIP,
                verified=True,
            )
            self.store.record_learning(
                session,
                item,
                action=_ACTION_SKIP,
                origin="USER_DIRECT",
            )
            next_session = self.store.advance(session)
            response = self._after_action_response(
                "E-mail mantido sem alteração.",
                next_session,
            )
            self.store.remember_response(
                next_session,
                fingerprint=fingerprint,
                response=response,
            )
            return {
                "status": next_session.status,
                "action": _ACTION_SKIP,
                "verified": True,
                "response": response,
            }

        action_type = "email_archive" if action == _ACTION_ARCHIVE else "email_delete"
        action_record = self.approval_store.queue_action(
            action_type=action_type,
            description=(
                "Arquivar e-mail por comando direto do usuário"
                if action == _ACTION_ARCHIVE
                else "Mover e-mail para lixeira por comando direto do usuário"
            ),
            payload={
                "message_id": item["message_id"],
                "origin": "USER_DIRECT",
                "triage_session_id": session.session_id,
                "triage_position": session.current_position,
            },
            permission_key=f"{action_type}:triage:{session.session_id}:{session.current_position}",
            tier=TIER_HIGH,
        )
        if action_record.tier != TIER_HIGH or action_record.status != STATUS_PENDING:
            raise GmailTriageError("Gate high/pending não foi aplicado.")

        # The authenticated direct user request is the approval event. The LLM
        # cannot reach this path because it is not exposed as a tool.
        self.approval_store.update_status(action_record.id, STATUS_APPROVED)

        try:
            if action == _ACTION_ARCHIVE:
                self.connector.archive_message(item["message_id"])
                labels = self.connector.get_message_labels(item["message_id"])
                verified = "INBOX" not in labels
                item_status = _ITEM_ARCHIVED
                success_text = "Arquivado e verificado no Gmail."
            else:
                self.connector.delete_message(item["message_id"])
                labels = self.connector.get_message_labels(item["message_id"])
                verified = "TRASH" in labels
                item_status = _ITEM_TRASHED
                success_text = "Movido para a lixeira e verificado no Gmail."
        except Exception:
            self.approval_store.update_status(action_record.id, "execution_failed")
            self.store.mark_item(
                session,
                status=_ITEM_ERROR,
                action=action,
                verified=False,
            )
            raise

        if not verified:
            self.approval_store.update_status(
                action_record.id,
                "verification_failed",
            )
            self.store.mark_item(
                session,
                status=_ITEM_ERROR,
                action=action,
                verified=False,
            )
            response = (
                "A ação foi enviada ao Gmail, mas a validação do estado final falhou. "
                "A triagem permaneceu neste e-mail."
            )
            self.store.remember_response(
                session,
                fingerprint=fingerprint,
                response=response,
            )
            return {
                "status": _ITEM_ERROR,
                "action": action,
                "verified": False,
                "response": response,
            }

        self.approval_store.update_status(action_record.id, STATUS_EXECUTED)
        self.store.mark_item(
            session,
            status=item_status,
            action=action,
            verified=True,
        )
        self.store.record_learning(
            session,
            item,
            action=action,
            origin="USER_DIRECT",
        )
        next_session = self.store.advance(session)
        response = self._after_action_response(success_text, next_session)
        self.store.remember_response(
            next_session,
            fingerprint=fingerprint,
            response=response,
        )
        return {
            "status": next_session.status,
            "action": action,
            "verified": True,
            "approval_tier": TIER_HIGH,
            "approval_status": STATUS_EXECUTED,
            "response": response,
        }

    def _after_action_response(
        self,
        prefix: str,
        session: TriageSession,
    ) -> str:
        if session.status == _STATUS_COMPLETED:
            return f"{prefix}\n\nTriagem concluída: {session.total} de {session.total}."
        current = self.current()
        return f"{prefix}\n\n{self.render_current(current)}"

    @staticmethod
    def render_current(current: Dict[str, Any], *, details: bool = False) -> str:
        if current.get("status") == _STATUS_COMPLETED:
            return current.get("message", "Triagem concluída.")

        position = current.get("position", 0)
        total = current.get("total", 0)
        sender = current.get("sender") or "(remetente não informado)"
        subject = current.get("subject") or "(sem assunto)"
        content = current.get("content") or "(sem conteúdo textual)"
        lines = [
            f"{position} de {total}.",
            f"Remetente: {sender}",
            f"Assunto: {subject}",
            f"Conteúdo: {content}",
        ]
        if current.get("content_truncated") and not details:
            lines.append("O conteúdo foi abreviado; diga “mais detalhes” para ampliar.")
        suggestion = current.get("suggestion")
        if suggestion:
            action_labels = {
                _ACTION_ARCHIVE: "arquivar",
                _ACTION_TRASH: "lixeira",
                _ACTION_SKIP: "manter/pular",
            }
            label = action_labels.get(suggestion["action"], suggestion["action"])
            lines.append(
                "Sugestão aprendida: "
                f"{label} — {suggestion['count']}/{suggestion['total']} decisões "
                f"anteriores semelhantes ({int(suggestion['confidence'] * 100)}% "
                f"por {suggestion['basis']})."
            )
        lines.append("Decisão: arquivar, lixeira, pular ou mais detalhes.")
        return "\n".join(lines)

    def close(self) -> None:
        if self._owns_store:
            self.store.close()
        if self._owns_approval:
            self.approval_store.close()


__all__ = [
    "GmailTriageError",
    "GmailTriageService",
    "GmailTriageStore",
    "NoActiveTriage",
    "classify_direct_command",
    "command_fingerprint",
    "infer_triage_query",
    "is_sequential_triage_request",
]
