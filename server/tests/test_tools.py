import asyncio

import pytest

from agatha_server.tools import Tool, ToolRegistry
from agatha_server.tools.calc import calculator, evaluate


@pytest.mark.parametrize("expr, expected", [
    ("240 * 0.15", 36),
    ("1234 * 56", 69104),
    ("2^10", 1024),
    ("2 ** 10", 1024),
    ("(2 - 1.5) * 1024 / 20", 25.6),
    ("10 / 4", 2.5),
    ("1,5 * 2", 3),
    ("sqrt(144)", 12),
    ("-3 + 5", 2),
    (36.0, 36),  # o modelo às vezes manda um número
    ("7 % 3", 1),
])
def test_calcula(expr, expected):
    assert evaluate(expr) == expected


@pytest.mark.parametrize("expr", [
    "__import__('os').system('ls')",
    "open('/etc/passwd')",
    "x + 1",
    "(1).__class__",
    "2 ** 100000",
    "[1, 2]",
])
def test_recusa_o_que_nao_e_aritmetica(expr):
    with pytest.raises((ValueError, SyntaxError)):
        evaluate(expr)


def test_resultado_decimal_vem_com_virgula():
    # o modelo copia o resultado: "62.5" seria falado como versão ("ponto cinco")
    assert calculator.handler({"expressao": "250 * 0.25"}) == {"resultado": "62,5"}
    assert calculator.handler({"expressao": "250 * 0.18"}) == {"resultado": 45}


def test_ferramenta_devolve_erro_em_vez_de_quebrar():
    assert calculator.handler({"expressao": "1/0"})["erro"].startswith("não consegui calcular")
    assert calculator.handler({"expressao": "abc"})["erro"]


def test_registro():
    def boom(args):
        raise RuntimeError("falhou")

    async def slow_ok(args):
        return {"ok": args["x"]}

    reg = ToolRegistry([calculator, Tool("boom", "d", boom, "r"), Tool("assincrona", "d", slow_ok, "r")])
    run = asyncio.run
    assert run(reg.execute("calcular", '{"expressao": "2+2"}')) == {"resultado": 4}
    assert run(reg.execute("assincrona", '{"x": 1}')) == {"ok": 1}
    assert "desconhecida" in run(reg.execute("nao_existe", "{}"))["erro"]
    assert "inválidos" in run(reg.execute("calcular", "{quebrado"))["erro"]
    assert "falhou" in run(reg.execute("boom", "{}"))["erro"]
    assert [s["function"]["name"] for s in reg.schemas()] == ["calcular", "boom", "assincrona"]
