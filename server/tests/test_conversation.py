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
    conv = Conversation(llm, synth, ThreadPoolExecutor(1), ConversationConfig(history_turns=2),
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
    assert user == {"role": "user", "content": "[domingo, 11 de outubro de 2026, 17:55] Que horas são?"}


def test_historico_e_limitado_e_guarda_o_texto_exato():
    conv, llm, _ = make("Resposta **bruta**.\n")
    for i in range(3):
        run(conv, f"pergunta {i}")
    # history_turns=2: só as duas últimas trocas ficam, com o texto exato enviado e gerado
    stamp = "[domingo, 11 de outubro de 2026, 17:55]"
    assert [m["content"] for m in conv.history] == [
        f"{stamp} pergunta 1", "Resposta **bruta**.\n", f"{stamp} pergunta 2", "Resposta **bruta**.\n",
    ]
    # a 3ª chamada viu o sistema + as 2 trocas anteriores, idênticas ao que foi enviado/gerado, + a pergunta
    raw = "Resposta **bruta**.\n"
    assert [m["role"] for m in llm.calls[2]] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert [m["content"] for m in llm.calls[2][1:]] == [
        f"{stamp} pergunta 0", raw, f"{stamp} pergunta 1", raw, f"{stamp} pergunta 2",
    ]


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
