"""Testes do stream duplex do barge-in (DuplexAudio) com um sounddevice falso."""

import asyncio
import sys
import threading
import time
import types

import pytest

SR = 16000


class FakeRawStream:
    """Imita o RawStream: callback(indata, outdata, ...) a cada `blocksize` amostras, numa thread."""

    def __init__(self, samplerate, blocksize, channels, dtype, device, latency, callback):
        self.samplerate, self.blocksize, self.callback = samplerate, blocksize, callback
        self.latency = (0.01, 0.02)
        self.played = bytearray()
        self._stop = threading.Event()

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def _run(self):
        status = types.SimpleNamespace(input_overflow=False)
        mic = b"\x07\x00" * self.blocksize
        while not self._stop.is_set():
            out = bytearray(self.blocksize * 2)
            self.callback(mic, out, self.blocksize, None, status)
            self.played += out
            time.sleep(self.blocksize / self.samplerate / 4)  # 4× o ritmo real: testes rápidos


@pytest.fixture
def duplex(monkeypatch):
    ok = lambda **kw: None  # noqa: E731
    module = types.SimpleNamespace(RawStream=FakeRawStream, PortAudioError=Exception,
                                   check_input_settings=ok, check_output_settings=ok)
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    from agatha_client.audio import DuplexAudio

    return DuplexAudio(None, None)


def run_with_stream(duplex, scenario):
    """Roda `scenario(duplex, refs)` com o stream aberto; `refs` acumula a referência de cada bloco."""
    async def main():
        refs = []

        async def consume():
            async for mic, ref, _ in duplex.frames():
                assert len(mic) == len(ref) == 1024
                refs.append(ref)

        task = asyncio.create_task(consume())
        while duplex.latency == 0:
            await asyncio.sleep(0.001)
        try:
            return await asyncio.wait_for(scenario(duplex, refs), 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return asyncio.run(main())


def tocado(refs) -> bytes:
    return b"".join(refs).replace(b"\x00\x00", b"")


def test_referencia_e_o_que_foi_tocado(duplex):
    pcm = b"\x01\x02" * 8000  # 0,5 s

    async def scenario(d, refs):
        d.begin()
        d.feed(pcm)
        d.end()
        first = await d.wait_done()
        return first, tocado(refs)

    first, played = run_with_stream(duplex, scenario)
    assert first is not None and played == pcm


def test_pausa_e_retoma_sem_perder_nada(duplex):
    pcm = b"\x01\x02" * 16000  # 1 s

    async def scenario(d, refs):
        d.begin()
        d.feed(pcm)
        await asyncio.sleep(0.05)
        d.pause()
        before = d.played_ms()
        n = len(refs)
        await asyncio.sleep(0.05)
        silent = all(r == b"\x00" * 1024 for r in refs[n + 1 :])  # o bloco já em curso pode sair
        assert d.played_ms() == before  # pausado: não avança
        d.resume()
        d.end()
        await d.wait_done()
        return before, silent, tocado(refs)

    before, silent, played = run_with_stream(duplex, scenario)
    assert before > 0 and silent
    assert played == pcm  # retomou do ponto onde parou


def test_stop_descarta_o_resto_e_libera(duplex):
    async def scenario(d, refs):
        d.begin()
        d.feed(b"\x01\x02" * 16000)
        await asyncio.sleep(0.03)
        d.pause()
        d.stop()
        await asyncio.wait_for(d.wait_done(), 1)
        n = len(refs)
        await asyncio.sleep(0.03)
        return all(r == b"\x00" * 1024 for r in refs[n + 1 :]), len(tocado(refs))

    silent, played = run_with_stream(duplex, scenario)
    assert silent and played < 32000


def test_played_ms_desconta_a_latencia_de_saida(duplex):
    duplex.latency = 0.02
    duplex.begin()
    duplex._played = 2 * SR  # 1 s entregue à placa
    assert duplex.played_ms() == pytest.approx(980)
