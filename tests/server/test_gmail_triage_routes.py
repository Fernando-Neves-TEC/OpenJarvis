from __future__ import annotations

import pytest
from fastapi.responses import StreamingResponse

from openjarvis.server.models import ChatCompletionRequest, ChatMessage
from openjarvis.server.routes import _maybe_handle_gmail_triage


class FakeTriageService:
    instances = []
    active = True
    cached = None

    def __init__(self, *, session_key="default"):
        self.session_key = session_key
        self.started = []
        self.commands = []
        self.targeted_commands = []
        self.snapshot_queries = []
        self.closed = False
        type(self).instances.append(self)

    def start(self, *, query, max_results=None):
        self.started.append((query, max_results))
        return {
            "status": "active",
            "position": 1,
            "total": 4,
            "sender": "Sender <sender@example.com>",
            "subject": "Subject",
            "date": "Today",
            "content": "Body",
            "content_truncated": False,
            "suggestion": None,
        }

    def render_current(self, current):
        return "1 de 4.\nRemetente: Sender\nAssunto: Subject\nConteúdo: Body"

    def has_active(self):
        return type(self).active

    def cached_response(self, fingerprint):
        return type(self).cached

    def handle_direct_user_command(self, command, *, fingerprint):
        self.commands.append((command, fingerprint))
        return {
            "response": "Arquivado e verificado no Gmail.\n\n2 de 4.",
            "verified": True,
        }

    def handle_targeted_user_action(self, command, *, fingerprint):
        self.targeted_commands.append((command, fingerprint))
        return {
            "response": "2 mensagens arquivadas e verificadas no Gmail.\n\n3 de 4.",
            "verified": True,
        }

    def handle_snapshot_query(self, text, *, fingerprint):
        self.snapshot_queries.append((text, fingerprint))
        if "Grupo Juliani" in text:
            response = "Não encontrei no snapshot atual nenhuma mensagem correspondente."
        elif "Quantas" in text:
            response = "Restam 3 mensagens no snapshot atual."
        else:
            response = "Restam 3 mensagens no snapshot atual: Vercel — Subject."
        return {"response": response}

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    FakeTriageService.instances = []
    FakeTriageService.active = True
    FakeTriageService.cached = None
    monkeypatch.setattr(
        "openjarvis.connectors.gmail_triage.GmailTriageService",
        FakeTriageService,
    )


@pytest.mark.asyncio
async def test_sequential_request_bypasses_llm_with_zero_token_response():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(
                role="user",
                content=(
                    "Veja minha caixa de email e apresente cada um por vez "
                    "para eu decidir o que fazer"
                ),
            )
        ],
        stream=False,
        conversation_id="conv-a",
    )

    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.total_tokens == 0
    assert response.choices[0].message.content.startswith("1 de 4.")
    svc = FakeTriageService.instances[-1]
    assert svc.session_key == "conv-a"
    assert svc.started == [("in:inbox", None)]
    assert svc.closed is True


@pytest.mark.asyncio
async def test_unread_request_uses_unread_snapshot():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(
                role="user",
                content="Revise meus emails não lidos um por vez",
            )
        ],
    )

    await _maybe_handle_gmail_triage(req)

    svc = FakeTriageService.instances[-1]
    assert svc.started == [("in:inbox is:unread", None)]


@pytest.mark.asyncio
async def test_direct_archive_is_handled_without_llm():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(role="user", content="triagem anterior"),
            ChatMessage(role="assistant", content="1 de 4"),
            ChatMessage(role="user", content="arquivar"),
        ],
        stream=False,
    )

    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert response.usage.prompt_tokens == 0
    assert response.usage.completion_tokens == 0
    assert "Arquivado e verificado" in response.choices[0].message.content
    svc = FakeTriageService.instances[-1]
    assert len(svc.commands) == 1
    assert svc.commands[0][0] == "arquivar"


@pytest.mark.asyncio
async def test_ambiguous_sentence_is_not_intercepted():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(
                role="user",
                content="acho que talvez seja melhor arquivar este, o que você acha?",
            )
        ],
    )

    response = await _maybe_handle_gmail_triage(req)

    assert response is None
    assert FakeTriageService.instances == []


@pytest.mark.asyncio
async def test_retry_replays_cached_response_even_when_session_completed():
    FakeTriageService.active = False
    FakeTriageService.cached = "Resposta já confirmada anteriormente."
    req = ChatCompletionRequest(
        model="local",
        messages=[ChatMessage(role="user", content="arquivar")],
    )

    response = await _maybe_handle_gmail_triage(req)

    assert response is not None
    assert (
        response.choices[0].message.content
        == "Resposta já confirmada anteriormente."
    )
    svc = FakeTriageService.instances[-1]
    assert svc.commands == []


@pytest.mark.asyncio
async def test_streaming_direct_action_reports_zero_usage():
    req = ChatCompletionRequest(
        model="local",
        messages=[
            ChatMessage(role="assistant", content="1 de 4"),
            ChatMessage(role="user", content="lixeira"),
        ],
        stream=True,
    )

    response = await _maybe_handle_gmail_triage(req)

    assert isinstance(response, StreamingResponse)
    chunks = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        chunks.append(chunk)
    body = "".join(chunks)
    assert "Movido" not in body  # fake returns archive wording; only structure matters.
    assert '"prompt_tokens": 0' in body
    assert '"completion_tokens": 0' in body
    assert '"gmail_triage": true' in body
    assert "data: [DONE]" in body
