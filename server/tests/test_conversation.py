import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from agatha_server.conversation import FILLERS, Audio, Conversation, ConversationConfig, Done, TextChunk
from agatha_server.llm import ContextOverflow, TextDelta, ToolCallDelta
from agatha_server.memory import MemoryConfig, MemoryStore
from agatha_server.tools.memory import memory_tools
from agatha_server.prompts import describe_now
from agatha_server.tools import Example, Tool, ToolRegistry


class FakeLLM:
    """LLM falso. Cada resposta do roteiro é um texto ou ("tool", nome, argumentos_json)."""

    def __init__(self, *replies, fail_after: int | None = None, fail_with_tools: bool = False):
        self.replies = list(replies)
        self.fail_after = fail_after
        self.fail_with_tools = fail_with_tools
        self.calls: list[list[dict]] = []
        self.tools_seen: list = []
        self.prefills: list[tuple[list[dict], list | None]] = []

    async def prefill(self, messages, tools=None):
        self.prefills.append((messages, tools))

    async def stream_chat(self, messages, tools=None):
        self.calls.append(messages)
        self.tools_seen.append(tools)
        if self.fail_with_tools and tools:
            raise RuntimeError("HTTP 500: Failed to parse tool call arguments as JSON")
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, tuple):
            _, name, args = reply
            yield ToolCallDelta(0, "call_x", name, "")
            for i in range(0, len(args), 5):
                yield ToolCallDelta(0, None, None, args[i : i + 5])
            return
        for i in range(0, len(reply), 4):  # pedaços de 4 caracteres, como tokens
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("llm caiu")
            yield TextDelta(reply[i : i + 4])


class FakeSynth:
    sample_rate = 22050

    def __init__(self):
        self.spoken: list[str] = []

    def synthesize(self, text: str) -> bytes:
        self.spoken.append(text)
        return b"\x00\x00" * len(text)


NOW = datetime(2026, 10, 11, 17, 55)
STAMP = "[domingo, 11/10/2026, 17h55]"


def run(conversation: Conversation, text: str) -> list:
    async def collect():
        return [e async for e in conversation.respond(text, t_ref=0.0)]

    return asyncio.run(collect())


def make(*replies, tools: ToolRegistry | None = None, memory: MemoryStore | None = None,
         **kwargs) -> tuple[Conversation, FakeLLM, FakeSynth]:
    llm, synth = FakeLLM(*replies, **kwargs), FakeSynth()
    conv = Conversation(llm, synth, ThreadPoolExecutor(1), ConversationConfig(history_turns=4),
                        clock=lambda: 1.0, now=lambda: NOW, tools=tools, memory=memory)
    return conv, llm, synth


def test_eventos_em_ordem():
    conv, _, synth = make("Oi, Rei. Tudo certo por aqui.")
    events = run(conv, "Oi Agatha")
    assert [type(e) for e in events] == [TextChunk, Audio, TextChunk, Audio, Done]
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Oi, Rei.", "Tudo certo por aqui."]
    done = events[-1]
    assert done.reason == "done"
    assert done.text == "Oi, Rei. Tudo certo por aqui."
    assert done.tools == []
    assert set(done.timings) == {"llm_first_token_ms", "first_chunk_ms", "tts_first_ms"}
    assert done.timings["first_chunk_ms"] == 1000  # relógio falso: sempre 1 s após a referência


def test_tts_recebe_texto_limpo_e_normalizado():
    conv, _, synth = make("Use o **top**. O disco está com 85% 😊.")
    events = run(conv, "pergunta")
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["Use o top.", "O disco está com 85%."]
    assert synth.spoken == ["Use o top.", "O disco está com oitenta e cinco por cento."]


def test_carimbo_de_data_repetido_pelo_modelo_nao_e_falado():
    conv, _, synth = make(f"{STAMP} São 17h55 agora.")
    run(conv, "Que horas são?")
    assert synth.spoken == ["São dezessete e cinquenta e cinco agora."]


def test_data_e_hora_vao_na_mensagem_do_usuario():
    conv, llm, _ = make("Ok.")
    run(conv, "Que horas são?")
    system, user = llm.calls[0]
    assert system["role"] == "system"
    assert describe_now(NOW) not in system["content"]  # prefixo estável para o cache do llama.cpp
    assert user == {"role": "user", "content": f"{STAMP} Que horas são?"}


def test_historico_guarda_o_texto_exato_e_corta_em_bloco():
    conv, llm, _ = make("Resposta **bruta**.\n")
    sizes, prefills = [], []
    for i in range(6):
        run(conv, f"pergunta {i}")
        sizes.append(len(conv.history))
        prefills.append(len(llm.prefills))
    # history_turns=4: cresce até 4 turnos; no 5º, volta para os 2 mais recentes de uma vez
    assert sizes == [1, 2, 3, 4, 2, 3]
    raw = "Resposta **bruta**.\n"
    assert [[m["content"] for m in turn] for turn in conv.history] == [
        [f"{STAMP} pergunta 3", raw], [f"{STAMP} pergunta 4", raw], [f"{STAMP} pergunta 5", raw],
    ]
    # o cache só é pré-aquecido depois do corte (5ª resposta), com o prompt já cortado
    assert prefills == [0, 0, 0, 0, 1, 1]
    messages, _ = llm.prefills[0]
    assert messages[0]["role"] == "system"
    assert [m["content"] for m in messages[1:]] == [f"{STAMP} pergunta 3", raw, f"{STAMP} pergunta 4", raw]
    # entre dois cortes, cada chamada começa exatamente com a anterior: o prefixo não muda
    for i in range(1, 4):
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


# --- Ferramentas -----------------------------------------------------------------------

def echo_tools(calls: list) -> ToolRegistry:
    def handler(args):
        calls.append(args)
        return {"resultado": 36}

    return ToolRegistry([Tool(
        name="calcular", description="calcula", handler=handler, rule="contas: use calcular",
        properties={"expressao": {"type": "string"}}, required=("expressao",),
        examples=(Example("Quanto é 18% de 350?", {"expressao": "350 * 0.18"}, {"resultado": 63}, "Dá 63."),),
    )])


def test_ferramenta_fala_espera_executa_e_responde_com_o_resultado():
    executed = []
    conv, llm, synth = make(("tool", "calcular", '{"expressao": "240 * 0.15"}'), "É 36.",
                            tools=echo_tools(executed))
    events = run(conv, "Quanto é 15% de 240?")
    texts = [e.text for e in events if isinstance(e, TextChunk)]
    assert texts[0] in FILLERS  # fala algo assim que percebe a chamada
    assert texts[1:] == ["É 36."]
    assert executed == [{"expressao": "240 * 0.15"}]
    done = events[-1]
    assert done.tools == ["calcular"]
    assert "tool_ms" in done.timings
    # 1ª ida com ferramentas; 2ª ida recebe a chamada e o resultado
    assert llm.tools_seen[0] and llm.tools_seen[0][0]["function"]["name"] == "calcular"
    second = llm.calls[1]
    assert second[-2]["tool_calls"][0]["function"] == {"name": "calcular", "arguments": '{"expressao": "240 * 0.15"}'}
    assert second[-1] == {"role": "tool", "tool_call_id": "call_x", "content": json.dumps({"resultado": 36})}
    # o turno inteiro (pergunta, chamada, resultado, resposta) vai para o histórico, exatamente
    assert [m["role"] for m in conv.history[0]] == ["user", "assistant", "tool", "assistant"]
    assert conv.history[0][2]["content"] == json.dumps({"resultado": 36})  # pequeno: fica inteiro


def test_resultado_grande_de_ferramenta_vira_marcador_no_historico():
    big = {"cidade": "Novo Hamburgo", "hoje": "de 12 a 25 graus, chance de chuva 10%, nublado",
           "amanhã": "de 14 a 28 graus, chance de chuva 39%, garoa fraca", "sexta-feira": "de 18 a 30 graus"}
    tools = ToolRegistry([Tool(name="clima", description="clima", handler=lambda args: big, rule="tempo: use clima",
                               properties={"cidade": {"type": "string"}}, required=("cidade",))])
    conv, llm, synth = make(("tool", "clima", '{"cidade": "Novo Hamburgo"}'), "Amanhã tem 39% de chance de chuva.",
                            "Nublado.", tools=tools)
    run(conv, "Vai chover amanhã?")
    assert llm.calls[1][-1]["content"] == json.dumps(big, ensure_ascii=False)  # na hora, o modelo vê tudo
    tool_msg = conv.history[0][2]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "call_x"
    assert json.loads(tool_msg["content"]) == {"omitido": "já usado na resposta; consulte de novo se precisar"}
    assert conv.history[0][1]["tool_calls"][0]["function"]["name"] == "clima"  # a chamada continua
    run(conv, "E hoje?")
    assert "omitido" in llm.calls[2][-3]["content"]  # a pergunta seguinte já vai com o marcador


def test_prefixo_tem_regras_e_exemplos_das_ferramentas():
    conv, llm, _ = make("Oi.", tools=echo_tools([]))
    run(conv, "oi")
    messages = llm.calls[0]
    assert "contas: use calcular" in messages[0]["content"]
    roles = [m["role"] for m in messages[1:-1]]
    assert roles[:4] == ["user", "assistant", "tool", "assistant"]  # exemplo de chamada da ferramenta
    assert messages[2]["tool_calls"][0]["function"]["name"] == "calcular"


def test_sem_ferramentas_nao_envia_tools():
    conv, llm, _ = make("Oi.")
    run(conv, "oi")
    assert llm.tools_seen == [None]
    assert len(llm.calls[0]) == 2  # sistema + pergunta, sem exemplos


def test_falha_com_ferramentas_tenta_de_novo_sem():
    conv, llm, synth = make("Não consegui calcular, mas dá uns 36.", tools=echo_tools([]), fail_with_tools=True)
    events = run(conv, "Quanto é 15% de 240?")
    assert events[-1].reason == "done"
    assert llm.tools_seen[0] is not None and llm.tools_seen[1] is None
    assert synth.spoken[0].startswith("Não consegui calcular")


def test_limite_de_rodadas_de_ferramenta():
    executed = []
    # o modelo insiste em chamar a ferramenta; depois de 2 rodadas, a 3ª ida vai sem ferramentas
    conv, llm, _ = make(("tool", "calcular", '{"expressao": "1+1"}'), ("tool", "calcular", '{"expressao": "2+2"}'),
                        "Pronto.", tools=echo_tools(executed))
    events = run(conv, "conta aí")
    assert len(executed) == 2
    assert llm.tools_seen[2] is None
    assert events[-1].text.endswith("Pronto.")


# --- Memória (ADR-010) -----------------------------------------------------------------

def test_memoria_entra_no_prompt_e_muda_so_no_turno_seguinte():
    mem = MemoryStore(MemoryConfig(db=":memory:"))
    tools = ToolRegistry(memory_tools(mem))
    conv, llm, _ = make(("tool", "guardar_fato", '{"fato": "O banco de produção se chama PRDORA"}'), "Guardei.", "Ok.",
                        tools=tools, memory=mem)

    async def scenario():
        events = [e async for e in conv.respond("Lembra que o banco se chama PRDORA", t_ref=0.0)]
        await conv._prefill_task  # pré-aquecimento disparado pela mudança na memória
        events2 = [e async for e in conv.respond("O que você sabe sobre mim?", t_ref=0.0)]
        return events, events2

    asyncio.run(scenario())
    first_round, second_round, next_turn = llm.calls
    assert "memória sobre o Reinaldo está vazia" in first_round[0]["content"]
    # dentro do mesmo turno o prompt não muda (senão o llama.cpp reprocessaria tudo no meio)
    assert second_round[0] == first_round[0]
    # no turno seguinte, o fato aparece numerado
    assert "1. O banco de produção se chama PRDORA" in next_turn[0]["content"]
    # e o cache do novo prompt foi recalculado em segundo plano, com as ferramentas
    messages, schemas = llm.prefills[-1]
    assert messages[0] == next_turn[0] and schemas


class OverflowLLM(FakeLLM):
    """Recusa o prompt se ele tiver mais que `limit` mensagens (simula o contexto estourado)."""

    def __init__(self, limit: int, *replies):
        super().__init__(*replies)
        self.limit = limit

    async def stream_chat(self, messages, tools=None):
        if len(messages) > self.limit:
            self.calls.append(messages)
            raise ContextOverflow("request exceeds the available context size")
        async for d in super().stream_chat(messages, tools):
            yield d


def test_contexto_estourado_corta_o_historico_e_tenta_de_novo():
    llm, synth = OverflowLLM(7, "Ok."), FakeSynth()
    conv = Conversation(llm, synth, ThreadPoolExecutor(1), ConversationConfig(history_turns=12),
                        clock=lambda: 1.0, now=lambda: NOW)
    for i in range(3):  # sistema + 3 turnos × 2 mensagens = 7 mensagens: ainda cabe
        run(conv, f"pergunta {i}")
    events = run(conv, "pergunta 3")  # 9 mensagens: estoura, corta para 2 turnos e responde
    assert events[-1].reason == "done"
    assert len(conv.history) == 3  # os 2 que ficaram + o turno atual
    assert len(llm.calls[-1]) == 6  # sistema + 2 turnos + pergunta


def test_contexto_estourado_sem_historico_e_erro():
    llm, synth = OverflowLLM(1, "Ok."), FakeSynth()
    conv = Conversation(llm, synth, ThreadPoolExecutor(1), clock=lambda: 1.0, now=lambda: NOW)
    assert run(conv, "pergunta")[-1].reason == "error"
