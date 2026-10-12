"""Gera amostras sintéticas para treinar a wake word "Agatha" (Fase 5, etapa 2).

Positivos: o nome falado pelas vozes PT-BR do Piper (cadu, edresson, faber, jeff), sozinho ou
depois de uma saudação curta ("Ei, Agatha"), com variação de velocidade, entonação e pronúncia.
O clipe termina no fim do nome, que é onde o detector precisa disparar.

Negativos:
- frases parecidas ("a gata", "agora", "a garrafa"...) nas mesmas vozes e na voz da Agatha;
- respostas e perguntas reais (bench/results e logs de turnos) na voz da Agatha ("dii"), para ela
  não se acionar com a própria voz saindo pelas caixas.

A voz "dii" fica fora dos positivos de propósito: se ela disser o próprio nome numa resposta, o
detector não pode interromper a fala dela.

Uso (venv do treino, ver README.md desta pasta):
    nice -n 19 .venv-wake/bin/python client/wakeword/generate.py --out /dados/agatha/data/wakeword/sintetico
"""

import argparse
import glob
import json
import random
import re
import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[2]
VOICES_DIR = Path("/dados/agatha/models/tts/piper")
POSITIVE_VOICES = ["pt_BR-cadu-medium", "pt_BR-edresson-low", "pt_BR-faber-medium", "pt_BR-jeff-medium"]
AGATHA_VOICE = "dii_pt-BR"
SAMPLE_RATE = 16000

# "Agatha" e "Ágata" saem iguais no Piper (ˈa-ga-ta); "Agáta" tem o acento na 2ª sílaba.
NAMES = ["Agatha"] * 6 + ["Agáta"] * 1
ENDINGS = ["", "!", "?", ",", "..."]
PREFIXES = [""] * 8 + ["Ei, ", "Oi, ", "Ok, ", "Ô ", "Olá, ", "Bom dia, ", "Obrigado, ", "Certo, ",
                       "Escuta, ", "Ô, ", "E aí, ", "Valeu, ", "Boa noite, ", "Por favor, "]

# Parecidas com o nome (ou começando igual), em várias posições da frase
CONFUSABLE = [
    "a gata", "a gata dormiu", "a gata subiu no telhado", "agora", "agora sim", "agora não dá",
    "a garota", "a garrafa", "a batata", "a lata", "a data", "a pata", "a mata", "a nata", "a rata",
    "a gaveta", "a galera", "a gente", "a gaita", "o gato", "Renata", "Beata", "pirata", "barata",
    "a sacada", "a garagem", "Agamenon", "ágil", "a gasolina", "a gota", "agosto", "aguenta aí",
    "a guarita", "acabou", "a capa", "a cada hora", "a tabela", "aguarda", "achei", "agachar",
    "a taxa", "Atacama", "a casa", "a massa", "a graça", "a gruta", "a grade", "a granada",
    "água gelada", "a gangue", "Agnaldo", "Ágda", "a goiaba", "a gema", "a gola", "a gata preta",
    "agrada", "a gravata", "Gaga", "agasalho", "agenda", "a grata surpresa", "a gata da vizinha",
    "olha a gata", "cadê a gata", "agora vai", "é agora", "a garçonete", "a galinha", "a agulha",
]
NAME_RE = re.compile(r"(?i)\b(ag[aá]th?a|[áa]gata)\b")


def collect_agatha_texts() -> list[str]:
    """Respostas e perguntas reais, sem o nome: viram fala negativa na voz da Agatha."""
    texts: set[str] = set()

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in ("response", "reply", "answer", "text", "resposta") and isinstance(value, str):
                    texts.add(value.strip())
                else:
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    for path in glob.glob(str(ROOT / "bench" / "results" / "*.json")):
        if "/wake-" not in path:  # os do benchmark da wake word têm as falas do Reinaldo
            walk(json.loads(Path(path).read_text(encoding="utf-8")))
    for path in glob.glob("/dados/agatha/logs/turns-*.jsonl"):
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("event") == "response" and row.get("text"):
                texts.add(row["text"])
    return sorted(t for t in texts if 15 < len(t) < 400 and not NAME_RE.search(t))


class Voices:
    def __init__(self):
        from piper import PiperVoice, SynthesisConfig

        self._cfg = SynthesisConfig
        self._voices = {}
        self._load = lambda name: PiperVoice.load(str(VOICES_DIR / f"{name}.onnx"))

    def say(self, voice: str, text: str, length: float, noise: float, noise_w: float) -> np.ndarray:
        if voice not in self._voices:
            self._voices[voice] = self._load(voice)
        v = self._voices[voice]
        cfg = self._cfg(length_scale=length, noise_scale=noise, noise_w_scale=noise_w)
        chunks = list(v.synthesize(text, cfg))
        audio = np.concatenate([c.audio_int16_array for c in chunks]).astype(np.float32)
        rate = chunks[0].sample_rate
        if rate != SAMPLE_RATE:
            g = np.gcd(rate, SAMPLE_RATE)
            audio = resample_poly(audio, SAMPLE_RATE // g, rate // g)
        return audio


def trim(audio: np.ndarray, pad_s: float = 0.05) -> np.ndarray:
    """Corta o silêncio das pontas (o fim do clipe tem de ser o fim do nome)."""
    frames = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1)
    loud = np.where(db > db.max() - 40)[0]
    if not len(loud):
        return audio
    pad = int(pad_s * SAMPLE_RATE)
    return audio[max(0, loud[0] * 160 - pad) : min(len(audio), (loud[-1] + 1) * 160 + pad)]


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(np.clip(np.round(audio), -32768, 32767).astype("<i2").tobytes())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--positives", type=int, default=4000)
    parser.add_argument("--confusable", type=int, default=2500)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    rnd = random.Random(args.seed)
    voices = Voices()
    manifest = []
    plan = []
    for i in range(args.positives):
        text = rnd.choice(PREFIXES) + rnd.choice(NAMES) + rnd.choice(ENDINGS)
        plan.append(("positivos", f"pos-{i:05d}", rnd.choice(POSITIVE_VOICES), text, True))
    for i in range(args.confusable):
        text = rnd.choice(CONFUSABLE)
        if rnd.random() < 0.4:  # dentro de uma frase
            text = f"{text}, {rnd.choice(['viu', 'né', 'que horas são', 'olha só', 'tá bom', 'de novo'])}"
        plan.append(("negativos", f"conf-{i:05d}", rnd.choice(POSITIVE_VOICES + [AGATHA_VOICE]), text, True))
    for i, text in enumerate(collect_agatha_texts()):
        plan.append(("negativos", f"resp-{i:05d}", AGATHA_VOICE, text, False))

    for sub in ("positivos", "negativos"):
        (args.out / sub).mkdir(parents=True, exist_ok=True)
    for n, (sub, name, voice, text, do_trim) in enumerate(plan, 1):
        path = args.out / sub / f"{name}.wav"
        params = {"length": round(rnd.uniform(0.8, 1.35), 2), "noise": round(rnd.uniform(0.4, 0.9), 2),
                  "noise_w": round(rnd.uniform(0.5, 1.1), 2)}
        if not path.exists():
            audio = voices.say(voice, text, **params)
            write_wav(path, trim(audio) if do_trim else audio)
        manifest.append({"id": name, "file": f"{sub}/{name}.wav", "label": sub[:-1], "voice": voice,
                         "text": text, **params})
        if n % 500 == 0 or n == len(plan):
            print(f"{n}/{len(plan)}", file=sys.stderr, flush=True)
    (args.out / "manifest.jsonl").write_text(
        "".join(json.dumps(m, ensure_ascii=False) + "\n" for m in manifest), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
