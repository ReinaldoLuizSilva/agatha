"""Gravador guiado de amostras: wake word e barge-in (Fase 5) e jargão para o STT (Fase 6).

Mostra uma frase, grava a sua voz pelo microfone e salva um WAV de 16 kHz por fala,
com um manifest.jsonl descrevendo cada uma. As partes do roteiro:

    perto       "Agatha" em frases variadas, a ~50 cm do notebook, voz normal
    variado     baixinho, rápido, devagar, chamando, firme (como quem interrompe)
    longe       a 2–3 m do notebook, como chamaria de outro canto do cômodo
    negativas   frases SEM o nome, inclusive as parecidas ("a gata", "agora")
    eco         a Agatha fala pelas caixas de som e você a interrompe no meio (ou fica quieto);
                salva o microfone e, ao lado, o áudio exato que saiu nas caixas (*.ref.wav)
    ambiente    alguns minutos de TV, rádio ou podcast tocando em outro aparelho
    jargao      frases com o jargão do trabalho (Oracle, Zabbix, infra), para medir o STT (Fase 6);
                fica fora do padrão: grave com --part jargao

Uso (no notebook, dentro de client/):
    python -m agatha_client.record --out gravacoes                      # partes da wake word, em ordem
    python -m agatha_client.record --out gravacoes-stt --part jargao    # jargão (benchmark do STT)
    python -m agatha_client.record --out gravacoes --part perto longe   # só algumas
    python -m agatha_client.record --out gravacoes --part eco --replies respostas
    python -m agatha_client.record --list                               # mostra o roteiro
    python -m agatha_client.record --out gravacoes --redo perto-001..perto-020   # regrava estas

Pode parar a qualquer momento (q): rodar de novo continua de onde parou. Uma fala regravada ganha
uma linha nova no manifest.jsonl; vale a última linha de cada id.
"""

import argparse
import json
import sys
import time
import wave
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE, FatalError, list_devices, sounddevice

# --- Roteiro ---------------------------------------------------------------------------

NEAR = [
    "Agatha",
    "Agatha?",
    "Agatha, que horas são?",
    "Agatha, tem algum problema no Zabbix?",
    "Agatha, como está a tablespace USERS?",
    "Agatha, quantas sessões tem no Oracle?",
    "Agatha, quanto é quinze por cento de duzentos?",
    "Agatha, o que é housekeeping no Zabbix?",
    "Agatha, me ajuda com uma coisa",
    "Agatha, guarda que a reunião é amanhã",
    "Agatha, esquece isso",
    "Agatha, para",
    "Agatha, chega",
    "Agatha, espera",
    "Agatha, cancela",
    "Ei, Agatha",
    "Oi, Agatha, tudo bem?",
    "Bom dia, Agatha",
    "Obrigado, Agatha",
    "Certo, Agatha. E o host do banco?",
]

VARIED = [
    ("Agatha", "baixinho"),
    ("Agatha, que horas são?", "baixinho"),
    ("Agatha, tá aí?", "rápido"),
    ("Agatha, tem problema no Zabbix?", "rápido"),
    ("Agatha, para", "rápido"),
    ("Agatha", "devagar, arrastado"),
    ("Agatha!", "chamando, mais alto (sem gritar)"),
    ("Agatha, para!", "firme, como quem interrompe"),
    ("Agatha, chega!", "firme, como quem interrompe"),
    ("Agatha, e aí?", "descontraído"),
    ("Agatha", "cansado, desanimado"),
    ("Agatha, me escuta", "virado de lado para o notebook"),
]

FAR = [
    "Agatha",
    "Agatha!",
    "Agatha, que horas são?",
    "Agatha, tem algum problema no Zabbix?",
    "Agatha, quantas sessões tem no Oracle?",
    "Agatha, me ajuda com uma coisa",
    "Agatha, para",
    "Agatha, chega",
    "Ei, Agatha",
    "Oi, Agatha, tudo bem?",
    "Obrigado, Agatha",
    "Agatha, esquece isso",
]

# Sem o nome. "Ágata" fica de fora: em PT-BR se pronuncia igual a "Agatha".
NEGATIVES = [
    "A gata subiu no telhado",
    "Agora não dá",
    "A garrafa está na geladeira",
    "A garota chegou",
    "A gente conversa depois",
    "A data da entrega é sexta",
    "A cada hora o backup roda",
    "A tabela está cheia",
    "Aguarda um pouco",
    "Acabou o café",
    "A Renata ligou",
    "Achei a chave do carro",
    "Agenda a reunião para amanhã",
    "Alberta é uma província do Canadá",
    "A pasta está no servidor",
    "A batata está pronta",
    "Atacama é um deserto",
    "Amanhã tem jogo",
    "Alguém viu o meu fone?",
    "A taxa subiu de novo",
    "A lata está vazia",
    "Agachar dói o joelho",
    "Que horas são?",
    "Tem algum problema no Zabbix?",
    "Para com isso",
    "Chega por hoje",
    "Espera aí",
    "Obrigado",
    "Ei, você viu isso?",
    "O host do banco caiu de novo",
]

# Uma interrupção sim, outra não: as caladas medem se a própria voz da Agatha dispara o detector.
INTERRUPTIONS = [
    "Agatha, para",
    None,
    "Agatha, chega",
    None,
    "Agatha, espera",
    None,
    "Agatha, me fala outra coisa",
    None,
]

# Fase 6 (precisão): o jargão do dia a dia, inclusive o que o STT já errou ("Rima" por RMAN, "abics"
# por Zabbix, "valer" por validar) e os nomes do ambiente. Também serve para o benchmark do LLM.
JARGON = [
    "Agatha, para que serve o RMAN?",
    "Agatha, explica a diferença entre backup completo, incremental e diferencial.",
    "Agatha, como eu gero um relatório AWR no Oracle?",
    "Agatha, o que é o ASH no Oracle?",
    "Agatha, o Data Guard está sincronizado?",
    "Agatha, qual a diferença entre CDB e PDB?",
    "Agatha, onde fica o SPFILE do banco?",
    "Agatha, a tablespace de UNDO está cheia?",
    "Agatha, quantos grupos de redo log o banco tem?",
    "Agatha, o banco está em modo archivelog?",
    "Agatha, o listener do Oracle está no ar?",
    "Agatha, o que é o ASM?",
    "Agatha, como funciona o flashback database?",
    "Agatha, quantas sessões ativas tem no PRDORA?",
    "Agatha, tem algum lock no banco agora?",
    "Agatha, valida se o Zabbix está no ar.",
    "Agatha, verifica o host agatha-srv no Zabbix.",
    "Agatha, qual a diferença entre item, trigger e template no Zabbix?",
    "Agatha, como funciona o housekeeping do Zabbix?",
    "Agatha, o que é um Zabbix proxy?",
    "Agatha, como funciona a descoberta de baixo nível no Zabbix?",
    "Agatha, quais triggers estão disparadas agora?",
    "Agatha, o agente do Zabbix está respondendo?",
    "Agatha, qual a severidade desse problema?",
    "Agatha, me mostra os problemas de alta prioridade.",
    "Agatha, para que serve o Grafana?",
    "Agatha, qual a diferença entre PostgreSQL e Oracle?",
    "Agatha, o que é um container Docker?",
    "Agatha, qual a diferença entre Podman e Docker?",
    "Agatha, o que é um pod no Kubernetes?",
    "Agatha, para que serve o Ansible?",
    "Agatha, o que o Terraform faz?",
    "Agatha, como está o uso de CPU do servidor?",
    "Agatha, quanto de memória o servidor está usando?",
    "Agatha, o disco do barra dados está cheio?",
    "Agatha, quanto é dois terabytes em gigabytes?",
    "Agatha, guarda que o PRDORA roda na porta mil quinhentos e vinte e um.",
    "Agatha, o que você sabe sobre o meu ambiente?",
    "Agatha, valida o backup de ontem.",
    "Agatha, reinicia o serviço do listener.",
]

PARTS = {
    "perto": "a ~50 cm do notebook, voz normal",
    "variado": "a ~50 cm do notebook, do jeito indicado em cada frase",
    "longe": "a 2–3 m do notebook, como chamaria de outro canto do cômodo",
    "negativas": "a ~50 cm do notebook, voz normal; frases SEM o nome",
    "eco": "caixas de som do notebook no volume de sempre; a Agatha fala e você a interrompe",
    "ambiente": "TV, rádio ou podcast tocando em outro aparelho; ninguém chama a Agatha",
    "jargao": "a ~50 cm do notebook, voz normal, do jeito que você pergunta no dia a dia",
}
DEFAULT_PARTS = ["perto", "variado", "longe", "negativas", "eco", "ambiente"]  # roteiro da wake word
NEAR_REPEATS = 2
VARIED_REPEATS = 2
AMBIENT_CHUNK_S = 60
ECO_TAIL_S = 1.5  # depois do fim da resposta, para pegar uma interrupção atrasada
# Começo de cada gravação jogado fora: o mic do headset dá um tranco de ~−6 dBFS ao abrir o stream
# (Fase 9). O aviso "● gravando" só aparece depois dele, então a fala não cai no trecho descartado.
LEAD_IN_S = 0.5


@dataclass(frozen=True)
class Take:
    id: str
    part: str
    label: str  # wake | no_wake | echo_wake | echo_only | ambient
    text: str | None  # o que dizer (None: ficar calado)
    hint: str
    duration_s: float
    reply: str | None = None  # parte eco: arquivo da resposta tocada


def speech_duration(text: str) -> float:
    """Tempo de gravação para uma frase: folga para reagir + ~0,45 s por palavra."""
    return round(max(2.5, 1.5 + 0.45 * len(text.split())), 1)


def build_plan(parts: list[str], replies: list[Path] | None = None, ambient_min: int = 10) -> list[Take]:
    plan: list[Take] = []

    def add(part, label, text, hint, duration, reply=None):
        n = sum(1 for t in plan if t.part == part) + 1
        plan.append(Take(f"{part}-{n:03d}", part, label, text, hint, duration, reply))

    for part in parts:
        if part == "perto":
            for rep in range(NEAR_REPEATS):
                for text in NEAR:
                    add(part, "wake", text, "voz normal", speech_duration(text))
        elif part == "variado":
            for rep in range(VARIED_REPEATS):
                for text, hint in VARIED:
                    add(part, "wake", text, hint, speech_duration(text))
        elif part == "longe":
            for text in FAR:
                add(part, "wake", text, "de longe, voz normal", speech_duration(text))
        elif part == "negativas":
            for text in NEGATIVES:
                add(part, "no_wake", text, "voz normal", speech_duration(text))
        elif part == "eco":
            for i, reply in enumerate(replies or []):
                text = INTERRUPTIONS[i % len(INTERRUPTIONS)]
                label, hint = ("echo_wake", "no meio da fala dela") if text else ("echo_only", "fique calado")
                add(part, label, text, hint, round(wav_duration(reply) + ECO_TAIL_S, 1), reply.name)
        elif part == "ambiente":
            for _ in range(ambient_min):
                add(part, "ambient", None, "não fale nada", AMBIENT_CHUNK_S)
        elif part == "jargao":
            for text in JARGON:
                add(part, "stt", text, "voz normal, como no dia a dia", speech_duration(text) + 1.0)
        else:
            raise ValueError(f"parte desconhecida: {part}")
    return plan


# --- Áudio -----------------------------------------------------------------------------

def write_wav(path: Path, pcm: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(pcm, dtype="<i2").tobytes())


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """WAV PCM 16 bits; estéreo vira mono pela média."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise FatalError(f"{path}: só WAV de 16 bits")
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        channels, rate = w.getnchannels(), w.getframerate()
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1).astype("<i2")
    return pcm, rate


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def resample(pcm: np.ndarray, rate_in: int, rate_out: int) -> np.ndarray:
    """Reamostragem para a parte eco (22 050 → 16 000 Hz): passa-baixa FIR + interpolação."""
    if rate_in == rate_out:
        return pcm.astype("<i2")
    x = pcm.astype(np.float64)
    if rate_out < rate_in:  # corta acima da nova Nyquist para não dobrar frequências
        cutoff = 0.45 * rate_out / rate_in  # fração da taxa de entrada
        n = np.arange(-64, 65)
        taps = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(len(n))
        x = np.convolve(x, taps / taps.sum())[len(n) // 2 : len(n) // 2 + len(x)]  # "same" erra se x < filtro
    t_out = np.arange(int(len(x) * rate_out / rate_in)) * rate_in / rate_out
    y = np.interp(t_out, np.arange(len(x)), x)
    return np.clip(np.round(y), -32768, 32767).astype("<i2")


def levels(pcm: np.ndarray) -> tuple[float, float]:
    """(pico em dBFS, RMS em dBFS do trecho de 300 ms mais forte)."""
    x = pcm.astype(np.float64) / 32768
    if not len(x):
        return -120.0, -120.0
    peak = float(np.abs(x).max())
    win = int(0.3 * SAMPLE_RATE)
    if len(x) >= win:
        energy = np.convolve(x * x, np.ones(win) / win, mode="valid").max()
    else:
        energy = float(np.mean(x * x))
    db = lambda v: round(20 * np.log10(max(v, 1e-6)), 1)  # noqa: E731
    return db(peak), db(float(np.sqrt(energy)))


def level_warning(peak_db: float, rms_db: float, label: str) -> str | None:
    if peak_db >= -0.1:
        return "estourou (som saturado): fale um pouco mais longe ou baixe o ganho do microfone"
    if label in ("wake", "no_wake"):
        if rms_db < -40:
            return "muito baixo: confira se o microfone certo está selecionado (--device)"
        # Voz tem pico ~8–15 dB acima do RMS; estalo ou chiado constante fica a 1–2 dB.
        if peak_db - rms_db < 4:
            return "não parece voz (estalo ou ruído constante): confira o conector do microfone"
    return None


def select_redo(plan: list[Take], specs: list[str]) -> list[Take]:
    """Falas a regravar: ids ("perto-003") ou intervalos na ordem do roteiro ("perto-001..perto-020")."""
    ids = [t.id for t in plan]
    chosen: set[str] = set()
    for spec in specs:
        first, _, last = spec.partition("..")
        last = last or first
        for name in (first, last):
            if name not in ids:
                raise FatalError(f"--redo: {name} não está no roteiro (veja --list)")
        a, b = ids.index(first), ids.index(last)
        if a > b:
            raise FatalError(f"--redo: intervalo invertido: {spec}")
        chosen.update(ids[a : b + 1])
    return [t for t in plan if t.id in chosen]


# --- Manifest --------------------------------------------------------------------------

def load_done(manifest: Path) -> set[str]:
    done = set()
    if manifest.is_file():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["id"])
    return done


def append_manifest(manifest: Path, entry: dict) -> None:
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# --- Gravação --------------------------------------------------------------------------

class Recorder:
    def __init__(self, out: Path, device, output_device, replies_dir: Path | None):
        self.sd = sounddevice()
        self.out = out
        self.device = device
        self.output_device = output_device
        self.replies_dir = replies_dir
        self.manifest = out / "manifest.jsonl"
        try:
            self.sd.check_input_settings(device=device, samplerate=SAMPLE_RATE, channels=1, dtype="int16")
        except (self.sd.PortAudioError, ValueError) as e:
            raise FatalError(f"o microfone não aceita 16 kHz mono: {e} (veja --list-devices e use --device)")
        self.device_name = self.sd.query_devices(device, "input")["name"]
        self.output_name = self.sd.query_devices(output_device, "output")["name"]

    def _record(self, take: Take) -> tuple[np.ndarray, np.ndarray | None]:
        lead = int(LEAD_IN_S * SAMPLE_RATE)
        frames = int(take.duration_s * SAMPLE_RATE)
        ref = None
        if take.reply:
            pcm, rate = read_wav(self.replies_dir / take.reply)
            ref = np.zeros(lead + frames, dtype="<i2")  # a resposta começa depois do trecho descartado
            played = resample(pcm, rate, SAMPLE_RATE)[:frames]
            ref[lead : lead + len(played)] = played
            # entrada e saída no mesmo stream: as duas ficam alinhadas (atraso constante)
            try:
                rec = self.sd.playrec(ref.reshape(-1, 1), samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                      device=(self.device, self.output_device))
            except self.sd.PortAudioError as e:
                raise FatalError(f"não foi possível abrir microfone e caixas juntos: {e}\n"
                                 "Escolha os dois do mesmo tipo (ex.: ambos [MME]) com --device e "
                                 "--output-device; veja --list-devices.")
        else:
            rec = self.sd.rec(lead + frames, samplerate=SAMPLE_RATE, channels=1, dtype="int16", device=self.device)
        start = time.monotonic()
        while (left := LEAD_IN_S + take.duration_s - (time.monotonic() - start)) > 0:
            if left > take.duration_s:
                print("\r  ○ espere...          ", end="", flush=True)
            else:
                print(f"\r  ● gravando... {left:4.1f} s ", end="", flush=True)
            time.sleep(0.1)
        self.sd.wait()
        print("\r" + " " * 30 + "\r", end="")
        return rec[lead:, 0].copy(), (ref[lead:] if ref is not None else None)

    def save(self, take: Take, mic: np.ndarray, ref: np.ndarray | None) -> dict:
        path = self.out / take.part / f"{take.id}.wav"
        write_wav(path, mic)
        entry = {**asdict(take), "file": f"{take.part}/{take.id}.wav", "sample_rate": SAMPLE_RATE,
                 "device": self.device_name, "recorded_at": datetime.now().isoformat(timespec="seconds")}
        if ref is not None:
            write_wav(self.out / take.part / f"{take.id}.ref.wav", ref)
            entry["ref_file"] = f"{take.part}/{take.id}.ref.wav"
            entry["output_device"] = self.output_name
        entry["peak_db"], entry["rms_db"] = levels(mic)
        return entry

    def run(self, plan: list[Take], redo: list[Take] | None = None) -> None:
        print(f"Microfone: {self.device_name}")
        if redo:
            todo = redo
            print(f"Regravando {len(todo)} falas.\n")
        else:
            done = load_done(self.manifest)
            todo = [t for t in plan if t.id not in done]
            if not todo:
                print("Nada a gravar: tudo já está no manifest.")
                return
            print(f"{len(plan) - len(todo)} de {len(plan)} já gravadas; faltam {len(todo)} "
                  f"(~{sum(t.duration_s + 3 for t in todo) / 60:.0f} min).\n")
        part = None
        i = 0
        while i < len(todo):
            take = todo[i]
            if take.part != part:
                part = take.part
                print(f"=== {part}: {PARTS[part]} ===")
            what = f"diga: «{take.text}»" if take.text else "fique calado"
            if take.reply:
                what += f" ({take.hint}, resposta {take.reply}, {take.duration_s:g} s)"
            elif take.text:
                what += f"  ({take.hint})"
            else:
                what += f" ({take.duration_s:g} s)"
            print(f"[{take.id}] {what}")
            key = input("  Enter grava · p pula · q sai > ").strip().lower()
            if key == "q":
                print("Até depois: rode o mesmo comando para continuar.")
                return
            if key == "p":
                i += 1
                continue
            mic, ref = self._record(take)
            entry = self.save(take, mic, ref)
            warning = level_warning(entry["peak_db"], entry["rms_db"], take.label)
            print(f"  ✓ salvo (pico {entry['peak_db']} dBFS)" + (f"  ⚠ {warning}" if warning else ""))
            if warning and input("  Gravar de novo? (s/N) > ").strip().lower() == "s":
                continue  # sobrescreve o WAV; o manifest fica com a última linha
            append_manifest(self.manifest, entry)
            i += 1
        print("\nPronto! Mande a pasta para o servidor (veja client/README.md).")


# --- CLI -------------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="gravacoes", help="pasta das gravações (padrão: gravacoes)")
    parser.add_argument("--part", nargs="+", choices=list(PARTS), default=DEFAULT_PARTS, metavar="PARTE",
                        help=f"partes a gravar, em ordem (padrão: as da wake word): {', '.join(PARTS)}")
    parser.add_argument("--replies", help="pasta com os WAVs das respostas da Agatha (parte eco)")
    parser.add_argument("--minutes", type=int, default=10, help="minutos da parte ambiente (padrão: 10)")
    parser.add_argument("--device", help="índice ou nome do microfone (veja --list-devices)")
    parser.add_argument("--output-device", help="índice ou nome da saída de som (parte eco)")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--list", action="store_true", help="mostra o roteiro e sai")
    parser.add_argument("--redo", nargs="+", metavar="ID",
                        help="regrava estas falas: ids ou intervalos (ex.: perto-001..perto-020)")
    args = parser.parse_args()

    from .__main__ import device_arg

    replies = None
    if args.replies:
        replies = sorted(p for p in Path(args.replies).glob("*.wav") if not p.name.endswith(".ref.wav"))
        if not replies:
            parser.error(f"nenhum .wav em {args.replies}")
    elif "eco" in args.part and not (args.list or args.list_devices or args.redo):
        parser.error("a parte eco precisa de --replies (pasta com as respostas da Agatha); "
                     "para pular, escolha as partes com --part")
    try:
        if args.list_devices:
            list_devices()
            return 0
        plan = build_plan(args.part, replies, args.minutes)
        if args.list:
            for t in plan:
                print(f"{t.id:14s} {t.label:9s} {t.duration_s:5.1f} s  {t.text or '-'}  ({t.hint})")
            print(f"\n{len(plan)} gravações, ~{sum(t.duration_s + 3 for t in plan) / 60:.0f} min")
            return 0
        redo = select_redo(plan, args.redo) if args.redo else None
        Recorder(Path(args.out), device_arg(args.device), device_arg(args.output_device),
                 Path(args.replies) if args.replies else None).run(plan, redo)
    except FatalError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrompido: rode o mesmo comando para continuar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
