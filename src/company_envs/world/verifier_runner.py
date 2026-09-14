"""Standalone stdlib-only runner, mounted read-only in the verifier namespace."""

import ast
import builtins
import json
import resource

IMPORTS = {"math", "re", "datetime", "decimal", "statistics", "collections", "json"}
BUILTINS = {
    "abs",
    "all",
    "any",
    "bool",
    "dict",
    "enumerate",
    "float",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "range",
    "reversed",
    "round",
    "set",
    "sorted",
    "str",
    "sum",
    "tuple",
    "zip",
    "ValueError",
    "KeyError",
    "TypeError",
    "Exception",
}


def validate(code):
    tree = ast.parse(code)
    if len(code) > 60000 or len(list(ast.walk(tree))) > 15000:
        raise ValueError("Verifier exceeds the bounded program size")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "verify" for n in tree.body):
        raise ValueError("Verifier must define verify(initial, final, files, events)")
    for node in tree.body:
        if not isinstance(
            node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Expr)
        ):
            raise ValueError(  # noqa: TRY004 -- rejected program syntax, not an API argument type
                "Only imports, functions, constants and a docstring are permitted at module scope"
            )
        if isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
            raise ValueError("Module-level execution is not permitted")  # noqa: TRY004
    forbidden = {
        "open",
        "eval",
        "exec",
        "compile",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "input",
        "type",
        "object",
        "super",
        "help",
        "breakpoint",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef, ast.Global, ast.Nonlocal)):
            raise ValueError("Unsupported verifier construct")  # noqa: TRY004
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in forbidden):
            raise ValueError(f"Forbidden verifier name: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError("Private attribute access is forbidden")
        if isinstance(node, ast.FunctionDef) and (node.decorator_list or node.name.startswith("_")):
            raise ValueError("Decorators and private function names are forbidden")
        if isinstance(node, ast.Import) and any(n.name not in IMPORTS for n in node.names):
            raise ValueError("Unsupported verifier import")
        if isinstance(node, ast.ImportFrom) and (
            node.level
            or node.module not in IMPORTS
            or any(n.name.startswith("_") or n.name == "*" for n in node.names)
        ):
            raise ValueError("Unsupported verifier import")
    return tree


def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name not in IMPORTS or level:
        raise ValueError("Unsupported verifier import")
    return builtins.__import__(name, globals, locals, fromlist, level)


def main():
    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    with open("/input.json") as stream:
        data = json.load(stream)
    with open("/verifier.py") as stream:
        code = stream.read()
    scope = {
        "__builtins__": {**{name: getattr(builtins, name) for name in BUILTINS}, "__import__": safe_import}
    }
    exec(compile(validate(code), "/verifier.py", "exec"), scope)  # noqa: S102 -- only inside the isolated runner
    result = scope["verify"](data["initial"], data["final"], data.get("files", {}), data.get("events", []))
    encoded = json.dumps(result, allow_nan=False)
    if len(encoded) > 100000:
        raise ValueError("Verifier result exceeds the output limit")
    print(encoded)


if __name__ == "__main__":
    main()
