from __future__ import annotations

import pytest

from openjarvis.server.models import ChatCompletionRequest, ChatMessage
from openjarvis.server.routes import _maybe_handle_gmail_triage
from tests.server.test_gmail_triage_routes import FakeTriageService


@pytest.fixture(autouse=True)
def _patch_triage_service(monkeypatch):
    FakeTriageService.instances = []
    FakeTriageService.active = True
    FakeTriageService.cached = None
    monkeypatch.setattr(
        "openjarvis.connectors.gmail_triage.GmailTriageService",
        FakeTriageService,
    )


@pytest.mark.asyncio
async def test_general_mailbox_review_bypasses_llm():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(
                role="user",
                content="Gostaria que você revisasse a minha caixa de e-mail",
            )
        ],
        stream=False,
        conversation_id="transcript-review",
    )
    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    svc = FakeTriageService.instances[-1]
    assert svc.started == [("in:inbox", None)]


@pytest.mark.asyncio
async def test_vercel_targeted_archive_bypasses_llm():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(role="assistant", content="1 de 10."),
            ChatMessage(
                role="user",
                content="Ok, as da Vercel podem arquivar para mim",
            ),
        ],
        stream=False,
        conversation_id="transcript-actions",
    )
    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    svc = FakeTriageService.instances[-1]
    assert [x[0] for x in svc.targeted_commands] == [
        "Ok, as da Vercel podem arquivar para mim"
    ]


@pytest.mark.asyncio
async def test_metricool_targeted_delete_bypasses_llm():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(role="assistant", content="3 de 10."),
            ChatMessage(role="user", content="A mensagem da Metricool pode excluir."),
        ],
        stream=False,
        conversation_id="transcript-actions",
    )
    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    svc = FakeTriageService.instances[-1]
    assert [x[0] for x in svc.targeted_commands] == [
        "A mensagem da Metricool pode excluir."
    ]


@pytest.mark.asyncio
async def test_snapshot_remaining_count_bypasses_llm():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(role="assistant", content="3 de 10."),
            ChatMessage(role="user", content="Quantas restam agora?"),
        ],
        stream=False,
        conversation_id="transcript-query",
    )
    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    assert "Restam 3 mensagens" in response.choices[0].message.content
    svc = FakeTriageService.instances[-1]
    assert len(svc.snapshot_queries) == 1


@pytest.mark.asyncio
async def test_nonexistent_group_details_bypasses_llm():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(role="assistant", content="3 de 10."),
            ChatMessage(
                role="user",
                content="Você pode me dar detalhes dessa do Grupo Juliani?",
            ),
        ],
        stream=False,
        conversation_id="transcript-query",
    )
    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    assert "Não encontrei no snapshot atual" in response.choices[0].message.content
    svc = FakeTriageService.instances[-1]
    assert len(svc.snapshot_queries) == 1


@pytest.mark.asyncio
async def test_pending_bell_question_bypasses_llm_without_starting_triage():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(
                role="user",
                content=(
                    "Você viu que tem três e-mails aqui aguardando para serem "
                    "arquivados ou excluídos?"
                ),
            )
        ],
        stream=False,
        conversation_id="pending-bell",
    )

    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    assert "3 e-mails aguardando aprovação" in response.choices[0].message.content
    svc = FakeTriageService.instances[-1]
    assert svc.pending_queries == [True]
    assert svc.started == []


@pytest.mark.asyncio
async def test_collective_pending_archive_bypasses_llm_and_needs_no_ids():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(
                role="user",
                content="Pode arquivar os três e-mails pendentes do sininho.",
            )
        ],
        stream=False,
        conversation_id="pending-bell",
    )

    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    assert "arquivados e verificados" in response.choices[0].message.content
    svc = FakeTriageService.instances[-1]
    assert svc.pending_actions == [
        "Pode arquivar os três e-mails pendentes do sininho."
    ]
