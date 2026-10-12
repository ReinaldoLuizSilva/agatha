"""Sonda do openWakeWord (Fase 5, etapa 2): o embedding dele separa "Agatha" do resto?

Antes de investir no treino completo (horas de GPU e ~20 GB de dados), testa se o embedding de
áudio pré-treinado do openWakeWord (independente de idioma) distingue o "Agatha" do Reinaldo de
fala comum e das negativas difíceis ("a gata", "agora"). Treina um classificador pequeno sobre
janelas de 16 embeddings (o mesmo formato dos modelos do openWakeWord) só com as gravações, em
duas dobras: treino e teste nunca compartilham falas. Os positivos de eco ficam só no teste e
ainda estão SEM cancelamento de eco.

Sem marcação de onde o nome está na fala, usa o roteiro: o nome vem no começo ("Agatha, ...") ou no
fim ("Obrigado, Agatha"), com o início e o fim da fala pela energia (gravações em sala quieta).

Uso: .venv-wake/bin/python bench/wake_probe.py      (dependências em bench/requirements-wake.txt)
"""
import json, sys, wave
from datetime import datetime
from pathlib import Path
import numpy as np
from openwakeword.utils import AudioFeatures
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

DATA = Path("/dados/agatha/data/wakeword/gravacoes")
FLEURS = sorted(Path("/dados/agatha/data/fleurs/pt_br/dev").glob("*.wav"))
SR = 16000
WIN = 16
F = AudioFeatures(inference_framework="onnx", ncpu=3)
rng = np.random.default_rng(0)


def read(p):
    try:
        w = wave.open(str(p)); return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()
    except wave.Error:
        import soundfile as sf
        x, _ = sf.read(str(p), dtype="float32"); return np.clip(np.round(x * 32767), -32768, 32767).astype("<i2")


def embed(x):
    x = np.concatenate([np.zeros(SR, dtype="<i2"), x, np.zeros(SR // 2, dtype="<i2")])  # contexto
    e = F._get_embeddings(x)  # (T, 96); embedding t termina em ~0,76 + 0,08 t s
    return e, 1.0  # deslocamento do áudio original


def windows(e):
    if len(e) < WIN: return np.zeros((0, WIN * 96)), np.zeros(0)
    idx = np.arange(WIN - 1, len(e))
    W = np.stack([e[i - WIN + 1 : i + 1].ravel() for i in idx])
    end_s = 0.76 + 0.08 * idx
    return W, end_s


def speech_bounds(x):
    f = x.astype(float).reshape(-1, 320)[: len(x) // 320]
    db = 10 * np.log10((f ** 2).mean(1) + 1)
    live = db[db > 3]  # sem os ~300 ms de zeros do começo de cada gravação
    if not len(live): return None
    floor = np.percentile(live, 10)
    on = np.where(db > floor + 15)[0]
    if not len(on): return None
    return on[0] * 0.02, (on[-1] + 1) * 0.02


rows = {}
for l in (DATA / "manifest.jsonl").read_text(encoding="utf-8").splitlines():
    r = json.loads(l); rows[r["id"]] = r

items = []
for r in rows.values():
    group = {"wake": r["part"], "no_wake": "negativas", "echo_wake": "eco-fala", "echo_only": "eco-calado", "ambient": "ambiente"}[r["label"]]
    items.append(dict(id=r["id"], group=group, text=r["text"], path=DATA / r["file"]))
items += [dict(id="fleurs-" + p.stem, group="fleurs", text=None, path=p) for p in FLEURS]

print("embeddings...", file=sys.stderr)
for n, it in enumerate(items):
    x = read(it["path"])
    it["dur"] = len(x) / SR
    e, off = embed(x)
    it["W"], end_s = windows(e)
    it["end_s"] = end_s - off  # em segundos do áudio original
    if it["group"] in ("perto", "variado", "longe", "eco-fala") and it["text"]:
        b = speech_bounds(x) if it["group"] != "eco-fala" else None
        it["bounds"] = b
    if n % 100 == 0: print(n, len(items), file=sys.stderr)


def name_end(it):
    """Instante aproximado do fim de "Agatha" na fala (roteiro: nome no começo ou no fim)."""
    b = it.get("bounds")
    if not b: return None
    words = it["text"].replace(",", "").replace(".", "").replace("!", "").replace("?", "").split()
    if words[0].lower() == "agatha": return b[0] + 0.65
    if words[-1].lower() == "agatha": return b[1]
    return b[0] + 0.25 + 0.65  # "Ei, Agatha", "Oi, Agatha", "Certo, Agatha"


def split_items(fold):
    pos_tr, pos_te, neg_tr, neg_te = [], [], [], []
    for it in items:
        g, i = it["group"], it["id"]
        num = int(i.rsplit("-", 1)[-1])  # FLEURS: o nome do arquivo é numérico
        if g in ("perto", "variado", "longe"):
            # dobra 0: testa longe + 2ª rodada de variado; dobra 1: testa 1ª rodada de perto e variado
            test = (g == "longe" or (g == "variado" and num > 12)) if fold == 0 else (g in ("perto", "variado") and (num <= 20 if g == "perto" else num <= 12))
            (pos_te if test else pos_tr).append(it)
        elif g == "eco-fala":
            pos_te.append(it)
        else:
            (neg_te if num % 2 == fold else neg_tr).append(it)
    return pos_tr, pos_te, neg_tr, neg_te


def train_set(pos_tr, neg_tr):
    X, y = [], []
    for it in pos_tr:
        t = name_end(it)
        if t is None: continue
        sel = np.abs(it["end_s"] - t) <= 0.16  # janelas que terminam perto do fim do nome
        X.append(it["W"][sel]); y += [1] * sel.sum()
        far = (it["end_s"] < t - 0.6) | (it["end_s"] > t + 1.0)  # resto da frase: negativo difícil
        X.append(it["W"][far]); y += [0] * far.sum()
    for it in neg_tr:
        W = it["W"]
        if it["group"] == "fleurs" and len(W) > 40: W = W[rng.choice(len(W), 40, replace=False)]
        X.append(W); y += [0] * len(W)
    return np.concatenate(X), np.array(y)


def fires(scores, end_s, thr, refractory=2.0):
    n, last = 0, -1e9
    for s, t in zip(scores, end_s):
        if s >= thr and t - last >= refractory: n += 1; last = t
    return n


THRS = (0.5, 0.7, 0.8, 0.9, 0.95, 0.98, 0.99)
REPORT = []
_print = print

def print(*args, **kwargs):
    if kwargs.get("file") is None:
        REPORT.append(" ".join(str(a) for a in args))
    _print(*args, **kwargs)

for kind in ("logreg", "mlp"):
    agg = {thr: {"pos": [], "neg": {}} for thr in THRS}
    for fold in (0, 1):
        pos_tr, pos_te, neg_tr, neg_te = split_items(fold)
        X, y = train_set(pos_tr, neg_tr)
        clf = (make_pipeline(StandardScaler(), LogisticRegression(C=0.05, class_weight="balanced", max_iter=3000))
               if kind == "logreg" else
               make_pipeline(StandardScaler(), MLPClassifier((64,), alpha=1e-2, max_iter=400, random_state=0)))
        if kind == "mlp":  # sem class_weight: repete os positivos
            reps = max(1, int((y == 0).sum() / max(1, (y == 1).sum()) / 4))
            X = np.concatenate([X] + [X[y == 1]] * reps); y = np.concatenate([y] + [y[y == 1]] * reps)
        clf.fit(X, y)
        for it in pos_te + neg_te:
            it["score"] = clf.predict_proba(it["W"])[:, 1] if len(it["W"]) else np.zeros(0)
        for thr in THRS:
            for it in pos_te:
                agg[thr]["pos"].append((it["group"], it["score"].max(initial=0) >= thr))
            for it in neg_te:
                d = agg[thr]["neg"].setdefault(it["group"], [0, 0, 0.0])
                f = fires(it["score"], it["end_s"], thr)
                d[0] += f; d[1] += f > 0; d[2] += it["dur"] / 3600
    print(f"\n### {kind} (2 dobras; positivos de teste: {len(agg[THRS[0]]['pos'])})")
    print("| limiar | perto | variado | longe | eco-fala (sem AEC) | negativas | eco-calado | ambiente | fleurs | disparos/h (eco calado+ambiente+FLEURS) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for thr in THRS:
        a = agg[thr]
        rec = {}
        for g, hit in a["pos"]: rec.setdefault(g, []).append(hit)
        cells = [f"{np.mean(rec[g]):.0%} ({len(rec[g])})" for g in ("perto", "variado", "longe", "eco-fala")]
        neg = a["neg"]
        cells += [f"{neg[g][0]}" for g in ("negativas", "eco-calado", "ambiente", "fleurs")]
        h = sum(neg[g][2] for g in ("eco-calado", "ambiente", "fleurs")); f = sum(neg[g][0] for g in ("eco-calado", "ambiente", "fleurs"))
        print(f"| {thr} | " + " | ".join(cells) + f" | {f / h:.1f} |")
    print("horas de negativos testados:", {g: round(v[2], 3) for g, v in agg[THRS[0]]["neg"].items()})

out = Path(__file__).resolve().parent / "results" / f"wake-probe-{datetime.now():%Y%m%d-%H%M%S}.md"
out.write_text("\n".join(REPORT) + "\n", encoding="utf-8")
_print(f"\n→ {out}")
