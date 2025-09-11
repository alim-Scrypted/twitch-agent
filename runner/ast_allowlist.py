import ast

ALLOWED_CALL_BASES = {"agent"}
ALLOWED_NODE_TYPES = {
    ast.Module, ast.Expr, ast.Call, ast.Attribute, ast.Name, ast.Load,
    ast.Str, ast.Bytes, ast.Num, ast.Constant, ast.Tuple, ast.List, ast.Dict,
    ast.keyword, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv,
    ast.Mod, ast.Pow, ast.UnaryOp, ast.USub, ast.UAdd, ast.Compare,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.BoolOp, ast.And, ast.Or,
    ast.JoinedStr, ast.FormattedValue, ast.Assign
}

def _is_allowed_call(node: ast.Call) -> bool:
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return node.func.value.id in ALLOWED_CALL_BASES
    return False

def validate_snippet(src: str) -> None:
    try:
        tree = ast.parse(src, mode="exec")
    except SyntaxError as e:
        raise ValueError(f"SyntaxError: {e}")
    
    for n in ast.walk(tree):
        if type(n) not in ALLOWED_NODE_TYPES:
            raise ValueError(f"Node not allowed: {type(n).__name__}")
        if isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                raise ValueError(f"Dunder attribute not allowed")
        if isinstance(n, ast.Name):
            if n.id not in {"agent"}:
                raise ValueError(f"Unknown name: {n.id}")
        if isinstance(n, ast.Call):
            if not _is_allowed_call(n):
                raise ValueError(f"Only agent.<method>(...) calls are allowed")

    lowered = src.replace(" ", "").lower()
    banned = ("import", "exec(", "eval(", "__", "subprocess", "os.", "sys.", "open(")
    if any(b in lowered for b in banned):
        raise ValueError(f"Contains banned token")
        
