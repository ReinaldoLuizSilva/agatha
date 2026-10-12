"""Treina e avalia o detector da wake word "Agatha" para o cliente (Fase 5, etapa 2).

Usa o pipeline do openWakeWord: melspectrograma → embedding pré-treinado (96 valores a cada
80 ms) → classificador pequeno sobre janelas de 16 embeddings (~2 s de áudio), que dispara quando
o nome termina no fim da janela. Só o classificador é treinado, no CPU.

Dados:
- positivos: gravações do Reinaldo (python -m agatha_client.record) e amostras sintéticas
  (generate.py), com ruído, reverberação e volume aleatórios;
- negativos: 2.000 h de áudio variado já convertido em embeddings (ACAV100M, do openWakeWord),
  frases parecidas e respostas da Agatha (generate.py), FLEURS PT-BR, ambiente e eco gravados,
  e o resto das frases depois do nome.

Avaliação honesta em duas dobras, com a mesma divisão de bench/wake_probe.py: as falas de teste
do Reinaldo nunca entram no treino daquela dobra. Os positivos de eco (sem cancelamento de eco)
ficam só no teste. Falsos disparos medidos também nas 10,7 h do conjunto de validação do
openWakeWord.

Uso (venv do treino, ver README.md desta pasta), sempre com teto de memória e prioridade baixa
para não atrapalhar a Agatha:
    systemd-run --user --scope -p MemoryMax=5G -p MemorySwapMax=0 nice -n 19 \
        .venv-wake/bin/python client/wakeword/train.py --eval     # 2 dobras + tabela
    (idem) ... train.py --final                                   # usa tudo, exporta ONNX
"""

import argparse
import copy
import json
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "client"))  # agatha_client.aec: o mesmo cancelamento de eco do cliente
DATA = Path("/dados/agatha/data/wakeword")
RECORDINGS = DATA / "gravacoes"
SYNTHETIC = DATA / "sintetico"
FEATURES = DATA / "features"
ACAV = DATA / "oww" / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VALIDATION = DATA / "oww" / "validation_set_features.npy"
FLEURS = Path("/dados/agatha/data/fleurs/pt_br/dev")
MODEL_OUT = Path("/dados/agatha/models/wakeword/agatha.onnx")
RESULTS = ROOT / "bench" / "results"

SR = 16000
WIN = 16  # embeddings por janela
EMB = 96
STEP_S = 0.08  # um embedding a cada 80 ms
FIRST_END_S = 0.76  # o 1º embedding cobre o áudio até ~0,76 s
# Silêncio antes e depois de cada gravação: a 1ª janela completa só termina em ~1,96 s, e o nome
# costuma terminar antes disso. No cliente o microfone é contínuo e sempre há áudio antes.
LEAD_S, TAIL_S = 1.5, 0.5
# Cancelamento de eco (bench/aec_bench.py): o eco chega ~289 ms depois do que foi tocado; o filtro
# começa 16 ms antes, porque não modela eco que chegue antes do atraso informado.
AEC_DELAY_MS, AEC_MU = 273, 1.0
ECHO_GROUPS = ("eco-fala", "eco-calado")
THRESHOLDS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99)
REFRACTORY_S = 2.0
POS_GROUPS = ("perto", "variado", "longe", "eco-fala")
NEG_GROUPS = ("negativas", "eco-calado", "ambiente", "fleurs")
rng = np.random.default_rng(0)


# --- Áudio e embeddings ----------------------------------------------------------------

def read(path: Path) -> np.ndarray:
    """float32 na escala de int16 (como o openWakeWord espera)."""
    try:
        with wave.open(str(path), "rb") as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    except wave.Error:
        import soundfile as sf

        audio, _ = sf.read(str(path), dtype="float32")
        return audio * 32767


_features = None


def embed(audio: np.ndarray) -> np.ndarray:
    """(T, 96): o embedding t cobre o áudio até FIRST_END_S + t * STEP_S."""
    global _features
    if _features is None:
        from openwakeword.utils import AudioFeatures

        _features = AudioFeatures(inference_framework="onnx", ncpu=3)
    audio = np.clip(audio, -32768, 32767).astype(np.int16)
    if len(audio) < int((FIRST_END_S + 0.1) * SR):
        audio = np.concatenate([np.zeros(int(FIRST_END_S * SR), np.int16), audio])
    return _features._get_embeddings(audio).astype(np.float32)


def embed_padded(audio: np.ndarray) -> np.ndarray:
    """Embeddings com LEAD_S de silêncio antes e TAIL_S depois (tempos: some LEAD_S de volta)."""
    return embed(np.concatenate([np.zeros(int(LEAD_S * SR), np.float32), audio,
                                 np.zeros(int(TAIL_S * SR), np.float32)]))


def windows(emb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Janelas de 16 embeddings e o instante (s) em que cada uma termina."""
    if len(emb) < WIN:
        return np.zeros((0, WIN, EMB), np.float32), np.zeros(0)
    idx = np.arange(WIN - 1, len(emb))
    return np.stack([emb[i - WIN + 1 : i + 1] for i in idx]), FIRST_END_S + STEP_S * idx


def window_at(emb: np.ndarray, end_s: float) -> np.ndarray | None:
    i = int(round((end_s - FIRST_END_S) / STEP_S))
    return emb[i - WIN + 1 : i + 1] if WIN - 1 <= i < len(emb) else None


# --- Aumento de dados ------------------------------------------------------------------

class NoiseBank:
    """Ruídos de fundo: ambiente gravado, fala (FLEURS), voz da Agatha e ruído branco/rosa."""

    def __init__(self, paths: list[Path]):
        self.clips = [read(p) for p in paths]
        self.clips = [c for c in self.clips if len(c) > SR]

    def sample(self, n: int) -> np.ndarray:
        kind = rng.random()
        if kind < 0.15 or not self.clips:
            white = rng.normal(0, 1, n)
            if kind < 0.07:  # rosa: integra o branco
                white = np.cumsum(white)
                white -= np.convolve(white, np.ones(400) / 400, mode="same")
            return (white / (np.std(white) + 1e-9)).astype(np.float32)
        clip = self.clips[rng.integers(len(self.clips))]
        if len(clip) < n:
            clip = np.tile(clip, n // len(clip) + 1)
        start = rng.integers(0, len(clip) - n + 1)
        seg = clip[start : start + n]
        return (seg / (np.std(seg) + 1e-9)).astype(np.float32)


def reverb(x: np.ndarray) -> np.ndarray:
    """Sala sintética: caminho direto + cauda de ruído com decaimento exponencial."""
    rt60 = rng.uniform(0.15, 0.6)
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    rir = rng.normal(0, 1, n) * np.exp(-6.9 * t / rt60) * rng.uniform(0.1, 0.4)
    rir[0] = 1.0
    y = np.convolve(x, rir)[: len(x)]
    return y * (np.abs(x).max() / (np.abs(y).max() + 1e-9))


def augment(x: np.ndarray, noise: NoiseBank, clean_prob: float = 0.2) -> np.ndarray:
    y = x.copy()
    if rng.random() < 0.4:
        y = reverb(y)
    if rng.random() > clean_prob:
        snr = rng.uniform(3, 25)
        n = noise.sample(len(y))
        y = y + n * (np.std(y) + 1e-9) / (10 ** (snr / 20))
    gain = 10 ** (rng.uniform(-15, 5) / 20)
    peak = np.abs(y).max() * gain
    if peak > 32000:
        gain *= 32000 / peak
    return y * gain


# --- Gravações do Reinaldo -------------------------------------------------------------

def load_recordings() -> list[dict]:
    rows = {}
    for line in (RECORDINGS / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            rows[row["id"]] = row
    group = {"no_wake": "negativas", "echo_wake": "eco-fala", "echo_only": "eco-calado", "ambient": "ambiente"}
    items = [{"id": r["id"], "group": group.get(r["label"], r["part"]), "text": r["text"],
              "path": RECORDINGS / r["file"], "ref": RECORDINGS / r["ref_file"] if r.get("ref_file") else None}
             for r in rows.values()]
    items += [{"id": f"fleurs-{p.stem}", "group": "fleurs", "text": None, "path": p}
              for p in sorted(FLEURS.glob("*.wav"))]
    return items


def speech_bounds(x: np.ndarray) -> tuple[float, float] | None:
    frames = x[: len(x) // 320 * 320].reshape(-1, 320)
    db = 10 * np.log10((frames ** 2).mean(1) + 1)
    # o começo de cada gravação tem ~300 ms de zeros (o stream abrindo): fora da conta do fundo
    live = db[db > 3]
    if not len(live):
        return None
    loud = np.where(db > np.percentile(live, 10) + 15)[0]
    return (loud[0] * 0.02, (loud[-1] + 1) * 0.02) if len(loud) else None


def name_end(item: dict, x: np.ndarray) -> float | None:
    """Fim aproximado de "Agatha" na fala, pelo roteiro (nome no começo ou no fim)."""
    bounds = speech_bounds(x)
    if not bounds or not item["text"]:
        return None
    words = [w.strip(",.!?").lower() for w in item["text"].split()]
    if words[0] == "agatha":
        return bounds[0] + 0.65
    if words[-1] == "agatha":
        return bounds[1]
    return bounds[0] + 0.9  # "Ei, Agatha", "Oi, Agatha, ...", "Certo, Agatha. ..."


def is_test(item: dict, fold: int | None) -> bool:
    """Mesma divisão de bench/wake_probe.py. fold=None: nada é teste (modelo final)."""
    if fold is None:
        return False
    g, num = item["group"], int(item["id"].rsplit("-", 1)[-1])
    if g == "eco-fala":
        return True
    if g in ("perto", "variado", "longe"):
        if fold == 0:
            return g == "longe" or (g == "variado" and num > 12)
        return (g == "perto" and num <= 20) or (g == "variado" and num <= 12)
    if g == "eco-calado":  # são todas pares (o roteiro alterna interrupção e silêncio): 6 para cada lado
        return (num // 2) % 2 == fold
    return num % 2 == fold


# --- Cancelamento de eco ---------------------------------------------------------------

def run_aec(ec, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Passa microfone e referência pelo AEC em blocos de 10 ms, como o cliente."""
    n = min(len(mic), len(ref)) // 160 * 160
    out = np.concatenate([ec.process(mic[i : i + 160], ref[i : i + 160]) for i in range(0, n, 160)])
    return np.roll(out, -ec.n).astype(np.float32)  # o AEC devolve com 1 bloco de atraso


_warm_aec = None


def warm_aec(items: list[dict]):
    """AEC já adaptado à sala (passou por todas as gravações de eco calado), como no meio de uma
    sessão do cliente. Devolve uma cópia: cada uso parte do mesmo estado."""
    global _warm_aec
    if _warm_aec is None:
        from agatha_client.aec import EchoCanceller

        _warm_aec = EchoCanceller(delay_ms=AEC_DELAY_MS, mu=AEC_MU)
        for it in items:
            if it["group"] == "eco-calado":
                run_aec(_warm_aec, read(it["path"]), read(it["ref"]))
    return copy.deepcopy(_warm_aec)


def echo_mix(voice: np.ndarray, voice_bounds: tuple[float, float], echo: dict, ser_db: float,
             ec) -> tuple[np.ndarray, float]:
    """A voz do Reinaldo por cima de um trecho de eco real, já com o AEC. Devolve (áudio, início da voz)."""
    mic, ref = read(echo["path"]), read(echo["ref"])
    pre, lead = int(0.5 * SR), int(0.5 * SR)  # 0,5 s para encher a fila de atraso do AEC + 0,5 s antes da voz
    total = pre + lead + len(voice)
    if len(mic) < total:
        reps = total // len(mic) + 1
        mic, ref = np.tile(mic, reps), np.tile(ref, reps)
    start = rng.integers(0, len(mic) - total + 1)
    m, r = mic[start : start + total].copy(), ref[start : start + total]
    a, b = (int(t * SR) for t in voice_bounds)
    v_energy = float(np.mean(voice[a:b] ** 2)) + 1e-9
    e_energy = float(np.mean(m[pre + lead + a : pre + lead + b] ** 2)) + 1e-9
    m[pre + lead :] += voice * np.sqrt(e_energy / v_energy * 10 ** (ser_db / 10))
    return run_aec(ec, m, r)[pre:], lead / SR


# --- Cache de embeddings ---------------------------------------------------------------

def cached(name: str, build):
    path = FEATURES / f"{name}.npz"
    if path.exists():
        data = np.load(path, allow_pickle=True)
        return {k: data[k] for k in data.files}
    t0 = time.time()
    data = build()
    FEATURES.mkdir(parents=True, exist_ok=True)
    np.savez(path, **data)
    print(f"  {name}: {time.time() - t0:.0f} s", file=sys.stderr, flush=True)
    return data


def recording_features(items: list[dict]) -> dict:
    """Embeddings de cada gravação e do FLEURS (sem aumento), para treino e teste."""
    def build():
        out = {}
        for n, it in enumerate(items):
            x = read(it["path"])
            out[it["id"]] = embed_padded(x)
            ne = name_end(it, x) if it["group"] in ("perto", "variado", "longe") else None
            out[f"{it['id']}::meta"] = np.array([len(x) / SR, -1 if ne is None else ne])
            if it["group"] in ECHO_GROUPS:  # o que o detector do cliente vê: depois do AEC
                out[f"{it['id']}::aec"] = embed_padded(run_aec(warm_aec(items), x, read(it["ref"])))
            if n % 100 == 0:
                print(f"  gravações {n}/{len(items)}", file=sys.stderr, flush=True)
        return out
    return cached("gravacoes-v4", build)


def synthetic_rows() -> list[dict]:
    return [json.loads(line) for line in (SYNTHETIC / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]


def synthetic_positive_windows(noise: NoiseBank, variant: str, copies: int = 2) -> np.ndarray:
    """Nome sintético no fim da janela, depois de 1,5 s de fundo, com até 150 ms de folga."""
    def build():
        rows = [r for r in synthetic_rows() if r["label"] == "positivo"]
        out = []
        for n, r in enumerate(rows):
            clip = read(SYNTHETIC / r["file"])
            for _ in range(copies):
                lead, tail = int(1.5 * SR), int(rng.uniform(0, 0.15) * SR)
                x = np.concatenate([np.zeros(lead, np.float32), clip, np.zeros(tail + int(0.1 * SR), np.float32)])
                x = augment(x, noise)
                emb = embed(x)
                end = (lead + len(clip) + tail) / SR
                for jitter in (-STEP_S, 0.0):
                    w = window_at(emb, end + jitter)
                    if w is not None:
                        out.append(w)
            if n % 500 == 0:
                print(f"  sintéticos positivos {n}/{len(rows)}", file=sys.stderr, flush=True)
        return {"x": np.array(out, np.float16)}
    return cached(f"sintetico-positivos-{variant}", build)["x"]


def synthetic_negative_windows(noise: NoiseBank, variant: str) -> np.ndarray:
    def build():
        rows = [r for r in synthetic_rows() if r["label"] == "negativo"]
        out = []
        for n, r in enumerate(rows):
            clip = read(SYNTHETIC / r["file"])
            x = np.concatenate([np.zeros(int(rng.uniform(0.3, 1.5) * SR), np.float32), clip,
                                np.zeros(int(0.5 * SR), np.float32)])
            w, _ = windows(embed(augment(x, noise, clean_prob=0.4)))
            out.append(w)
            if n % 500 == 0:
                print(f"  sintéticos negativos {n}/{len(rows)}", file=sys.stderr, flush=True)
        return {"x": np.concatenate(out).astype(np.float16)}
    return cached(f"sintetico-negativos-{variant}", build)["x"]


def acav_sample(n_windows: int) -> np.ndarray:
    """Amostra das 2.000 h em blocos contínuos (o disco é mecânico: nada de leitura aleatória)."""
    def build():
        data = np.load(ACAV, mmap_mode="r")
        blocks, size = 64, n_windows // 64
        starts = np.sort(rng.choice(data.shape[0] - size, blocks, replace=False))
        return {"x": np.concatenate([np.asarray(data[s : s + size]) for s in starts]).astype(np.float16)}
    return cached(f"acav-{n_windows}", build)["x"]


# --- Modelo ----------------------------------------------------------------------------

def make_model():
    import torch.nn as nn

    return nn.Sequential(
        nn.Flatten(),
        nn.Linear(WIN * EMB, 128), nn.LayerNorm(128), nn.ReLU(),
        nn.Linear(128, 128), nn.LayerNorm(128), nn.ReLU(),
        nn.Linear(128, 1), nn.Sigmoid(),
    )


def train(real_pos: np.ndarray, syn_pos: np.ndarray, neg: np.ndarray, acav: np.ndarray, steps: int,
          synthetic_frac: float):
    """Lotes de 256 positivos (fração `synthetic_frac` sintética) + 384 negativos + 384 do ACAV."""
    import torch

    torch.manual_seed(0)
    torch.set_num_threads(3)
    batch_rng = np.random.default_rng(0)
    model = make_model()
    opt = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-2)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=1e-3, total_steps=steps)
    bce = torch.nn.BCELoss(reduction="none")
    n_syn = int(256 * synthetic_frac) if len(syn_pos) else 0
    for step in range(steps):
        p = [real_pos[batch_rng.integers(len(real_pos), size=256 - n_syn)]]
        if n_syn:
            p.append(syn_pos[batch_rng.integers(len(syn_pos), size=n_syn)])
        p = np.concatenate(p)
        n = neg[batch_rng.integers(len(neg), size=384)]
        a = acav[batch_rng.integers(len(acav), size=384)]
        x = torch.from_numpy(np.concatenate([p, n, a]).astype(np.float32))
        y = torch.cat([torch.ones(len(p)), torch.zeros(len(n) + len(a))])
        # O peso dos negativos cresce de 1 a 100: primeiro aprende o nome, depois a não disparar à
        # toa. Com teto 10 os falsos disparos na validação ficavam em 10–20/h; com 100, ~1–3/h.
        w = torch.cat([torch.ones(len(p)), torch.full((len(n) + len(a),), 1 + 99 * step / steps)])
        loss = (bce(model(x).squeeze(1).clamp(1e-6, 1 - 1e-6), y) * w).mean()
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()
        if step % 2000 == 0:
            print(f"    passo {step}/{steps} perda {loss.item():.4f}", file=sys.stderr, flush=True)
    model.eval()
    return model


def mine_hard_negatives(model, blocks: int = 50, size: int = 40_000, keep: int = 600) -> np.ndarray:
    """Negativos difíceis: janelas de outra fatia do ACAV100M em que o modelo mais dispara.

    Lê um bloco contínuo por vez (~120 MB): carregar a fatia inteira (6 GB) jogou a Agatha no swap.
    """
    data = np.load(ACAV, mmap_mode="r")
    starts = np.sort(np.random.default_rng(1).choice(data.shape[0] - size, blocks, replace=False))
    hard, above = [], 0
    for start in starts:
        block = np.asarray(data[start : start + size])
        scores = predict(model, block)
        above += int((scores >= 0.5).sum())
        hard.append(block[np.argsort(scores)[-keep:]].astype(np.float16))
    print(f"    mineração: {above} de {blocks * size} janelas acima de 0,5", file=sys.stderr, flush=True)
    return np.concatenate(hard)


def fit(real_pos, syn_pos, neg, acav, args):
    """Treina, minera negativos difíceis e treina de novo com eles (em dobro)."""
    model = train(real_pos, syn_pos, neg, acav, args.steps, args.synthetic)
    hard = mine_hard_negatives(model)
    return train(real_pos, syn_pos, np.concatenate([neg, hard, hard]), acav, args.steps, args.synthetic)


def predict(model, w: np.ndarray) -> np.ndarray:
    import torch

    if not len(w):
        return np.zeros(0)
    with torch.no_grad():
        return np.concatenate([model(torch.from_numpy(w[i : i + 4096].astype(np.float32))).squeeze(1).numpy()
                               for i in range(0, len(w), 4096)])


def count_fires(scores: np.ndarray, end_s: np.ndarray, thr: float) -> int:
    fires, last = 0, -1e9
    for s, t in zip(scores, end_s):
        if s >= thr and t - last >= REFRACTORY_S:
            fires, last = fires + 1, t
    return fires


# --- Montagem dos conjuntos e avaliação ------------------------------------------------

def recording_windows(items, feats, fold, noise: NoiseBank, pos_copies: int = 8, echo_copies: int = 4):
    """Positivos (janelas no fim do nome, com cópias aumentadas e cópias por cima do eco da Agatha,
    já com o AEC) e negativos das gravações de treino."""
    pos, neg = [], []
    echo_takes = [it for it in items if it["group"] == "eco-calado" and not is_test(it, fold)]
    for it in items:
        if is_test(it, fold) or it["group"] == "eco-fala":
            continue
        w, end_s = windows(feats[it["id"]])
        end_s = end_s - LEAD_S
        if it["group"] in ("perto", "variado", "longe"):
            ne = feats[f"{it['id']}::meta"][1]
            if ne < 0:
                continue
            near = np.abs(end_s - ne) <= 0.16
            pos.append(w[near])
            neg.append(w[(end_s < ne - 0.6) | (end_s > ne + 1.0)])  # o resto da frase
            x = read(it["path"])
            for _ in range(pos_copies):
                speed = rng.uniform(0.92, 1.08)
                from scipy.signal import resample_poly

                y = resample_poly(x, 100, int(round(100 * speed))) if abs(speed - 1) > 0.01 else x
                y = augment(y, noise)
                e2 = embed_padded(y)
                for jitter in (-STEP_S, 0.0):
                    win = window_at(e2, LEAD_S + ne / speed + jitter)
                    if win is not None:
                        pos.append(win[None])
            bounds = speech_bounds(x)
            for _ in range(echo_copies if bounds else 0):
                ser = rng.uniform(-10, 0)  # medido nas interrupções reais: voz 0 a 10 dB abaixo do eco
                mixed, voice_at = echo_mix(x, bounds, echo_takes[rng.integers(len(echo_takes))], ser,
                                           warm_aec(items))
                e3 = embed_padded(mixed)
                for jitter in (-STEP_S, 0.0):
                    win = window_at(e3, LEAD_S + voice_at + ne + jitter)
                    if win is not None:
                        pos.append(win[None])
        elif it["group"] == "eco-calado":
            neg.append(w)
            neg.append(windows(feats[f"{it['id']}::aec"])[0])  # a voz dela que sobra depois do AEC
        else:
            if it["group"] == "fleurs" and len(w) > 60:
                w = w[rng.choice(len(w), 60, replace=False)]
            neg.append(w)
    return np.concatenate(pos).astype(np.float16), np.concatenate(neg).astype(np.float16)


def validation_fires(model, thr_list) -> dict:
    v = np.load(VALIDATION).astype(np.float32)
    w = np.lib.stride_tricks.sliding_window_view(v, (WIN, EMB))[:, 0]
    scores = predict(model, w)
    end_s = np.arange(len(scores)) * STEP_S
    hours = len(v) * STEP_S / 3600
    return {str(t): round(count_fires(scores, end_s, t) / hours, 2) for t in thr_list}


def evaluate(model, items, feats, fold) -> dict:
    out = {"pos": [], "neg": {}}
    for it in items:
        if not is_test(it, fold):
            continue
        key = f"{it['id']}::aec" if it["group"] in ECHO_GROUPS else it["id"]
        w, end_s = windows(feats[key])
        scores = predict(model, w)
        end_s = end_s - LEAD_S
        dur = feats[f"{it['id']}::meta"][0]
        if it["group"] in POS_GROUPS:
            out["pos"].append({"id": it["id"], "group": it["group"], "max": float(scores.max(initial=0))})
        else:
            out["neg"].setdefault(it["group"], []).append(
                {"id": it["id"], "hours": dur / 3600, "fires": {str(t): count_fires(scores, end_s, t) for t in THRESHOLDS}})
    return out


def report(evals: list[dict], val: list[dict]) -> str:
    lines = ["| limiar | " + " | ".join(POS_GROUPS) + " | " + " | ".join(NEG_GROUPS)
             + " | falsos/h (eco calado + ambiente + FLEURS) | falsos/h (validação 10,7 h) |",
             "|---" * (3 + len(POS_GROUPS) + len(NEG_GROUPS)) + "|"]
    for t in THRESHOLDS:
        cells = []
        for g in POS_GROUPS:
            hits = [p["max"] >= t for e in evals for p in e["pos"] if p["group"] == g]
            cells.append(f"{np.mean(hits):.0%} ({len(hits)})" if hits else "-")
        f_sum = h_sum = 0.0
        for g in NEG_GROUPS:
            rows = [r for e in evals for r in e["neg"].get(g, [])]
            fires = sum(r["fires"][str(t)] for r in rows)
            cells.append(str(fires))
            if g != "negativas":
                f_sum += fires
                h_sum += sum(r["hours"] for r in rows)
        cells.append(f"{f_sum / h_sum:.1f}")
        cells.append(f"{np.mean([v[str(t)] for v in val]):.2f}")
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def export_onnx(model, path: Path) -> None:
    """Mesmo formato dos modelos do openWakeWord: entrada (1, 16, 96), saída (1, 1)."""
    import torch

    path.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(model, torch.zeros(1, WIN, EMB), str(path), input_names=["x"], output_names=["score"],
                      dynamic_axes={"x": {0: "batch"}, "score": {0: "batch"}}, opset_version=17, dynamo=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--eval", action="store_true", help="avalia em 2 dobras (não exporta)")
    parser.add_argument("--final", action="store_true", help="treina com tudo e exporta o ONNX")
    parser.add_argument("--steps", type=int, default=12000)
    parser.add_argument("--synthetic", type=float, default=0.0,
                        help="fração sintética dos positivos (padrão 0: na avaliação, os sintéticos "
                             "dobraram os falsos disparos sem melhorar o acerto na voz do Reinaldo)")
    parser.add_argument("--acav", type=int, default=800_000, help="janelas do ACAV100M carregadas na RAM")
    args = parser.parse_args()
    if not (args.eval or args.final):
        parser.error("escolha --eval e/ou --final")

    items = load_recordings()
    feats = recording_features(items)
    acav = acav_sample(args.acav)
    agatha_voice = sorted((SYNTHETIC / "negativos").glob("resp-*.wav"))[:150]

    def datasets(variant: str):
        # Ruído de fundo: só a voz da Agatha e ruído branco/rosa, na avaliação e no modelo final.
        # O ambiente e o FLEURS são teste nas dobras; e com o FLEURS por baixo dos positivos o modelo
        # final disparava 12–30 vezes por hora na validação (contra 3–9 na avaliação).
        noise = NoiseBank(list(agatha_voice))
        syn_pos = synthetic_positive_windows(noise, variant)
        syn_neg = synthetic_negative_windows(noise, variant)
        print(f"sintéticos ({variant}): {len(syn_pos)} positivos, {len(syn_neg)} negativos; ACAV: {len(acav)}",
              file=sys.stderr)
        return noise, syn_pos, syn_neg

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.eval:
        noise, syn_pos, syn_neg = datasets("eval")
        evals, vals = [], []
        for fold in (0, 1):
            rec_pos, rec_neg = recording_windows(items, feats, fold, noise)
            print(f"dobra {fold}: {len(rec_pos)} positivos e {len(rec_neg)} negativos das gravações", file=sys.stderr)
            model = fit(rec_pos, syn_pos, np.concatenate([syn_neg, rec_neg]), acav, args)
            evals.append(evaluate(model, items, feats, fold))
            vals.append(validation_fires(model, THRESHOLDS))
        table = report(evals, vals)
        print(table)
        out = RESULTS / f"wake-train-{stamp}"
        out.with_suffix(".md").write_text(
            f"# Treino da wake word: avaliação em 2 dobras ({stamp})\n\n"
            f"Positivos: gravações do Reinaldo + {args.synthetic:.0%} de sintéticos ({len(syn_pos)} janelas). "
            f"Negativos: {len(syn_neg)} janelas sintéticas, {len(acav)} do ACAV100M e mineração de "
            f"negativos difíceis. {args.steps} passos por treino.\n\n{table}\n", encoding="utf-8")
        out.with_suffix(".json").write_text(json.dumps({"evals": evals, "validation": vals}, indent=1), encoding="utf-8")
        print(f"→ {out.with_suffix('.md')}")
    if args.final:
        noise, syn_pos, syn_neg = datasets("eval")  # mesma configuração que foi avaliada
        rec_pos, rec_neg = recording_windows(items, feats, None, noise)
        model = fit(rec_pos, syn_pos, np.concatenate([syn_neg, rec_neg]), acav, args)
        export_onnx(model, MODEL_OUT)
        print(f"modelo final → {MODEL_OUT}; falsos/h na validação: {validation_fires(model, THRESHOLDS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
