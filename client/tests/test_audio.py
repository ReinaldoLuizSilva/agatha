"""Testes da reprodução (Player) com um sounddevice falso que chama o callback numa thread."""

import asyncio
import sys
import threading
import time
import types

import pytest


class FakeOutputStream:
    """Imita o RawOutputStream: um callback a cada 1024 amostras, no ritmo real, numa thread."""

    instances: list["FakeOutputStream"] = []

    def __init__(self, samplerate, channels, dtype, device, latency, callback):
        self.samplerate = samplerate
        self.callback = callback
        self.latency = 0.02
        self.played = bytearray()
        self._stop = threading.Event()
        FakeOutputStream.instances.append(self)

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        frames = 1024
        while not self._stop.is_set():
            out = bytearray(frames * 2)
            self.callback(out, frames, None, None)
            self.played += out
            time.sleep(frames / self.samplerate)

    def stop(self):
        self._stop.set()
        self._thread.join()

    def close(self):
        pass


@pytest.fixture
def fake_sounddevice(monkeypatch):
    module = types.SimpleNamespace(RawOutputStream=FakeOutputStream, PortAudioError=Exception)
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    FakeOutputStream.instances.clear()
    return module


def test_player_toca_tudo_e_informa_o_primeiro_som(fake_sounddevice):
    from agatha_client.audio import Player

    async def scenario():
        player = Player(device=None, sample_rate=22050)
        player.begin()
        t0 = time.monotonic()
        pcm = b"\x01\x02" * 4410  # 0,2 s
        player.feed(pcm[:4000])
        await asyncio.sleep(0.05)
        player.feed(pcm[4000:])
        player.end()
        first = await player.wait_done()
        elapsed = time.monotonic() - t0
        player.close()
        return first - t0, elapsed, bytes(FakeOutputStream.instances[0].played), pcm

    first_delay, elapsed, played, pcm = asyncio.run(scenario())
    assert pcm in played  # o áudio saiu inteiro e em ordem, entre trechos de silêncio
    assert 0 < first_delay < 0.2  # primeiro som logo depois do primeiro feed (+ latência)
    assert elapsed >= 0.2  # esperou o áudio tocar até o fim


def test_player_sem_audio_libera_na_hora(fake_sounddevice):
    from agatha_client.audio import Player

    async def scenario():
        player = Player(device=None, sample_rate=22050)
        player.begin()
        player.end()
        first = await asyncio.wait_for(player.wait_done(), 1)
        player.close()
        return first

    assert asyncio.run(scenario()) is None


def test_player_reaproveitado_entre_respostas(fake_sounddevice):
    from agatha_client.audio import Player

    async def scenario():
        player = Player(device=None, sample_rate=22050)
        firsts = []
        for _ in range(2):
            player.begin()
            player.feed(b"\x05\x00" * 2205)
            player.end()
            firsts.append(await asyncio.wait_for(player.wait_done(), 2))
        player.close()
        return firsts, len(FakeOutputStream.instances)

    firsts, streams = asyncio.run(scenario())
    assert streams == 1  # um único fluxo de saída para a sessão inteira
    assert firsts[0] is not None and firsts[1] is not None and firsts[1] > firsts[0]
