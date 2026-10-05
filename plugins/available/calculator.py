"""JARVIS bundled plugin — exact calculator (offline, no AI call).

Safe by construction: the expression is parsed with `ast` and only numbers,
arithmetic operators and a fixed list of math functions are evaluated —
no names, attributes, calls to anything else, or imports can run.
"""
import ast
import math
import operator

PLUGIN = {
    "name": "calculator",
    "version": "1.0.0",
    "description": (
        "Evaluate a maths expression exactly and offline: arithmetic, powers, percentages, "
        "sqrt, sin/cos/tan (degrees), log, ln, factorial, round. Use for any calculation "
        "the user asks for instead of estimating."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "expression": {"type": "STRING", "description": "e.g. '(25*42)+sqrt(144)' or '18% of 2500'"},
        },
        "required": ["expression"],
    },
    "risk": "safe",
    "permissions": [],
    "capabilities": ["math", "calculate", "arithmetic", "calculator", "compute", "percentage"],
}

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "log": math.log10, "ln": math.log, "exp": math.exp,
    "sin": lambda d: math.sin(math.radians(d)), "cos": lambda d: math.cos(math.radians(d)),
    "tan": lambda d: math.tan(math.radians(d)), "factorial": lambda n: math.factorial(int(n)),
    "floor": math.floor, "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def _ev(n):
    if isinstance(n, ast.Expression):
        return _ev(n.body)
    if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
        return n.value
    if isinstance(n, ast.Name) and n.id in _CONSTS:
        return _CONSTS[n.id]
    if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
        v = _ev(n.operand)
        return -v if isinstance(n.op, ast.USub) else v
    if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
        a, b = _ev(n.left), _ev(n.right)
        if isinstance(n.op, ast.Pow) and (abs(b) > 1000 or abs(a) > 1e12):
            raise ValueError("number too large")
        return _OPS[type(n.op)](a, b)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
        args = [_ev(a) for a in n.args]
        if n.func.id == "factorial" and args and args[0] > 500:
            raise ValueError("number too large")
        return _FUNCS[n.func.id](*args)
    raise ValueError("unsupported expression")


def calculate(expr: str):
    import re
    e = str(expr or "").lower().replace("×", "*").replace("÷", "/").replace("^", "**").replace(",", "")
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", e)
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)
    if len(e) > 200:
        raise ValueError("expression too long")
    v = _ev(ast.parse(e, mode="eval"))
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return int(v)
        return float(f"{v:.10g}")
    return v


def run(parameters, player=None, session_memory=None):
    expr = parameters.get("expression", "")
    try:
        return f"{expr} = {calculate(expr)}"
    except ZeroDivisionError:
        return "That divides by zero, so it has no answer."
    except Exception as e:
        return f"I couldn't calculate that ({e})."


def verify(parameters, result_text):
    return ("=" in result_text, "result computed" if "=" in result_text else "no result")
