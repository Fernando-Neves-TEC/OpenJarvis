from __future__ import annotations

from pathlib import Path

from openjarvis.connectors.gmail_triage import (
    GmailTriageService,
    GmailTriageStore,
    classify_action_intent,
    classify_snapshot_query,
    is_sequential_triage_request,
)
from openjarvis.tools.approval_store import ApprovalStore


class TranscriptConnector:
    def __init__(self):
        self.items = [
            {
                "message_id": "v1",
                "thread_id": "tv1",
                "sender": "Vercel <notifications@vercel.com>",
                "subject": "Preview deployment failed for moveplus-clinic-hub",
                "date": "Today",
                "snippet": "Preview deployment failed",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
            {
                "message_id": "v2",
                "thread_id": "tv2",
                "sender": "Vercel <notifications@vercel.com>",
                "subject": "2 deployments failed for chore/reconcile",
                "date": "Today",
                "snippet": "Two deployments failed",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
            {
                "message_id": "m1",
                "thread_id": "tm1",
                "sender": "Metricool <hello@metricool.com>",
                "subject": "Your audience has a lot to say",
                "date": "Today",
                "snippet": "Marketing update",
                "labels": ["INBOX", "UNREAD", "CATEGORY_PROMOTIONS"],
            },
            {
                "message_id": "g1",
                "thread_id": "tg1",
                "sender": "Google <no-reply@accounts.google.com>",
                "subject": "Alerta de segurança para sua conta",
                "date": "Today",
                "snippet": "Security alert",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
            {
                "message_id": "pr1",
                "thread_id": "tpr1",
                "sender": "GitHub <notifications@github.com>",
                "subject": "Pull request updated in old repository",
                "date": "Today",
                "snippet": "Pull request notification",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
            {
                "message_id": "c1",
                "thread_id": "tc1",
                "sender": "Claro <fatura@claro.com.br>",
                "subject": "Sua Fatura Digital Claro chegou",
                "date": "Today",
                "snippet": "Fatura digital",
                "labels": ["INBOX", "UNREAD", "CATEGORY_UPDATES"],
            },
        ]
        self.labels = {
            item["message_id"]: list(item["labels"]) for item in self.items
        }
        self.archive_calls: list[str] = []
        self.trash_calls: list[str] = []
        self.body_calls: list[str] = []

    def list_message_stubs(self, *, query="", max_results=None):
        items = self.items if max_results is None else self.items[:max_results]
        return [
            {"message_id": item["message_id"], "thread_id": item["thread_id"]}
            for item in items
        ]

    def get_message_metadata(self, msg_id):
        item = next(x for x in self.items if x["message_id"] == msg_id)
        return {**item, "labels": list(self.labels[msg_id])}

    def get_message_content(self, msg_id):
        self.body_calls.append(msg_id)
        item = next(x for x in self.items if x["message_id"] == msg_id)
        return {
            **item,
            "body": f"body for {item['subject']}",
            "labels": list(self.labels[msg_id]),
        }

    def get_message_labels(self, msg_id):
        return list(self.labels[msg_id])

    def archive_message(self, msg_id):
        self.archive_calls.append(msg_id)
        self.labels[msg_id] = [x for x in self.labels[msg_id] if x != "INBOX"]

    def delete_message(self, msg_id):
        self.trash_calls.append(msg_id)
        if "TRASH" not in self.labels[msg_id]:
            self.labels[msg_id].append("TRASH")


def make_service(tmp_path: Path):
    connector = TranscriptConnector()
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    service = GmailTriageService(
        session_key="chat",
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    service.start(query="in:inbox is:unread")
    return service, connector, triage, approvals


def test_general_review_request_enters_deterministic_triage():
    assert (
        is_sequential_triage_request(
            "Gostaria que você revisasse a minha caixa de e-mail"
        )
        is True
    )


def test_transcript_targeted_vercel_archive_executes_without_second_approval(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        command = "Ok, as da Vercel podem arquivar para mim"
        assert classify_action_intent(command) == "archive"
        result = svc.handle_targeted_user_action(command, fingerprint="v")
        assert result["verified"] is True
        assert result["completed"] == 2
        assert connector.archive_calls == ["v1", "v2"]
        assert "INBOX" not in connector.labels["v1"]
        assert "INBOX" not in connector.labels["v2"]
        rows = approvals._conn.execute(
            "SELECT tier,status FROM pending_actions ORDER BY rowid"
        ).fetchall()
        assert rows == [("high", "executed"), ("high", "executed")]
        assert approvals.list_pending() == []
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_transcript_metricool_trash_executes_and_verifies(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        result = svc.handle_targeted_user_action(
            "A mensagem da Metricool pode excluir.",
            fingerprint="m",
        )
        assert result["verified"] is True
        assert result["completed"] == 1
        assert connector.trash_calls == ["m1"]
        assert "TRASH" in connector.labels["m1"]
        assert approvals.list_pending() == []
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_transcript_google_and_pull_request_batch_are_grounded(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        result = svc.handle_targeted_user_action(
            "As mensagens do pull request, as mensagens do Google, "
            "esses todos podem, você pode excluir, tá?",
            fingerprint="batch",
        )
        assert result["verified"] is True
        assert result["completed"] == 2
        assert set(connector.trash_calls) == {"g1", "pr1"}
        assert approvals.list_pending() == []
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_snapshot_count_and_list_are_factual_without_llm(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        assert classify_snapshot_query("Quantas restam agora?") == "remaining_count"
        count = svc.handle_snapshot_query("Quantas restam agora?", fingerprint="count")
        assert "Restam 6 mensagens" in count["response"]

        listed = svc.handle_snapshot_query(
            "Jarvis, quais outras mensagens que eu tenho?",
            fingerprint="list",
        )
        assert "Vercel" in listed["response"]
        assert "Metricool" in listed["response"]
        assert "Claro" in listed["response"]
        assert "Grupo Juliani" not in listed["response"]
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_nonexistent_group_reference_cannot_be_invented(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        response = svc.handle_snapshot_query(
            "Você pode me dar detalhes dessa do Grupo Juliani?",
            fingerprint="missing",
        )["response"]
        assert "Não encontrei no snapshot atual" in response
        assert connector.archive_calls == []
        assert connector.trash_calls == []
        assert approvals.list_pending() == []
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_verification_failure_never_reports_success_for_targeted_action(tmp_path):
    class BrokenConnector(TranscriptConnector):
        def archive_message(self, msg_id):
            self.archive_calls.append(msg_id)
            # Simulate Gmail/API failure to produce the expected final label.

    connector = BrokenConnector()
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    svc = GmailTriageService(
        session_key="chat",
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    try:
        svc.start(query="in:inbox is:unread")
        result = svc.handle_targeted_user_action(
            "As da Vercel podem arquivar para mim",
            fingerprint="broken",
        )
        assert result["verified"] is False
        assert result["completed"] == 0
        assert "sem declarar sucesso" in result["response"]
        assert "verificad" not in result["response"].lower()
    finally:
        svc.close()
        triage.close()
        approvals.close()
