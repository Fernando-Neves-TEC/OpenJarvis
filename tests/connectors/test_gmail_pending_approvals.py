from __future__ import annotations

from pathlib import Path

from openjarvis.connectors.gmail_triage import (
    GmailTriageService,
    GmailTriageStore,
    is_pending_email_approval_reference,
)
from openjarvis.tools.approval_store import ApprovalStore
from tests.connectors.test_gmail_triage_transcript_regression import TranscriptConnector


def make_service(tmp_path: Path, connector=None):
    connector = connector or TranscriptConnector()
    triage = GmailTriageStore(str(tmp_path / "triage.db"))
    approvals = ApprovalStore(str(tmp_path / "approvals.db"))
    service = GmailTriageService(
        session_key="chat",
        connector=connector,
        store=triage,
        approval_store=approvals,
    )
    return service, connector, triage, approvals


def queue(approvals, message_id, action_type="email_archive", sender="", subject=""):
    return approvals.queue_action(
        action_type=action_type,
        description="Ação proposta pelo agente",
        payload={
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
        },
        permission_key=f"{action_type}:message:{message_id}",
        tier="high",
    )


def test_pending_reference_recognizes_openjarvis_bell_language():
    assert is_pending_email_approval_reference(
        "Você viu que tem três e-mails aqui aguardando para serem arquivados?"
    )
    assert is_pending_email_approval_reference(
        "Eles ainda aparecem no sininho como aguardando aprovação."
    )
    assert not is_pending_email_approval_reference(
        "Gostaria de revisar minha caixa de e-mail."
    )


def test_pending_list_is_grounded_and_never_reads_bodies(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        queue(approvals, "v1")
        queue(approvals, "m1")

        result = svc.render_pending_email_approvals()

        assert result["count"] == 2
        assert "Vercel" in result["response"]
        assert "Metricool" in result["response"]
        assert "message_id" not in result["response"]
        assert connector.body_calls == []
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_collective_direct_archive_executes_pending_and_verifies(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        queue(approvals, "v1", sender="Vercel", subject="First")
        queue(approvals, "v2", sender="Vercel", subject="Second")
        queue(approvals, "m1", sender="Metricool", subject="Third")

        result = svc.handle_pending_email_approval_action(
            "Pode arquivar os três pendentes."
        )

        assert result["verified"] is True
        assert result["completed"] == 3
        assert set(connector.archive_calls) == {"v1", "v2", "m1"}
        assert approvals.list_pending() == []
        assert all(
            "INBOX" not in connector.get_message_labels(mid)
            for mid in ("v1", "v2", "m1")
        )
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_mixed_pending_actions_are_not_mutated(tmp_path):
    svc, connector, triage, approvals = make_service(tmp_path)
    try:
        queue(approvals, "v1", action_type="email_archive")
        queue(approvals, "m1", action_type="email_delete")

        result = svc.handle_pending_email_approval_action(
            "Pode arquivar todos os pendentes."
        )

        assert result["verified"] is False
        assert connector.archive_calls == []
        assert connector.trash_calls == []
        assert len(approvals.list_pending()) == 2
    finally:
        svc.close()
        triage.close()
        approvals.close()


def test_verification_failure_stays_pending_and_never_claims_success(tmp_path):
    class BrokenConnector(TranscriptConnector):
        def archive_message(self, msg_id):
            self.archive_calls.append(msg_id)

    connector = BrokenConnector()
    svc, connector, triage, approvals = make_service(tmp_path, connector)
    try:
        queue(approvals, "v1", sender="Vercel", subject="First")

        result = svc.handle_pending_email_approval_action(
            "Pode arquivar esse aviso pendente."
        )

        assert result["verified"] is False
        assert result["completed"] == 0
        assert len(approvals.list_pending()) == 1
        assert "não foi validada" in result["response"]
        assert "arquivado e verificado" not in result["response"]
    finally:
        svc.close()
        triage.close()
        approvals.close()
