"""Benchmark de LLMs (llama.cpp em container, GPU) para a Agatha.

Para cada modelo, sobe o llama-server num container Podman, faz uma pergunta de aquecimento
(coloca o prompt de sistema em cache) e mede, em streaming:
- tempo até o primeiro token (TTFT);
- tempo até a primeira frase completa e até o primeiro "trecho falável" (frase, ou pausa natural
  em vírgula/dois-pontos/travessão depois de CHUNK_MIN_CHARS), que é o que o TTS precisa para começar;
- tokens/s de geração;
- pico de VRAM da GPU inteira (inclui o que mais estiver rodando, como o STT).

Gera um JSON com os números e um Markdown com as respostas, para avaliação humana.

Uso:
    .venv/bin/python bench/llm_bench.py
    .venv/bin/python bench/llm_bench.py --models qwen3-4b-2507,gemma-3-4b
"""

import argparse
import json
import re
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

IMAGE = "localhost/agatha-llama:b11205"
MODELS_DIR = "/dados/agatha/models/llm"
RESULTS_DIR = Path(__file__).parent / "results"
CONTAINER = "agatha-llm-bench"
PORT = 8080

MODELS = {
    "qwen3.5-4b": "Qwen3.5-4B-Q4_K_M.gguf",
    "qwen3-4b-2507": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    "gemma-4-e2b": "gemma-4-E2B-it-Q4_K_M.gguf",
    "gemma-4-e4b": "gemma-4-E4B-it-Q4_K_M.gguf",
    "gemma-3-4b": "gemma-3-4b-it-Q4_K_M.gguf",
}

SYSTEM_PROMPTS = {}
SYSTEM_PROMPTS["v1"] = """\
Você é a Agatha, assistente de voz pessoal do Reinaldo. Pode chamá-lo de Reinaldo ou de Rei.
Suas respostas serão convertidas em fala, então:
- responda em português do Brasil, em uma a três frases curtas, a menos que ele peça detalhes;
- não use markdown, listas, emojis, tabelas nem blocos de código: escreva como quem fala;
- diga números, siglas e comandos de um jeito natural para ouvir;
- tom meio a meio entre profissional e descontraído: cordial e direto;
- se não souber algo ou não tiver acesso (hora atual, internet, sistemas), diga isso em vez de inventar.
O Reinaldo trabalha com infraestrutura (Linux, Oracle, Zabbix) e estuda cloud e DevOps."""

# v2: ajustes a partir da 1ª rodada (frase inicial curta para o TTS começar cedo, nada de ler
# código, sem cumprimentar a cada resposta, papéis explícitos).
SYSTEM_PROMPTS["v2"] = """\
Você é a Agatha, assistente de voz pessoal. O usuário é o Reinaldo; você o chama de Reinaldo ou de Rei.
Tudo o que você escreve será falado em voz alta, então:
- comece sempre com uma frase curta, de no máximo oito palavras; depois complemente se for útil;
- responda em português do Brasil, em até três frases no total, a menos que ele peça detalhes;
- nunca leia código, SQL ou comandos completos: diga o nome do comando ou da view e explique a ideia;
- não use markdown, listas, emojis, parênteses nem símbolos; escreva números por extenso quando curtos;
- não cumprimente nem repita o nome dele em toda resposta, só quando soar natural;
- tom meio a meio entre profissional e descontraído: cordial e direto;
- se não souber algo ou não tiver acesso (hora atual, internet, sistemas), diga isso em vez de inventar.
O Reinaldo trabalha com infraestrutura (Linux, Oracle, Zabbix) e estuda cloud e DevOps."""

# Cada item é uma conversa: lista de falas do usuário (a última é a medida).
PROMPTS = [
    ("saudação", ["Oi Agatha, tudo bem?"]),
    ("persona", ["Como você se chama e como vai me chamar?"]),
    ("dia a dia", ["Me dá uma ideia rápida de almoço com frango e arroz."]),
    ("conta", ["Quanto é quinze por cento de duzentos e quarenta?"]),
    ("sem acesso", ["Que horas são agora?"]),
    ("oracle conceito", ["Explica rapidinho o que é uma tablespace no Oracle."]),
    ("oracle prática", ["Como eu vejo o tamanho das tablespaces no Oracle?"]),
    ("zabbix conceito", ["O que é um trigger no Zabbix e pra que serve?"]),
    ("zabbix diagnóstico", ["Meu Zabbix agent parou de enviar dados. Por onde eu começo a investigar?"]),
    ("linux", ["Como eu vejo quais processos estão usando mais memória no Linux?"]),
    ("devops", ["Qual a diferença entre Podman e Docker?"]),
    ("curiosidade", ["Me conta uma curiosidade sobre o espaço."]),
    ("contexto", ["Quero aprender Kubernetes. Por onde eu começo?", "E quanto tempo leva, mais ou menos?"]),
]

SENTENCE_END = re.compile(r"[.!?…](\s|$)")
CLAUSE_END = re.compile(r"[,;:—–](\s|$)")
CHUNK_MIN_CHARS = 30  # pausa em vírgula só conta se o trecho já tiver esse tamanho


def first_chunk_end(text: str) -> bool:
    """Simula o divisor do pipeline: fim de frase, ou pausa natural num trecho já longo."""
    if SENTENCE_END.search(text):
        return True
    return any(m.start() >= CHUNK_MIN_CHARS for m in CLAUSE_END.finditer(text))
THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


# --- Container -----------------------------------------------------------------------

def podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["podman", *args], capture_output=True, text=True, check=check)


def start_server(model_file: str, ctx: int, extra: list[str]) -> None:
    podman("rm", "-f", CONTAINER, check=False)
    podman(
        "run", "-d", "--name", CONTAINER,
        "--device", "nvidia.com/gpu=all",
        "-p", f"127.0.0.1:{PORT}:8080",
        "-v", f"{MODELS_DIR}:/models:ro,z",
        IMAGE,
        "-m", f"/models/{model_file}",
        "--host", "0.0.0.0", "--port", "8080",
        "-ngl", "99",         # todas as camadas na GPU
        "-c", str(ctx),
        "-np", "1",           # um slot só: um usuário, e o KV cache não é multiplicado
        "--jinja",
        "--reasoning-budget", "0",  # sem modo "thinking": latência é prioridade
        *extra,
    )


def wait_ready(timeout: float = 300) -> float:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < timeout:
        state = podman("inspect", "-f", "{{.State.Running}}", CONTAINER, check=False).stdout.strip()
        if state != "true":
            raise RuntimeError("container encerrou:\n" + container_logs())
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if r.status == 200:
                    return time.perf_counter() - t0
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.5)
    raise RuntimeError("timeout esperando o llama-server")


def container_logs(lines: int = 40) -> str:
    return podman("logs", "--tail", str(lines), CONTAINER, check=False).stderr


def stop_server() -> None:
    podman("rm", "-f", CONTAINER, check=False)


# --- VRAM ------------------------------------------------------------------------------

def gpu_memory_used_mb() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(out.strip().splitlines()[0])


class VRAMPeak:
    """Amostra a VRAM total a cada 200 ms numa thread."""

    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            self.peak = max(self.peak, gpu_memory_used_mb())
            self._stop.wait(0.2)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


# --- Chat em streaming -----------------------------------------------------------------

def chat(messages: list[dict], max_tokens: int, seed: int) -> dict:
    body = json.dumps({
        "messages": messages,
        "stream": True,
        "max_tokens": max_tokens,
        "temperature": 0.6,
        "seed": seed,
        "cache_prompt": True,
        # desliga o modo "thinking" nos modelos que têm (Qwen3.5, Gemma 4); os outros ignoram
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    first_token = first_sentence = first_chunk = None
    text = ""
    timings = {}
    with urllib.request.urlopen(req, timeout=120) as r:
        for raw in r:
            line = raw.decode().strip()
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            timings = chunk.get("timings", timings)
            choices = chunk.get("choices") or []
            delta = choices[0].get("delta", {}).get("content") if choices else None
            if not delta:
                continue
            now = time.perf_counter() - t0
            if first_token is None:
                first_token = now
            text += delta
            visible = THINK_BLOCK.sub("", text)
            if first_sentence is None and SENTENCE_END.search(visible):
                first_sentence = now
            if first_chunk is None and first_chunk_end(visible):
                first_chunk = now
    total = time.perf_counter() - t0
    return {
        "text": THINK_BLOCK.sub("", text).strip(),
        "ttft_s": first_token,
        "first_sentence_s": first_sentence if first_sentence is not None else total,
        "first_chunk_s": first_chunk if first_chunk is not None else total,
        "total_s": total,
        "prompt_tokens": timings.get("prompt_n"),
        "prompt_ms": timings.get("prompt_ms"),
        "gen_tokens": timings.get("predicted_n"),
        "gen_tok_s": timings.get("predicted_per_second"),
    }


def run_model(name: str, args) -> dict:
    model_file = MODELS[name]
    system_prompt = SYSTEM_PROMPTS[args.system]
    vram_before = gpu_memory_used_mb()
    start_server(model_file, args.ctx, args.extra)
    try:
        load_s = wait_ready()
        vram_loaded = gpu_memory_used_mb()
        system = {"role": "system", "content": system_prompt}
        chat([system, {"role": "user", "content": "Oi."}], 16, args.seed)  # aquecimento

        results = []
        with VRAMPeak() as vram:
            for label, turns in PROMPTS:
                messages = [system]
                for i, turn in enumerate(turns):
                    messages.append({"role": "user", "content": turn})
                    r = chat(messages, args.max_tokens, args.seed)
                    messages.append({"role": "assistant", "content": r["text"]})
                r.update(label=label, question=turns[-1], context=turns[:-1])
                results.append(r)
                print(f"   {label:20s} ttft {r['ttft_s']:.2f}s · 1º trecho {r['first_chunk_s']:.2f}s · "
                      f"1ª frase {r['first_sentence_s']:.2f}s · "
                      f"{r['gen_tok_s'] or 0:.1f} tok/s", flush=True)
        return {
            "model": name,
            "file": model_file,
            "load_s": load_s,
            "vram_before_mb": vram_before,
            "vram_loaded_mb": vram_loaded,
            "vram_peak_mb": vram.peak,
            "ttft_s": statistics.median(r["ttft_s"] for r in results),
            "first_sentence_s": statistics.median(r["first_sentence_s"] for r in results),
            "first_chunk_s": statistics.median(r["first_chunk_s"] for r in results),
            "first_chunk_p90_s": sorted(r["first_chunk_s"] for r in results)[round(0.9 * (len(results) - 1))],
            "gen_tok_s": statistics.median(r["gen_tok_s"] for r in results if r["gen_tok_s"]),
            "results": results,
        }
    except Exception as e:
        print(f"   FALHOU: {e}", flush=True)
        return {"model": name, "file": model_file, "error": str(e), "logs": container_logs()}
    finally:
        stop_server()


# --- Relatórios ------------------------------------------------------------------------

def write_markdown(path: Path, runs: list[dict], args) -> None:
    ok = [r for r in runs if "error" not in r]
    lines = [
        f"# Benchmark de LLM: {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Imagem `{IMAGE}`, prompt {args.system}, contexto {args.ctx}, máx. {args.max_tokens} tokens, "
        f"temperatura 0,6, seed {args.seed}.",
        "VRAM = uso total da GPU (inclui o STT, se estiver rodando).",
        "",
        "| modelo | TTFT p50 | 1º trecho p50 | 1º trecho p90 | 1ª frase p50 | tok/s p50 | VRAM antes | VRAM pico | carga |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in runs:
        if "error" in r:
            lines.append(f"| {r['model']} | falhou | | | | | | | |")
            continue
        lines.append(
            f"| {r['model']} | {r['ttft_s']:.2f} s | {r['first_chunk_s']:.2f} s | {r['first_chunk_p90_s']:.2f} s "
            f"| {r['first_sentence_s']:.2f} s | {r['gen_tok_s']:.1f} "
            f"| {r['vram_before_mb']} MB | {r['vram_peak_mb']} MB | {r['load_s']:.0f} s |"
        )
    lines += ["", f"## Prompt de sistema ({args.system})", "", "```", SYSTEM_PROMPTS[args.system], "```", "",
              "## Respostas", ""]
    for i, (label, turns) in enumerate(PROMPTS):
        lines.append(f"### {i + 1}. {label}")
        lines.append("")
        for t in turns[:-1]:
            lines.append(f"> (contexto) {t}")
        lines.append(f"> **{turns[-1]}**")
        lines.append("")
        for r in ok:
            a = r["results"][i]
            lines.append(f"- **{r['model']}** (1º trecho {a['first_chunk_s']:.2f} s): {a['text']}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--ctx", type=int, default=4096)
    parser.add_argument("--max-tokens", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--system", choices=sorted(SYSTEM_PROMPTS), default="v1", help="versão do prompt de sistema")
    parser.add_argument("--extra", nargs=argparse.REMAINDER, default=[],
                        help="argumentos extras para o llama-server (sempre por último)")
    args = parser.parse_args()

    runs = []
    for name in args.models.split(","):
        print(f"→ {name}", flush=True)
        runs.append(run_model(name.strip(), args))

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out_json = RESULTS_DIR / f"llm-{args.system}-{stamp}.json"
    out_md = RESULTS_DIR / f"llm-{args.system}-{stamp}.md"
    out_json.write_text(json.dumps({
        "date": datetime.now().isoformat(timespec="seconds"),
        "image": IMAGE,
        "args": {k: v for k, v in vars(args).items()},
        "system_prompt": SYSTEM_PROMPTS[args.system],
        "runs": runs,
    }, ensure_ascii=False, indent=2))
    write_markdown(out_md, runs, args)
    print(f"\nresultados: {out_json}\nrespostas:  {out_md}")


if __name__ == "__main__":
    main()
