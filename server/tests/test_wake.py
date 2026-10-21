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
    ("Oracle. Agatha, para.", "stop"),  # sobra de eco antes do nome
    ("do banco Agatha chega", "stop"),
    ("Agatha, Paras", "stop"),  # o Whisper em 21/10
    ("Agatha, espera.", "stop"),
    ("Agatha, espera que eu vou ver uma coisa", "question"),
])
def test_tipo_de_interrupcao(text, kind):
    from agatha_server.wake import interruption_kind

    assert interruption_kind(text, CONFIG) == kind


@pytest.mark.parametrize("text, expected", [
    ("Oracle. Agatha, e o host do banco?", "Agatha, e o host do banco?"),
    ("Ok, Agatha, para", "Agatha, para"),
    ("Para.", "Para."),
    ("a tablespace users está cheia, Agatha", "a tablespace users está cheia, Agatha"),  # nome só no fim
])
def test_texto_a_partir_do_nome(text, expected):
    from agatha_server.wake import from_name

    assert from_name(text, CONFIG) == expected


@pytest.mark.parametrize("text, expected", [
    ("Agatha, para.", True),
    ("Agatha, pode parar", True),
    ("Agatha, chega por hoje", True),
    ("Agatha, par", True),  # "para" cortado pelo Whisper
    ("Par.", True),
    ("Agatha, espera", True),
    ("Agatha!", False),  # só o nome: é chamá-la
    ("Agatha, esquece isso", False),  # memória (ADR-010)
    ("Agatha, deixa pra lá", False),
    ("Agatha, para que serve um backup?", False),
])
def test_parar_com_ela_calada(text, expected):
    from agatha_server.wake import is_idle_stop

    assert is_idle_stop(text, CONFIG) is expected
