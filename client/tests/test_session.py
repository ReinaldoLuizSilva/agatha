"""Testes do barge-in na sessão do cliente (mensagens trocadas com o servidor), sem áudio real."""

import asyncio
import json

from agatha_client.__main__ import Session


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class FakePlayer:
    latency = 0.0

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in ("begin", "feed", "end", "pause", "resume", "stop"):
            return lambda *a: self.calls.append(name)
        raise AttributeError(name)

    def played_ms(self):
        return 1234.4

    async def wait_done(self):
        await asyncio.Event().wait()  # tocando até alguém decidir


class FakeBarge:
    def __init__(self, fire_on: int | None = None):
        self.fire_on, self.holds, self.n, self.ring = fire_on, [], 0, []

    def process(self, mic, ref, captured_at, active):
        self.n += 1
        self.ring.append((mic, captured_at))
        return mic, captured_at, (0.97 if active and self.n == self.fire_on else None)

    def pre_roll(self):
        ring, self.ring = self.ring, []
        return ring

    def sent(self):
        self.ring = []

    def hold(self, now):
        self.holds.append(now)

    def begin_stats(self, now):
        pass

    def end_stats(self):
        return None


class FakeSource:
    finite = False

    def __init__(self, n):
        self.n = n

    async def frames(self):
        for i in range(self.n):
            yield bytes([i]) * 4, b"\x00" * 4, float(i)
            await asyncio.sleep(0)


def make(fire_on=None, n=0):
    return Session(FakeWS(), FakeSource(n), FakePlayer(), {}, FakeBarge(fire_on))


def jsons(ws):
    return [json.loads(m) for m in ws.sent if isinstance(m, str)]


def test_disparo_pausa_avisa_e_manda_o_audio_desde_antes_do_nome():
    async def scenario():
        s = make(fire_on=4, n=6)
        s._on_message({"type": "response_start", "response": 1, "segment": 1})
        await s.send_loop()
        return s

    s = asyncio.run(scenario())
    assert s.player.calls == ["begin", "pause"]
    assert s.ws.sent[0] == json.dumps({"type": "barge_in", "response": 1, "played_ms": 1234})
    # blocos 1–4 (nunca enviados, inclusive o do disparo) e depois o microfone ao vivo (5 e 6)
    assert s.ws.sent[1:] == [bytes([i]) * 4 for i in range(6)]
    assert s.captured_at == [float(i) for i in range(6)]
    assert s.can_send.is_set() and s._confirming == 1


def test_nao_dispara_fora_da_resposta():
    async def scenario():
        s = make(fire_on=2, n=3)
        await s.send_loop()
        return s

    s = asyncio.run(scenario())
    assert jsons(s.ws) == [] and len(s.ws.sent) == 3


def start_barge_in(s):
    s._on_message({"type": "response_start", "response": 1, "segment": 1})
    s.pending.add(1)
    asyncio.run(s._start_barge_in(0.97))


def test_resume_volta_a_tocar_e_fecha_o_microfone():
    s = make()
    start_barge_in(s)
    s._on_message({"type": "barge_in_end", "response": 1, "action": "resume", "segment": None})
    assert s.player.calls[-1] == "resume"
    assert not s.can_send.is_set() and s._confirming is None and s.barge.holds


def test_stop_descarta_a_resposta_e_libera_o_microfone():
    async def scenario():
        s = make()
        s._on_message({"type": "response_start", "response": 1, "segment": 1})
        s.pending.add(1)
        s._on_message({"type": "response_end", "response": 1, "reason": "done", "timings": {}})
        await s._start_barge_in(0.97)
        s._on_message({"type": "barge_in_end", "response": 1, "action": "stop", "segment": 2})
        await asyncio.sleep(0)
        return s

    s = asyncio.run(scenario())
    assert s.player.calls[-1] == "stop"
    assert s._finish_task.cancelled()
    assert "playback_end" not in [m["type"] for m in jsons(s.ws)]  # resposta encerrada pelo servidor
    assert s.can_send.is_set() and s._speaking is None and not s.pending


def test_resume_depois_que_ela_terminou_reenvia_o_playback_end():
    async def scenario():
        s = make()
        s._on_message({"type": "response_start", "response": 1, "segment": 1})
        await s._start_barge_in(0.97)
        s._speaking = None  # o playback_end saiu durante a confirmação (o servidor o ignora)
        s._on_message({"type": "barge_in_end", "response": 1, "action": "resume", "segment": None})
        await asyncio.sleep(0)
        return s

    s = asyncio.run(scenario())
    assert jsons(s.ws)[-1] == {"type": "playback_end", "response": 1, "e2e_ms": None}
    assert s.can_send.is_set()


def test_barge_in_end_de_outra_resposta_e_ignorado():
    s = make()
    start_barge_in(s)
    s._on_message({"type": "barge_in_end", "response": 7, "action": "stop", "segment": None})
    assert s._confirming == 1 and s.player.calls[-1] == "pause"
