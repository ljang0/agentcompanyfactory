"""Read-only Stage 1 tools over a frozen packet; no shell, network or arbitrary paths.

Small newline-delimited MCP stdio server. Its only input file is supplied by the
coordinator, never by the model. Tool results are data, not instructions.
"""

import ast
import json
import math
import operator
import re
import sys
from pathlib import Path

TOOL_NAMES = ["read_source", "read_app_schema", "search_catalog", "calculate", "read_seed", "read_artifact"]
DEFAULT_PAGE_CHARS = 12000
MAX_PAGE_CHARS = 16000


def page(text, arguments):
    start, length = arguments.get("offset", 0), arguments.get("length", DEFAULT_PAGE_CHARS)
    if type(start) is not int or type(length) is not int or start < 0 or not 1 <= length <= MAX_PAGE_CHARS:
        raise ValueError(f"offset must be nonnegative and length must be 1..{MAX_PAGE_CHARS}")
    end = min(start + length, len(text))
    return {
        "text": text[start:end],
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }


def calculate(expression):
    if len(expression) > 1000:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 100:
        raise ValueError("expression too complex")
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
    }

    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            value = binary[type(node.op)](visit(node.left), visit(node.right))
        else:
            raise ValueError("only numeric literals, parentheses and + - * / // % are allowed")
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError("numeric range exceeded")
        return value

    return {"value": visit(tree.body), "arithmetic": "bounded Python numeric arithmetic"}


def pointer_value(document, pointer):
    """Resolve a JSON Pointer, without treating it as a filesystem path."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError("Record link requires a JSON Pointer")
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            document = document[int(part)] if isinstance(document, list) else document[part]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ValueError("Record link does not resolve") from exc
    return document


def invoke(context, name, arguments):
    if name == "read_artifact":
        artifacts = context.get("artifacts", {})
        artifact = arguments["name"]
        if artifact not in artifacts:
            raise ValueError("Artifact is not in the frozen packet")
        pointer = arguments["pointer"]
        if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
            raise ValueError("Artifact pointer must be an empty string or start with /")
        value = artifacts[artifact]
        for encoded in pointer.split("/")[1:]:
            if re.search(r"~(?![01])", encoded):
                raise ValueError("Invalid JSON Pointer escape")
            part = encoded.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part) or int(part) >= len(value):
                    raise ValueError("Invalid JSON Pointer array index")
                value = value[int(part)]
            elif isinstance(value, dict):
                value = value[part]
            else:
                raise TypeError("Artifact pointer traverses a scalar")
        return {
            "name": artifact,
            "pointer": arguments["pointer"],
            **page(json.dumps(value, sort_keys=True), arguments),
        }
    if name == "read_seed":
        if "seed" not in context:
            raise ValueError("No generated seed is available in this frozen packet")
        value = pointer_value(context["seed"], arguments["pointer"])
        return {"pointer": arguments["pointer"], **page(json.dumps(value, sort_keys=True), arguments)}
    if name == "calculate":
        return calculate(arguments["expression"])
    if name == "read_source":
        source = next((s for s in context["sources"] if s["url"] == arguments["url"]), None)
        if source is None:
            raise ValueError("URL is not in the frozen source packet")
        return {
            "url": source["url"],
            **page(source["excerpt"], arguments),
        }
    if name == "read_app_schema":
        document = context.get("app_schemas", {}).get(arguments["app_id"])
        if document is None:
            raise ValueError(
                "App has no schema document in this frozen packet; treat capability as unresolved"
            )
        return {
            **{key: value for key, value in document.items() if key != "text"},
            **page(document["text"], arguments),
        }
    if name == "search_catalog":
        query = arguments["query"].casefold().split()
        if not query or len(query) > 20:
            raise ValueError("provide 1..20 search terms")
        rows = context["catalog"] + [
            {"soc": soc, "title": title} for soc, title in context.get("occupations", {}).items()
        ]
        ranked = [(sum(word in json.dumps(row).casefold() for word in query), row) for row in rows]
        return [row for score, row in sorted(ranked, key=lambda pair: -pair[0])[:12] if score]
    raise ValueError("unknown tool")


def definitions(context=None):
    pagination = {
        "offset": {"type": "integer", "minimum": 0, "default": 0},
        "length": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_PAGE_CHARS,
            "default": DEFAULT_PAGE_CHARS,
            "description": f"Characters per page, at most {MAX_PAGE_CHARS}; use next_offset to continue.",
        },
    }
    entries = [
        (
            "read_source",
            "Read any portion of a supplied captured source. Continue with next_offset; no new URLs.",
            {"url": {"type": "string"}, **pagination},
            ["url"],
        ),
        (
            "read_app_schema",
            "Read a pinned app schema by catalog app_id. Continue with next_offset; documentation is not runtime proof.",
            {"app_id": {"type": "string"}, **pagination},
            ["app_id"],
        ),
        (
            "search_catalog",
            "Find application capabilities or occupation codes in the frozen catalog.",
            {"query": {"type": "string"}},
            ["query"],
        ),
        (
            "calculate",
            "Check arithmetic using numeric literals and + - * / // %; no code execution.",
            {"expression": {"type": "string"}},
            ["expression"],
        ),
    ]
    if context is not None and "seed" in context:
        entries.append(
            (
                "read_seed",
                "Read the actual generated seed by JSON Pointer; empty pointer is the whole seed. Paginate with next_offset.",
                {"pointer": {"type": "string"}, **pagination},
                ["pointer"],
            )
        )
    if context is not None and context.get("artifacts"):
        entries.append(
            (
                "read_artifact",
                "Read an exact frozen artifact by name and JSON Pointer; empty pointer is the whole artifact. "
                "Paginate with next_offset. Available names: " + ", ".join(sorted(context["artifacts"])),
                {
                    "name": {"type": "string", "enum": sorted(context["artifacts"])},
                    "pointer": {"type": "string"},
                    **pagination,
                },
                ["name", "pointer"],
            )
        )
    return [
        {
            "name": name,
            "description": description,
            "inputSchema": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        }
        for name, description, properties, required in entries
    ]


def serve(context, incoming, outgoing):
    for line in incoming:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {
                "protocolVersion": request["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stage1", "version": "1.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": definitions(context)}
        elif method == "tools/call":
            try:
                params = request["params"]
                value = invoke(context, params["name"], params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
            except (ValueError, KeyError, TypeError, SyntaxError, ArithmeticError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
        else:
            outgoing.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                + "\n"
            )
            outgoing.flush()
            continue
        outgoing.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\n")
        outgoing.flush()


if __name__ == "__main__":
    serve(json.loads(Path(sys.argv[1]).read_text()), sys.stdin, sys.stdout)
