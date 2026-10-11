import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from agatha_server.conversation import Audio, Conversation, ConversationConfig, Done, TextChunk
from agatha_server.prompts import describe_now


class FakeLLM:
    def __init__(self, reply: str, fail_after: int | None = None):
        self.reply = reply
        self.fail_after = fail_after
        self.calls: list[list[dict]] = []

    async def stream_chat(self, messages):
        self.calls.append(messages)
        for i in range(0, len(self.reply), 4):  # pedaços de 4 caracteres, como tokens
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("llm caiu")
            yield self.reply[i : i + 4]


class FakeSynth:
    sample_rate = 22050

    def __init__(self):
        self.spoken: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        return b"\x00\x00" * len(text)


NOW = datetime(2026, 10, 11, 17, 55)


def run(conversation: Conversation, text: str) -> list:
    async def collect():
        return [e async for e in conversation.respond(text, t_ref=0.0)]

    return asyncio.run(collect())


def make(reply: str, **kwargs) -> tuple[Conversation, FakeLLM, FakeSynth]:
    llm, synth = FakeLLM(reply, **kwargs), FakeSynth()
    conv = Conversation(llm, synth, ThreadPoolExecutor(1), ConversationConfig(history_turns=4),
                        clock=lambda: 1.0, now=lambda: NOW)
    return conv, llm, synth


def test_eventos_em_ordem():
    conv, _, synth = make("Oi, Rei. Tudo certo por aqui.")
    events = run(conv, "Oi Agatha")
    assert [type(e) for e in events] == [TextChunk, Audio, TextChunk, Audio, Done]
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Oi, Rei.", "Tudo certo por aqui."]
    done = events[-1]
    assert done.reason == "done"
    assert done.text == "Oi, Rei. Tudo certo por aqui."
    assert set(done.timings) == {"llm_first_token_ms", "first_chunk_ms", "tts_first_ms"}
    assert done.timings["first_chunk_ms"] == 1000  # relógio falso: sempre 1 s após a referência


def test_tts_recebe_texto_limpo_e_normalizado():
    conv, _, synth = make("Use o **top**. O disco está com 85% 😊.")
    events = run(conv, "pergunta")
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Use o top.", "O disco está com 85%."]
    assert synth.spoken == ["Use o top.", "O disco está com oitenta e cinco por cento."]


def test_data_e_hora_vao_na_mensagem_do_usuario():
    conv, llm, _ = make("Ok.")
    run(conv, "Que horas são?")
    system, user = llm.calls[0]
    assert system["role"] == "system"
    assert describe_now(NOW) not in system["content"]  # prefixo estável para o cache do llama.cpp
    assert user == {"role": "user", "content": "[domingo, 11/10/2026, 17h55] Que horas são?"}


def test_historico_guarda_o_texto_exato_e_corta_em_bloco():
    conv, llm, _ = make("Resposta **bruta**.\n")
    stamp = "[domingo, 11/10/2026, 17h55]"
    sizes = []
    for i in range(6):
        run(conv, f"pergunta {i}")
        sizes.append(len(conv.history) // 2)
    # history_turns=4: cresce até 4 trocas; na 5ª, volta para as 2 mais recentes de uma vez
    assert sizes == [1, 2, 3, 4, 2, 3]
    assert [m["content"] for m in conv.history] == [
        f"{stamp} pergunta 3", "Resposta **bruta**.\n", f"{stamp} pergunta 4", "Resposta **bruta**.\n",
        f"{stamp} pergunta 5", "Resposta **bruta**.\n",
    ]
    # a 3ª chamada viu o sistema + as 2 trocas anteriores, idênticas ao que foi enviado/gerado, + a pergunta
    raw = "Resposta **bruta**.\n"
    assert [m["content"] for m in llm.calls[2][1:]] == [
        f"{stamp} pergunta 0", raw, f"{stamp} pergunta 1", raw, f"{stamp} pergunta 2",
    ]
    # entre dois cortes, cada chamada começa exatamente com a anterior: o prefixo não muda e o
    # llama.cpp reaproveita o cache (chamadas 0 a 4; o corte acontece depois da 5ª resposta)
    for i in range(1, 5):
        assert llm.calls[i][: len(llm.calls[i - 1])] == llm.calls[i - 1]


def test_erro_no_llm():
    conv, _, _ = make("Primeira frase. Segunda frase que não chega.", fail_after=20)
    events = run(conv, "pergunta")
    assert isinstance(events[-1], Done)
    assert events[-1].reason == "error"
    assert events[-1].text == "Primeira frase."
    assert conv.history == []  # resposta com erro não entra no histórico


def test_resposta_so_com_emoji_nao_gera_audio():
    conv, _, synth = make("😊")
    events = run(conv, "oi")
    assert [type(e) for e in events] == [Done]
    assert synth.spoken == []
