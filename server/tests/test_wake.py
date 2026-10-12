import pytest

from agatha_server.wake import WakeConfig, is_addressed

CONFIG = WakeConfig()


@pytest.mark.parametrize("text", [
    "Agatha, que horas são?",
    "Certo, Agatha. Está escutando?",
    "Ok, Agatha, aqui você acha que é para que serve um item?",
    "Ei Ágata, me ajuda aqui",          # sem acento/variação
    "Ok, muito obrigado, Agatha.",       # no fim
    "Agatha consegue me validar o horário?",
    "Agatha-srv.",                      # o Whisper juntou o nome a um termo do vocabulário
])
def test_chamou_a_agatha(text):
    assert is_addressed(text, CONFIG)


@pytest.mark.parametrize("text", [
    "Que horas são agora?",
    "O livro novo da Agatha Christie fala de um crime num trem que atravessa a Europa inteira",
    "",
    "agathas",  # não é o nome
])
def test_nao_chamou(text):
    assert not is_addressed(text, CONFIG)


def test_palavras_configuraveis():
    assert is_addressed("Jarvis, abre o painel", WakeConfig(words=("jarvis",)))
    assert not is_addressed("Agatha, abre o painel", WakeConfig(words=("jarvis",)))


@pytest.mark.parametrize("text, kind", [
    ("Agatha, para.", "stop"),
    ("Agatha!", "stop"),
    ("Agatha, chega por hoje", "stop"),
    ("Agatha, pode parar", "stop"),
    ("Agatha, cala a boca", "stop"),
    ("Para.", "stop"),
    ("Agatha, para que serve um backup?", "question"),
    ("Agatha, e o host do banco?", "question"),
    ("Agatha, pode me explicar o que é um trigger?", "question"),
])
def test_tipo_de_interrupcao(text, kind):
    from agatha_server.wake import interruption_kind

    assert interruption_kind(text, CONFIG) == kind
