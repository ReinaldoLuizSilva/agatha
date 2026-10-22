import pytest

from agatha_server.text import Chunker, LeadingTimestampFilter, clean, looks_incomplete, normalize_for_speech


def stream(text: str, chunker: Chunker | None = None, step: int = 3) -> list[str]:
    """Alimenta o chunker em pedaços pequenos, como tokens do LLM."""
    chunker = chunker or Chunker()
    out = []
    for i in range(0, len(text), step):
        out += chunker.feed(text[i : i + step])
    return out + chunker.flush()


# --- Chunker ---------------------------------------------------------------------------

def test_divide_por_frase():
    assert stream("Oi, Rei. Tudo certo por aqui! E você?") == ["Oi, Rei.", "Tudo certo por aqui!", "E você?"]


def test_primeiro_trecho_corta_na_primeira_virgula_depois_de_15_caracteres():
    text = "Para rodar o Zabbix, você precisa de um servidor Linux e de um banco de dados."
    assert stream(text) == ["Para rodar o Zabbix,", "você precisa de um servidor Linux e de um banco de dados."]


def test_virgula_cedo_nao_corta():
    assert stream("Oi, Rei. Tudo bem.") == ["Oi, Rei.", "Tudo bem."]


def test_depois_do_primeiro_trecho_virgula_so_corta_em_trecho_longo():
    text = "Claro. Primeiro veja o serviço, depois os logs, e por fim a rede."
    assert stream(text) == ["Claro.", "Primeiro veja o serviço, depois os logs, e por fim a rede."]


def test_nao_corta_em_ip_nem_decimal():
    assert stream("O IP é 192.168.1.25 e o uso é 2,5 vezes maior. Ok.") == [
        "O IP é 192.168.1.25 e o uso é 2,5 vezes maior.", "Ok."
    ]


def test_quebra_de_linha_corta():
    assert stream("Primeiro passo\nSegundo passo") == ["Primeiro passo", "Segundo passo"]


def test_sem_pontuacao_sai_no_flush():
    chunker = Chunker()
    assert chunker.feed("texto sem pontuação") == []
    assert chunker.flush() == ["texto sem pontuação"]


# --- Limpeza ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Use o comando `top` ou **htop**.", "Use o comando top ou htop."),
    ("Tudo bem por aqui. 😊", "Tudo bem por aqui."),
    ("- primeiro\n- segundo", "primeiro segundo"),
    ("## Título\nTexto", "Título Texto"),
    ("Veja [a doc](https://x.com/a) ou https://y.com/b agora.", "Veja a doc ou link agora."),
    ("Um \"container\" (contêiner) lógico.", "Um container contêiner lógico."),
    ("```sql\nSELECT 1;\n```\nPronto.", "Pronto."),
    ("ps aux --sort=-%mem", "ps aux --sort=-%mem"),
])
def test_clean(raw, expected):
    assert clean(raw) == expected


# --- Normalização ----------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("O servidor 192.168.1.25 caiu.", "O servidor cento e noventa e dois ponto cento e sessenta e oito ponto um ponto vinte e cinco caiu."),
    ("Disco com 85% de uso.", "Disco com oitenta e cinco por cento de uso."),
    ("Podman 5.8 instalado.", "Podman cinco ponto oito instalado."),
    ("São 1.575 pessoas.", "São mil quinhentos e setenta e cinco pessoas."),
    ("Uso 2,5 vezes maior.", "Uso dois vírgula cinco vezes maior."),
    ("Alerta às 14:30.", "Alerta às catorze e trinta."),
    ("Reunião às 9h.", "Reunião às nove horas."),
    # horas no feminino (teste de audição de 19/10: "vinte e um e dois")
    ("São 21h02.", "São vinte e uma e dois."),
    ("Às 22h15.", "Às vinte e duas e quinze."),
    ("Começa à 1h.", "Começa à uma hora."),
    ("Às 2h30.", "Às duas e trinta."),
    ("Às 11h01.", "Às onze e um."),
    ("Às 12h00.", "Às doze horas."),
    ("O RMAN faz o backup.", "O érre man faz o backup."),
    ("Gera o AWR da PDB.", "Gera o a dáblio érre da pê dê bê."),
    ("Custa R$ 10,50.", "Custa dez reais e cinquenta centavos."),
    ("Custa R$ 1,01.", "Custa um real e um centavo."),
    ("Lucro de R$ 7,1 bilhões no 3º tri.", "Lucro de sete vírgula um bilhões de reais no terceiro tri."),
    ("Investe US$ 350 milhões.", "Investe trezentos e cinquenta milhões de dólares."),
    ("Vale R$ 2 mil.", "Vale dois mil reais."),
    ("Custa US$ 1.299.", "Custa mil duzentos e noventa e nove dólares."),
    ("O G1 diz que a IA do PHP...", "O gê um diz que a i a do pê agá pê..."),
    ("Ficou em 1º lugar.", "Ficou em primeiro lugar."),
    ("Tem 16 GB de RAM.", "Tem dezesseis gigabytes de ram."),
    ("A CPU está alta.", "A cê pê u está alta."),
    ("Quinze por cento.", "Quinze por cento."),
    ("Hoje é 11/10/2026.", "Hoje é onze de outubro de dois mil e vinte e seis."),
    ("A CPU está em 25.1%.", "A cê pê u está em vinte e cinco vírgula um por cento."),
    ("Disco com 4.0%.", "Disco com quatro por cento."),
    ("Memória em 61.04%.", "Memória em sessenta e um vírgula zero quatro por cento."),
    ("Tem 1.5 GB livres.", "Tem um vírgula cinco gigabytes livres."),
    ("Vence em 1/3.", "Vence em primeiro de março."),
    ("Backup de 05/01/26 ok.", "Backup de cinco de janeiro de dois mil e vinte e seis ok."),
])
def test_normalize(raw, expected):
    assert normalize_for_speech(raw) == expected


# --- Fala incompleta -------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Qual é o tamanho da", "Me explica para que serve o", "Verifica o servidor e", "Eu queria saber,",
    "Está me escutando...", "Liga o alerta de.", "Quero saber sobre",
])
def test_parece_incompleta(text):
    assert looks_incomplete(text)


@pytest.mark.parametrize("text", [
    "Que horas são agora?", "Obrigado, Agatha.", "Me fala do Zabbix", "Qual o tamanho da tablespace USERS?", "",
    "Esquece isso.", "O que é isso?",
])
def test_parece_completa(text):
    assert not looks_incomplete(text)


# --- Carimbo de data repetido pelo modelo --------------------------------------------

def filtered(text: str, step: int = 3) -> str:
    f = LeadingTimestampFilter()
    return "".join(f.feed(text[i : i + step]) for i in range(0, len(text), step)) + f.flush()


@pytest.mark.parametrize("raw, expected", [
    ("[domingo, 11/10/2026, 21h30] São 21h30 agora.", "São 21h30 agora."),
    ("  [domingo, 11/10/2026, 9h05]Oi.", "Oi."),
    ("São 21h30 agora.", "São 21h30 agora."),
    ("[nota] isso não é carimbo", "[nota] isso não é carimbo"),
    ("[" + "x" * 80, "[" + "x" * 80),  # colchete que nunca fecha: libera depois do limite
    ("[domingo", "[domingo"),            # resposta que acaba no meio: flush libera
])
def test_filtro_de_carimbo(raw, expected):
    assert filtered(raw) == expected


def test_estado_entre_parenteses_some_da_fala():
    from agatha_server.text import clean

    assert clean("Amanhã em Novo Hamburgo (RS) tem chuva.") == "Amanhã em Novo Hamburgo tem chuva."
    assert clean("Em Curitiba (PR), 17 graus.") == "Em Curitiba, 17 graus."
    assert clean("O banco (PRDORA) está no ar.") == "O banco PRDORA está no ar."  # não é estado: fica
