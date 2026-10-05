from __future__ import annotations

import json
from pathlib import Path

import pytest

from openjarvis.connectors.gmail_triage import (
    GmailTriageService,
    GmailTriageStore,
    classify_direct_command,
    command_fingerprint,
    infer_triage_query,
    is_sequential_triage_request,
)
from openjarvis.tools.approval_store import ApprovalStore


class FakeConnector:
    def __init__(self, items=None):
        self.items = items or [
            {
                "message_id": "m1",
                "thread_id": "t1",
                "sender": "Alerts <alerts@example.com>",
                "subject": "First",
                "date": "Mon",
                "snippet": "first snippet",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
            {
                "message_id": "m2",
                "thread_id": "t2",
                "sender": "News <news@example.com>",
                "subject": "Second",
                "date": "Tue",
                "snippet": "second snippet",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
            {
                "message_id": "m3",
                "thread_id": "t3",
                "sender": "Promo <promo@example.net>",
                "subject": "Third",
                "date": "Wed",
                "snippet": "third snippet",
                "labels": ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"],
            },
        ]
        self.labels = {
            item["message_id"]: list(item["labels"]) for item in self.items
        }
        self.metadata_calls = 0
        self.body_calls = []
        self.archive_calls = []
        self.trash_calls = []

    def list_message_metadata(self, *, query="", max_results=20):
        self.metadata_calls += 1
        return [dict(item) for item in self.items[:max_results]]

    def get_message_content(self, msg_id):
        self.body_calls.append(msg_id)
        item = next(i for i in self.items if i["message_id"] == msg_id)
        return {
            **item,
            "body": f"full body for {msg_id}",
            "labels": list(self.labels[msg_id]),
        }

    def get_message_labels(self, msg_id):
        return list(self.labels[msg_id])

    def archive_message(self, msg_id):
        self.archive_calls.append(msg_id)
        self.labels[msg_id] = [
            label for label in self.labels[msg_id] if label != "INBOX"
        ]

    def delete_message(self, msg_id):
        self.trash_calls.append(msg_id)
        if "TRASH" not in self.labels[msg_id]:
            self.labels[msg_id].append("TRASH")


@pytest.fixture()
def service(tmp_path: Path):
    connector = FakeConnector()
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    svc = GmailTriageService(
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    yield svc, connector, triage, approvals
    triage.close()
    approvals.close()


def test_start_creates_fixed_snapshot_and_fetches_only_current_body(service):
    svc, connector, _, _ = service

    current = svc.start(query="is:unread", max_results=20)

    assert current["position"] == 1
    assert current["total"] == 3
    assert current["remaining"] == 2
    assert current["subject"] == "First"
    assert connector.metadata_calls == 1
    assert connector.body_calls == ["m1"]

    rendered = svc.render_current(current)
    assert "1 de 3." in rendered
    assert "ID:" not in rendered
    assert "message_id" not in rendered
    assert "Decisão: arquivar, lixeira, pular ou mais detalhes." in rendered


def test_direct_archive_is_high_approved_executed_verified_and_advances(service):
    svc, connector, triage, approvals = service
    svc.start(query="is:unread")

    result = svc.handle_direct_user_command("arquivar", fingerprint="fp-1")

    assert result["verified"] is True
    assert result["approval_tier"] == "high"
    assert result["approval_status"] == "executed"
    assert connector.archive_calls == ["m1"]
    assert "INBOX" not in connector.labels["m1"]
    assert "Arquivado e verificado no Gmail." in result["response"]
    assert "2 de 3." in result["response"]

    session = triage.active_session("default")
    assert session is not None
    assert session.current_position == 1

    row = approvals._conn.execute(
        "SELECT action_type, tier, status, payload FROM pending_actions"
    ).fetchone()
    assert row[0] == "email_archive"
    assert row[1] == "high"
    assert row[2] == "executed"
    payload = json.loads(row[3])
    assert payload["origin"] == "USER_DIRECT"
    assert payload["message_id"] == "m1"


def test_direct_trash_is_verified_before_advance(service):
    svc, connector, triage, _ = service
    svc.start(query="is:unread")

    result = svc.handle_direct_user_command("lixeira", fingerprint="fp-trash")

    assert result["verified"] is True
    assert connector.trash_calls == ["m1"]
    assert "TRASH" in connector.labels["m1"]
    assert triage.active_session("default").current_position == 1


def test_duplicate_request_does_not_apply_same_command_to_next_email(service):
    svc, connector, triage, _ = service
    svc.start(query="is:unread")

    first = svc.handle_direct_user_command("arquivar", fingerprint="same-request")
    second = svc.handle_direct_user_command("arquivar", fingerprint="same-request")

    assert first["verified"] is True
    assert second["duplicate"] is True
    assert second["response"] == first["response"]
    assert connector.archive_calls == ["m1"]
    assert triage.active_session("default").current_position == 1


def test_duplicate_last_action_replays_after_session_completed(tmp_path: Path):
    connector = FakeConnector(items=[
        {
            "message_id": "only",
            "thread_id": "t",
            "sender": "One <one@example.com>",
            "subject": "Only",
            "date": "Mon",
            "snippet": "only",
            "labels": ["INBOX", "UNREAD"],
        }
    ])
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    svc = GmailTriageService(
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    try:
        svc.start(query="is:unread")
        result = svc.handle_direct_user_command("arquivar", fingerprint="last-fp")
        assert result["status"] == "completed"
        assert svc.has_active() is False
        assert svc.cached_response("last-fp") == result["response"]
        assert connector.archive_calls == ["only"]
    finally:
        triage.close()
        approvals.close()


def test_more_details_does_not_advance_or_modify(service):
    svc, connector, triage, _ = service
    svc.start(query="is:unread")
    before = triage.active_session("default").current_position

    result = svc.handle_direct_user_command("mais detalhes", fingerprint="details")

    assert result["status"] == "active"
    assert triage.active_session("default").current_position == before
    assert connector.archive_calls == []
    assert connector.trash_calls == []
    assert connector.body_calls == ["m1", "m1"]


def test_skip_advances_and_records_behavior_without_gmail_mutation(service):
    svc, connector, triage, _ = service
    svc.start(query="is:unread")

    result = svc.handle_direct_user_command("pular", fingerprint="skip")

    assert result["verified"] is True
    assert connector.archive_calls == []
    assert connector.trash_calls == []
    assert triage.active_session("default").current_position == 1

    count = triage._conn.execute(
        "SELECT COUNT(*) FROM triage_learning_events WHERE action='skip'"
    ).fetchone()[0]
    assert count == 1


def test_behavioral_memory_suggests_but_does_not_execute(service):
    svc, connector, triage, _ = service
    svc.start(query="is:unread")
    session = triage.active_session("default")
    item = triage.current_item(session)

    for _ in range(3):
        triage.record_learning(
            session,
            item,
            action="archive",
            origin="USER_DIRECT",
        )

    current = svc.current()

    assert current["suggestion"]["action"] == "archive"
    assert current["suggestion"]["count"] == 3
    assert current["suggestion"]["confidence"] == 1.0
    assert connector.archive_calls == []
    assert connector.trash_calls == []


def test_verification_failure_does_not_advance_or_leave_approved(tmp_path: Path):
    class BrokenArchiveConnector(FakeConnector):
        def archive_message(self, msg_id):
            self.archive_calls.append(msg_id)
            # Deliberately leave INBOX present.

    connector = BrokenArchiveConnector()
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    svc = GmailTriageService(
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    try:
        svc.start(query="is:unread")
        result = svc.handle_direct_user_command("arquivar", fingerprint="verify-fail")

        assert result["verified"] is False
        assert triage.active_session("default").current_position == 0
        row = approvals._conn.execute(
            "SELECT status FROM pending_actions"
        ).fetchone()
        assert row[0] == "verification_failed"
        assert approvals.list_approved() == []
    finally:
        triage.close()
        approvals.close()


def test_intent_helpers_are_strict_and_query_scope_is_explicit():
    text = (
        "Bom dia Jarvis, dê uma olhada na minha caixa de email e me apresente "
        "cada um por vez para eu decidir"
    )
    assert is_sequential_triage_request(text) is True
    assert infer_triage_query(text) == "in:inbox"
    assert infer_triage_query("mostre meus emails não lidos um por vez") == "is:unread"

    assert classify_direct_command("arquivar") == "archive"
    assert classify_direct_command("mover para lixeira") == "trash"
    assert classify_direct_command("mais detalhes") == "details"
    assert classify_direct_command("acho que talvez seja melhor arquivar") is None


def test_sessions_are_isolated_by_conversation_id(tmp_path: Path):
    connector = FakeConnector()
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    service_a = GmailTriageService(
        session_key="conversation-A",
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    service_b = GmailTriageService(
        session_key="conversation-B",
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    try:
        service_a.start(query="is:unread")
        assert service_a.has_active() is True
        assert service_b.has_active() is False
        with pytest.raises(Exception):
            service_b.handle_direct_user_command("arquivar", fingerprint="b")
        assert connector.archive_calls == []
        assert triage.active_session("conversation-A").current_position == 0
    finally:
        triage.close()
        approvals.close()


def test_command_fingerprint_changes_when_conversation_advances():
    class M:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    a = [M("user", "arquivar")]
    b = [M("user", "arquivar"), M("assistant", "ok"), M("user", "arquivar")]
    assert command_fingerprint(a) != command_fingerprint(b)
