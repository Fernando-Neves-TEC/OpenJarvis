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
_LEARNING_PAIR_MIN = 3
_LEARNING_DOMAIN_FALLBACK_MIN = 5
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


def _has_term(text: str, term: str) -> bool:
    """Match a word or phrase without accepting substrings inside another word."""
    return re.search(
        rf"(?<!\w){re.escape(term)}(?!\w)",
        text,
    ) is not None


def _last_term_position(text: str, terms: Iterable[str]) -> int:
    """Return the last whole-term match position, or -1 when absent."""
    position = -1
    for term in terms:
        for match in re.finditer(rf"(?<!\w){re.escape(term)}(?!\w)", text):
            position = max(position, match.start())
    return position


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
    """Map an unequivocal user command to a deterministic triage action.

    Natural wrappers are accepted so a direct user order does not fall back to
    the LLM merely because it was phrased conversationally. Advisory, doubtful
    or negated wording is deliberately rejected.
    """
    normalized = _normalize(text)
    if not normalized or len(normalized) > 180:
        return None

    normalized = re.sub(r"[,.!?:;]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    ambiguous_markers = (
        "talvez",
        "acho",
        "acha",
        "devo",
        "deveria",
        "seria melhor",
        "o que voce acha",
        "o que acha",
        "sugere",
        "sugerir",
        "recomenda",
        "recomendar",
        "nao sei",
        "em duvida",
    )
    if any(marker in normalized for marker in ambiguous_markers):
        return None
    if "nao" in normalized.split():
        return None

    tokens = normalized.split()
    leading_fillers = {
        "sim",
        "ok",
        "okay",
        "certo",
        "entao",
        "por",
        "favor",
        "voce",
        "vc",
        "eu",
        "quero",
        "que",
        "pode",
        "poderia",
        "vamos",
    }
    trailing_fillers = {
        "este",
        "esta",
        "esse",
        "essa",
        "isso",
        "ele",
        "email",
        "e-mail",
        "mensagem",
        "agora",
        "por",
        "favor",
        "pra",
        "para",
        "mim",
        "ta",
    }

    while tokens and tokens[0] in leading_fillers:
        tokens.pop(0)
    while tokens and tokens[-1] in trailing_fillers:
        tokens.pop()

    command = " ".join(tokens)
    exact = {
        "arquivar": _ACTION_ARCHIVE,
        "arquiva": _ACTION_ARCHIVE,
        "arquive": _ACTION_ARCHIVE,
        "lixeira": _ACTION_TRASH,
        "apagar": _ACTION_TRASH,
        "apague": _ACTION_TRASH,
        "excluir": _ACTION_TRASH,
        "exclua": _ACTION_TRASH,
        "mover para lixeira": _ACTION_TRASH,
        "mova para lixeira": _ACTION_TRASH,
        "mandar para lixeira": _ACTION_TRASH,
        "mande para lixeira": _ACTION_TRASH,
        "mandar pra lixeira": _ACTION_TRASH,
        "mande pra lixeira": _ACTION_TRASH,
        "jogar na lixeira": _ACTION_TRASH,
        "jogue na lixeira": _ACTION_TRASH,
        "joga na lixeira": _ACTION_TRASH,
        "pular": _ACTION_SKIP,
        "pule": _ACTION_SKIP,
        "manter": _ACTION_SKIP,
        "deixar": _ACTION_SKIP,
        "deixe": _ACTION_SKIP,
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
    return exact.get(command)


def infer_triage_query(text: str) -> str:
    """Infer only the mailbox scope; never infer an action."""
    normalized = _normalize(text)
    if any(token in normalized for token in ("nao lido", "nao lidos", "unread")):
        return "in:inbox is:unread"
    return "in:inbox"


def is_sequential_triage_request(text: str) -> bool:
    """Recognize explicit mailbox review requests and route them to a snapshot.

    A user does not need to say "um por vez". Reviewing the inbox is enough to
    enter deterministic triage; the workflow itself presents and tracks items.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    mailbox_signal = any(
        _has_term(normalized, term)
        for term in (
            "caixa de entrada",
            "caixa de email",
            "caixa de e-mail",
            "inbox",
            "emails",
            "e-mails",
            "meus emails",
            "meus e-mails",
        )
    )
    review_signal = any(
        _has_term(normalized, term)
        for term in (
            "olhar",
            "olhada",
            "dar uma olhada",
            "ver",
            "verificar",
            "revisar",
            "revisasse",
            "revise",
            "revisa",
            "analisar",
            "analise",
            "triagem",
            "o que fazer",
        )
    )
    return mailbox_signal and review_signal


def classify_action_intent(text: str) -> Optional[str]:
    """Recognize an explicit archive/trash order even when it names targets."""
    normalized = _normalize(text)
    if not normalized or len(normalized) > 500:
        return None
    ambiguous = (
        "talvez",
        "acho",
        "devo",
        "deveria",
        "o que voce acha",
        "o que acha",
        "sugere",
        "recomenda",
        "nao sei",
        "em duvida",
    )
    if any(marker in normalized for marker in ambiguous):
        return None
    if "nao " in normalized or normalized.startswith("nao"):
        return None

    archive_terms = ("arquivar", "arquive", "arquiva")
    trash_terms = (
        "excluir",
        "exclua",
        "apagar",
        "apague",
        "lixeira",
    )
    archive_pos = _last_term_position(normalized, archive_terms)
    trash_pos = _last_term_position(normalized, trash_terms)
    if archive_pos < 0 and trash_pos < 0:
        return None
    if archive_pos >= 0 and trash_pos >= 0:
        if "melhor dizendo" not in normalized and "corrigindo" not in normalized:
            return None
        return _ACTION_ARCHIVE if archive_pos > trash_pos else _ACTION_TRASH
    return _ACTION_ARCHIVE if archive_pos >= 0 else _ACTION_TRASH


def is_pending_email_approval_reference(text: str) -> bool:
    """Recognize explicit references to the OpenJarvis email approval queue."""
    normalized = _normalize(text)
    if not normalized:
        return False
    markers = (
        "aguardando aprovacao",
        "pendente",
        "pendentes",
        "aprovacao",
        "aprovacoes",
        "sininho",
        "aviso",
        "avisos",
    )
    if any(_has_term(normalized, marker) for marker in markers):
        return True

    mailbox_waiting = _has_term(normalized, "aguardando") and any(
        _has_term(normalized, term)
        for term in ("email", "e-mail", "emails", "e-mails")
    )
    proposed_action = any(
        _has_term(normalized, term)
        for term in (
            "arquivado",
            "arquivados",
            "arquivada",
            "arquivadas",
            "excluido",
            "excluidos",
            "excluida",
            "excluidas",
            "lixeira",
        )
    )
    return mailbox_waiting and proposed_action


def _is_collective_pending_reference(text: str) -> bool:
    normalized = _normalize(text)
    markers = (
        "todos",
        "todas",
        "esses",
        "essas",
        "os dois",
        "as duas",
        "os tres",
        "as tres",
        "os três",
        "as três",
        "pendentes",
    )
    return any(_has_term(normalized, marker) for marker in markers)


def classify_snapshot_query(text: str) -> Optional[str]:
    """Classify factual questions that must be answered from the live snapshot."""
    normalized = _normalize(text)
    if not normalized:
        return None
    if any(
        phrase in normalized
        for phrase in (
            "quantas restam",
            "quantos restam",
            "quantas faltam",
            "quantos faltam",
            "quantas mensagens restam",
        )
    ):
        return "remaining_count"
    if any(
        phrase in normalized
        for phrase in (
            "quais outras mensagens",
            "quais mensagens restam",
            "quais restam",
            "o que mais tem",
            "o que ainda tem",
            "quais outras",
        )
    ):
        return "remaining_list"
    if any(
        phrase in normalized
        for phrase in ("detalhes", "conteudo", "conteúdo", "me fale mais", "me diga mais")
    ) and any(
        token in normalized for token in ("essa", "esse", "dessa", "desse", "do ", "da ")
    ):
        return "reference_details"
    return None


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

    def all_items(
        self,
        session: TriageSession,
        *,
        pending_only: bool = False,
    ) -> list[Dict[str, Any]]:
        """Return snapshot items in stable position order."""
        sql = "SELECT * FROM triage_items WHERE session_id = ?"
        params: tuple[Any, ...] = (session.session_id,)
        if pending_only:
            sql += " AND status = ?"
            params = (session.session_id, _ITEM_PENDING)
        sql += " ORDER BY position"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
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
            for row in rows
        ]

    def hydrate_item(
        self,
        session: TriageSession,
        position: int,
        metadata: Dict[str, Any],
    ) -> None:
        """Persist metadata for any snapshot item without persisting its body."""
        sender = str(metadata.get("sender", ""))
        labels = list(metadata.get("labels", []))
        with self._lock:
            self._conn.execute(
                """
                UPDATE triage_items
                SET thread_id = ?, sender = ?, sender_domain = ?,
                    subject = ?, message_date = ?, snippet = ?, labels_json = ?
                WHERE session_id = ? AND position = ?
                """,
                (
                    str(metadata.get("thread_id", "")),
                    sender,
                    _sender_domain(sender),
                    str(metadata.get("subject", "")),
                    str(metadata.get("date", "")),
                    str(metadata.get("snippet", "")),
                    json.dumps(labels, ensure_ascii=False),
                    session.session_id,
                    int(position),
                ),
            )
            self._conn.commit()

    def mark_item_at(
        self,
        session: TriageSession,
        position: int,
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
                    int(position),
                ),
            )
            self._conn.commit()

    def reposition_to_next_pending(self, session: TriageSession) -> TriageSession:
        """Point at the first unresolved snapshot item or complete the session."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT MIN(position) AS position
                FROM triage_items
                WHERE session_id = ? AND status = ?
                """,
                (session.session_id, _ITEM_PENDING),
            ).fetchone()
            if row is not None and row["position"] is not None:
                position = int(row["position"])
                status = _STATUS_ACTIVE
            else:
                position = session.total
                status = _STATUS_COMPLETED
            self._conn.execute(
                """
                UPDATE triage_sessions
                SET current_position = ?, status = ?, updated_at = ?
                WHERE session_key = ? AND session_id = ?
                """,
                (
                    position,
                    status,
                    _utc_now(),
                    session.session_key,
                    session.session_id,
                ),
            )
            self._conn.commit()
        return self.get_session(session.session_key)  # type: ignore[return-value]

    def hydrate_current(
        self,
        session: TriageSession,
        metadata: Dict[str, Any],
    ) -> None:
        """Persist metadata for the current item without persisting its body."""
        sender = str(metadata.get("sender", ""))
        labels = list(metadata.get("labels", []))
        with self._lock:
            self._conn.execute(
                """
                UPDATE triage_items
                SET thread_id = ?, sender = ?, sender_domain = ?,
                    subject = ?, message_date = ?, snippet = ?, labels_json = ?
                WHERE session_id = ? AND position = ?
                """,
                (
                    str(metadata.get("thread_id", "")),
                    sender,
                    _sender_domain(sender),
                    str(metadata.get("subject", "")),
                    str(metadata.get("date", "")),
                    str(metadata.get("snippet", "")),
                    json.dumps(labels, ensure_ascii=False),
                    session.session_id,
                    session.current_position,
                ),
            )
            self._conn.commit()

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
        """Return a conservative suggestion learned from direct user decisions.

        Prefer sender-domain + Gmail category so, for example, promotional
        messages and security/update messages from the same provider do not
        contaminate one another. Domain-only fallback requires more evidence.
        Category-only learning is deliberately avoided because it is too broad.
        """
        domain = item.get("sender_domain", "")
        category = _category(item.get("labels", []))

        candidates: list[tuple[str, str, tuple[str, ...], int]] = []
        if domain and category:
            candidates.append(
                (
                    "remetente + categoria",
                    "sender_domain = ? AND category = ?",
                    (domain, category),
                    _LEARNING_PAIR_MIN,
                )
            )
        if domain:
            candidates.append(
                (
                    "remetente",
                    "sender_domain = ?",
                    (domain,),
                    _LEARNING_DOMAIN_FALLBACK_MIN,
                )
            )

        with self._lock:
            for basis, where_clause, params, minimum in candidates:
                rows = self._conn.execute(
                    f"""
                    SELECT action, COUNT(*) AS n
                    FROM triage_learning_events
                    WHERE {where_clause}
                    GROUP BY action
                    ORDER BY n DESC, action
                    """,
                    params,
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
                    "value": " / ".join(params),
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

    def pending_email_approvals(self) -> list[Any]:
        """Return current pending Gmail mutation approvals."""
        return [
            action
            for action in self.approval_store.list_pending()
            if action.action_type in {"email_archive", "email_delete"}
        ]

    def render_pending_email_approvals(self) -> Dict[str, Any]:
        """Render the approval bell from deterministic state, without message bodies."""
        pending = self.pending_email_approvals()
        if not pending:
            return {
                "count": 0,
                "response": "Não há e-mails aguardando aprovação no OpenJarvis.",
            }

        lines = [
            f"Há {len(pending)} "
            + ("e-mail aguardando aprovação:" if len(pending) == 1 else "e-mails aguardando aprovação:")
        ]
        for index, action in enumerate(pending, 1):
            payload = dict(action.payload or {})
            message_id = str(payload.get("message_id", "")).strip()
            sender = str(payload.get("sender", "")).strip()
            subject = str(payload.get("subject", "")).strip()
            if message_id and (not sender or not subject):
                try:
                    metadata = self.connector.get_message_metadata(message_id)
                except Exception:
                    metadata = {}
                sender = sender or str(metadata.get("sender", "")).strip()
                subject = subject or str(metadata.get("subject", "")).strip()
            action_label = (
                "Arquivar"
                if action.action_type == "email_archive"
                else "Mover para a lixeira"
            )
            sender_name = parseaddr(sender)[0] or sender or "(remetente não informado)"
            lines.append(
                f"{index}. {action_label} — {sender_name} — "
                f"{subject or '(sem assunto)'}"
            )
        lines.append(
            "Você pode aprovar/negá-los pelo sino ou dar uma ordem direta inequívoca."
        )
        return {"count": len(pending), "response": "\n".join(lines)}

    def handle_pending_email_approval_action(self, command: str) -> Dict[str, Any]:
        """Execute explicitly referenced pending e-mail approvals after Gmail verification."""
        action = classify_action_intent(command)
        if action not in {_ACTION_ARCHIVE, _ACTION_TRASH}:
            raise GmailTriageError("Ação de aprovação pendente não reconhecida.")

        pending = self.pending_email_approvals()
        if not pending:
            return {
                "verified": True,
                "completed": 0,
                "response": "Não há e-mails aguardando aprovação no OpenJarvis.",
            }
        if len(pending) > 1 and not _is_collective_pending_reference(command):
            return {
                "verified": False,
                "completed": 0,
                "response": (
                    f"Há {len(pending)} e-mails aguardando aprovação. "
                    "Especifique qual deles ou diga explicitamente para agir em todos."
                ),
            }

        expected_type = "email_archive" if action == _ACTION_ARCHIVE else "email_delete"
        incompatible = [item for item in pending if item.action_type != expected_type]
        if incompatible:
            return {
                "verified": False,
                "completed": 0,
                "response": (
                    "As aprovações pendentes não correspondem todas à ação pedida. "
                    "Nenhuma alteração foi feita; revise o sino antes de prosseguir."
                ),
            }

        completed = 0
        for approval in pending:
            if approval.tier != TIER_HIGH or approval.status != STATUS_PENDING:
                return {
                    "verified": False,
                    "completed": completed,
                    "response": (
                        "O estado da fila de aprovação mudou durante a execução. "
                        "A operação foi interrompida sem declarar sucesso."
                    ),
                }
            message_id = str(approval.payload.get("message_id", "")).strip()
            if not message_id:
                return {
                    "verified": False,
                    "completed": completed,
                    "response": (
                        "Uma aprovação pendente não possui ID interno de mensagem. "
                        "A operação foi interrompida sem declarar sucesso."
                    ),
                }

            labels_before = self.connector.get_message_labels(message_id)
            already_verified = (
                "INBOX" not in labels_before
                if action == _ACTION_ARCHIVE
                else "TRASH" in labels_before
            )
            if not already_verified:
                self.approval_store.update_status(approval.id, STATUS_APPROVED)
                try:
                    if action == _ACTION_ARCHIVE:
                        self.connector.archive_message(message_id)
                    else:
                        self.connector.delete_message(message_id)
                    labels_after = self.connector.get_message_labels(message_id)
                    verified = (
                        "INBOX" not in labels_after
                        if action == _ACTION_ARCHIVE
                        else "TRASH" in labels_after
                    )
                except Exception:
                    self.approval_store.update_status(approval.id, STATUS_PENDING)
                    raise
                if not verified:
                    self.approval_store.update_status(approval.id, STATUS_PENDING)
                    return {
                        "verified": False,
                        "completed": completed,
                        "response": (
                            f"{completed} de {len(pending)} ações tiveram resultado "
                            "confirmado. A próxima não foi validada no Gmail; "
                            "ela permanece pendente e nenhum sucesso foi declarado para ela."
                        ),
                    }

            self.approval_store.update_status(approval.id, STATUS_EXECUTED)
            completed += 1

        noun = "e-mail" if completed == 1 else "e-mails"
        result = (
            "arquivado e verificado"
            if action == _ACTION_ARCHIVE and completed == 1
            else "arquivados e verificados"
            if action == _ACTION_ARCHIVE
            else "movido para a lixeira e verificado"
            if completed == 1
            else "movidos para a lixeira e verificados"
        )
        return {
            "verified": True,
            "completed": completed,
            "response": f"{completed} {noun} {result} no Gmail. Aprovações pendentes: 0.",
        }

    def start(
        self,
        *,
        query: str = "in:inbox is:unread",
        max_results: Optional[int] = None,
        force_restart: bool = False,
    ) -> Dict[str, Any]:
        existing = self.store.active_session(self.session_key)
        if existing and not force_restart:
            return self.current()

        items = self.connector.list_message_stubs(
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

        if details:
            hydrated = self.connector.get_message_content(item["message_id"])
        else:
            hydrated = self.connector.get_message_metadata(item["message_id"])
        self.store.hydrate_current(session, hydrated)
        item = self.store.current_item(session)
        if item is None:
            raise GmailTriageError("Snapshot inconsistente após hidratação.")

        if details:
            content, truncated = _compact(
                hydrated.get("body") or item.get("snippet", ""),
                _DETAIL_BODY_CHARS,
            )
        else:
            content, truncated = _compact(
                hydrated.get("snippet") or item.get("snippet", ""),
                _DEFAULT_BODY_CHARS,
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
            "content": content,
            "content_truncated": truncated,
            "details": details,
            "suggestion": suggestion,
        }

    def _ensure_snapshot_metadata(
        self,
        session: TriageSession,
    ) -> list[Dict[str, Any]]:
        """Hydrate headers/snippets for snapshot matching without downloading bodies."""
        items = self.store.all_items(session)
        for item in items:
            if item.get("sender") and item.get("subject"):
                continue
            metadata = self.connector.get_message_metadata(item["message_id"])
            self.store.hydrate_item(session, item["position"], metadata)
        return self.store.all_items(session)

    @staticmethod
    def _reference_terms(text: str) -> set[str]:
        normalized = _normalize(text)
        tokens = set(re.findall(r"[a-z0-9]+", normalized))
        stop = {
            "a", "as", "o", "os", "um", "uma", "uns", "umas", "da", "das",
            "de", "do", "dos", "e", "em", "no", "na", "nos", "nas", "para",
            "pra", "por", "me", "mim", "voce", "vc", "essas", "esses", "essa",
            "esse", "mensagem", "mensagens", "email", "emails", "pode", "podem",
            "todos", "todas", "esse", "essa", "dessa", "desse", "arquivar",
            "arquive", "arquiva", "excluir", "exclua", "apagar", "apague",
            "lixeira", "agora", "ta", "ok", "sim", "melhor", "dizendo",
        }
        return {token for token in tokens if len(token) >= 4 and token not in stop}

    def _match_snapshot_items(
        self,
        session: TriageSession,
        text: str,
        *,
        pending_only: bool = True,
    ) -> list[Dict[str, Any]]:
        items = self._ensure_snapshot_metadata(session)
        if pending_only:
            items = [item for item in items if item["status"] == _ITEM_PENDING]
        terms = self._reference_terms(text)
        if not terms:
            return []

        normalized_text = _normalize(text)
        matches: list[tuple[int, Dict[str, Any]]] = []
        for item in items:
            sender = str(item.get("sender", ""))
            sender_name = _normalize(parseaddr(sender)[0])
            domain = str(item.get("sender_domain", ""))
            domain_root = domain.split(".")[-2] if "." in domain else domain
            haystack = _normalize(
                f"{sender} {domain} {item.get('subject', '')} {item.get('snippet', '')}"
            )
            hay_tokens = set(re.findall(r"[a-z0-9]+", haystack))
            overlap = terms & hay_tokens
            score = len(overlap)
            if sender_name and len(sender_name) >= 4 and sender_name in normalized_text:
                score += 5
            if domain_root and len(domain_root) >= 4 and domain_root in terms:
                score += 4
            if score > 0:
                matches.append((score, item))

        if not matches:
            return []
        # Reference terms are already stripped of generic/action words, so any
        # positive match is intentional enough for deterministic selection.
        # This preserves multi-target phrases such as "pull request e Google".
        return [item for score, item in matches if score > 0]

    def _execute_item_action(
        self,
        session: TriageSession,
        item: Dict[str, Any],
        action: str,
    ) -> tuple[bool, str]:
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
                "triage_position": item["position"],
            },
            permission_key=(
                f"{action_type}:triage:{session.session_id}:{item['position']}"
            ),
            tier=TIER_HIGH,
        )
        if action_record.tier != TIER_HIGH or action_record.status != STATUS_PENDING:
            raise GmailTriageError("Gate high/pending não foi aplicado.")

        # The direct user request itself is the approval event.
        self.approval_store.update_status(action_record.id, STATUS_APPROVED)
        try:
            if action == _ACTION_ARCHIVE:
                self.connector.archive_message(item["message_id"])
                labels = self.connector.get_message_labels(item["message_id"])
                verified = "INBOX" not in labels
                item_status = _ITEM_ARCHIVED
                success = "arquivado"
            else:
                self.connector.delete_message(item["message_id"])
                labels = self.connector.get_message_labels(item["message_id"])
                verified = "TRASH" in labels
                item_status = _ITEM_TRASHED
                success = "movido para a lixeira"
        except Exception:
            self.approval_store.update_status(action_record.id, "execution_failed")
            self.store.mark_item_at(
                session,
                item["position"],
                status=_ITEM_ERROR,
                action=action,
                verified=False,
            )
            raise

        if not verified:
            self.approval_store.update_status(action_record.id, "verification_failed")
            self.store.mark_item_at(
                session,
                item["position"],
                status=_ITEM_ERROR,
                action=action,
                verified=False,
            )
            return False, "A validação do estado final no Gmail falhou."

        self.approval_store.update_status(action_record.id, STATUS_EXECUTED)
        self.store.mark_item_at(
            session,
            item["position"],
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
        return True, success

    def handle_targeted_user_action(
        self,
        command: str,
        *,
        fingerprint: str,
    ) -> Dict[str, Any]:
        session = self.store.active_session(self.session_key)
        if session is None:
            raise NoActiveTriage("Nenhuma triagem Gmail ativa.")
        action = classify_action_intent(command)
        if action is None:
            raise GmailTriageError("Ação direta não reconhecida.")

        matches = self._match_snapshot_items(session, command, pending_only=True)
        if not matches:
            response = (
                "Não encontrei no snapshot atual nenhuma mensagem que corresponda "
                "à referência informada. Nenhuma alteração foi feita."
            )
            self.store.remember_response(
                session, fingerprint=fingerprint, response=response
            )
            return {"status": session.status, "verified": False, "response": response}

        completed = 0
        for item in matches:
            verified, message = self._execute_item_action(session, item, action)
            if not verified:
                if completed == 0:
                    progress = "Nenhuma ação foi confirmada como concluída."
                else:
                    progress = (
                        f"{completed} de {len(matches)} ações tiveram resultado "
                        "confirmado antes da falha."
                    )
                response = (
                    f"{progress} {message} "
                    "A execução foi interrompida sem declarar sucesso."
                )
                current_session = self.store.reposition_to_next_pending(session)
                self.store.remember_response(
                    current_session, fingerprint=fingerprint, response=response
                )
                return {
                    "status": current_session.status,
                    "verified": False,
                    "completed": completed,
                    "matched": len(matches),
                    "response": response,
                }
            completed += 1

        next_session = self.store.reposition_to_next_pending(session)
        noun = "mensagem" if completed == 1 else "mensagens"
        if action == _ACTION_ARCHIVE:
            verb = "arquivada" if completed == 1 else "arquivadas"
        else:
            verb = "movida para a lixeira" if completed == 1 else "movidas para a lixeira"
        verified_word = "verificada" if completed == 1 else "verificadas"
        response = f"{completed} {noun} {verb} e {verified_word} no Gmail."
        if next_session.status == _STATUS_ACTIVE:
            response += "\n\n" + self.render_current(self.current())
        else:
            response += f"\n\nTriagem concluída: {session.total} de {session.total}."
        self.store.remember_response(
            next_session, fingerprint=fingerprint, response=response
        )
        return {
            "status": next_session.status,
            "verified": True,
            "completed": completed,
            "matched": len(matches),
            "response": response,
        }

    def handle_snapshot_query(
        self,
        text: str,
        *,
        fingerprint: str,
    ) -> Dict[str, Any]:
        session = self.store.active_session(self.session_key)
        if session is None:
            raise NoActiveTriage("Nenhuma triagem Gmail ativa.")
        query_type = classify_snapshot_query(text)
        if query_type is None:
            raise GmailTriageError("Consulta do snapshot não reconhecida.")

        pending = self.store.all_items(session, pending_only=True)
        if query_type == "remaining_count":
            response = f"Restam {len(pending)} mensagens no snapshot atual."
        elif query_type == "remaining_list":
            items = self._ensure_snapshot_metadata(session)
            items = [item for item in items if item["status"] == _ITEM_PENDING]
            lines = [f"Restam {len(items)} mensagens no snapshot atual:"]
            for item in items[:20]:
                sender = parseaddr(item.get("sender", ""))[0] or item.get("sender", "")
                lines.append(
                    f"- {sender or '(remetente não informado)'} — "
                    f"{item.get('subject') or '(sem assunto)'}"
                )
            if len(items) > 20:
                lines.append(f"- ... e mais {len(items) - 20}.")
            response = "\n".join(lines)
        else:
            matches = self._match_snapshot_items(session, text, pending_only=False)
            if not matches:
                response = (
                    "Não encontrei no snapshot atual nenhuma mensagem que corresponda "
                    "à referência informada."
                )
            elif len(matches) > 1:
                lines = [
                    f"Encontrei {len(matches)} mensagens correspondentes. "
                    "Para evitar agir ou descrever a mensagem errada:"
                ]
                for item in matches[:10]:
                    sender = parseaddr(item.get("sender", ""))[0] or item.get("sender", "")
                    lines.append(
                        f"- {sender or '(remetente não informado)'} — "
                        f"{item.get('subject') or '(sem assunto)'}"
                    )
                response = "\n".join(lines)
            else:
                item = matches[0]
                full = self.connector.get_message_content(item["message_id"])
                body, truncated = _compact(full.get("body", ""), _DETAIL_BODY_CHARS)
                response = "\n".join(
                    [
                        f"Remetente: {full.get('sender') or '(não informado)'}",
                        f"Assunto: {full.get('subject') or '(sem assunto)'}",
                        f"Conteúdo: {body or '(sem conteúdo textual)'}",
                    ]
                )
                if truncated:
                    response += "\nO conteúdo foi abreviado."
        self.store.remember_response(
            session, fingerprint=fingerprint, response=response
        )
        return {"status": session.status, "response": response}

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

        verified, message = self._execute_item_action(session, item, action)
        if not verified:
            response = (
                f"{message} A triagem permaneceu neste e-mail e nenhuma "
                "confirmação de sucesso foi emitida."
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

        next_session = self.store.reposition_to_next_pending(session)
        success_text = (
            "Arquivado e verificado no Gmail."
            if action == _ACTION_ARCHIVE
            else "Movido para a lixeira e verificado no Gmail."
        )
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
        is_details = bool(current.get("details")) or details
        content = current.get("content") or (
            "(sem conteúdo textual)" if is_details else "(sem resumo disponível)"
        )
        lines = [
            f"{position} de {total}.",
            f"Remetente: {sender}",
            f"Assunto: {subject}",
            f"{'Conteúdo' if is_details else 'Resumo'}: {content}",
        ]
        if current.get("content_truncated") and is_details:
            lines.append("O conteúdo foi abreviado.")
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
    "classify_action_intent",
    "classify_direct_command",
    "classify_snapshot_query",
    "command_fingerprint",
    "infer_triage_query",
    "is_pending_email_approval_reference",
    "is_sequential_triage_request",
]
