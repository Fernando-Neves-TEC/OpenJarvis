from openjarvis.connectors.gmail_triage import (
    classify_action_intent,
    is_sequential_triage_request,
)


def test_verdade_does_not_match_ver_and_start_triage():
    text = (
        "Na verdade, o que eu estou falando é aqui na interface do OpenJarvis, "
        "não é do Gmail não, aqui na interface do OpenJarvis que estavam os três "
        "comunicados aqui desses e-mails aguardando para ser arquivado."
    )
    assert is_sequential_triage_request(text) is False


def test_archivado_is_not_an_archive_command():
    text = (
        "Eu ainda estou vendo eles aqui no aviso aguardando a aprovação, "
        "porque eles ainda aparecem como aguardando depois de arquivado?"
    )
    assert classify_action_intent(text) is None


def test_real_review_ver_still_matches():
    assert is_sequential_triage_request(
        "Quero ver os meus e-mails da caixa de entrada"
    ) is True


def test_real_archive_command_still_matches():
    assert classify_action_intent(
        "Pode arquivar as mensagens da Vercel para mim"
    ) == "archive"
