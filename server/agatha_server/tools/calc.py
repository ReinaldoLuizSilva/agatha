"""Calculadora segura: avalia só aritmética, pela árvore sintática (nada de eval)."""

import ast
import math
import operator

from .base import Example, Tool

_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"sqrt": math.sqrt, "raiz": math.sqrt, "abs": abs, "round": round}
MAX_EXPONENT = 1000  # evita contas gigantescas (2 ** 10**9)


def evaluate(expression) -> float | int:
    # o modelo às vezes manda um número em vez de texto, ou usa notação brasileira
    text = str(expression).replace("^", "**").replace("×", "*").replace("÷", "/").replace(",", ".")
    tree = ast.parse(text, mode="eval")

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
                raise ValueError("expoente grande demais")
            return _BINARY[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
                and not node.keywords):
            return _FUNCTIONS[node.func.id](*(ev(a) for a in node.args))
        raise ValueError(f"expressão não suportada: {ast.unparse(node)}")

    result = ev(tree)
    if isinstance(result, float) and result.is_integer() and abs(result) < 1e15:
        return int(result)
    return round(result, 6) if isinstance(result, float) else result


def _brazilian(value: float | int) -> str | int:
    """Decimal com vírgula ("62,5"): o modelo copia o resultado, e "62.5" seria falado como versão
    ("sessenta e dois ponto cinco"; teste de 12/10). Inteiro continua número."""
    text = repr(value)
    return text.replace(".", ",") if isinstance(value, float) and "e" not in text else value


def _calcular(args: dict) -> dict:
    try:
        return {"resultado": _brazilian(evaluate(args.get("expressao", "")))}
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError, TypeError) as e:
        return {"erro": f"não consegui calcular: {e}"}


calculator = Tool(
    name="calcular",
    description="Calcula uma expressão matemática. Use para qualquer conta, porcentagem ou conversão "
                "numérica. Exemplo de expressão: 240 * 0.15",
    handler=_calcular,
    rule="qualquer conta, porcentagem ou conversão de unidades: use calcular, mesmo que pareça fácil",
    properties={"expressao": {"type": "string", "description": "expressão em sintaxe Python"}},
    required=("expressao",),
    examples=(Example("Quanto é 18% de 350?", {"expressao": "350 * 0.18"}, {"resultado": 63}, "Dá 63."),),
)
