"""Paged reads of coordinator-supplied artifacts; no model-chosen file access."""

import io
import json

import pytest

from company_envs.stage_tools import definitions, invoke, serve


def test_artifacts_only_advertised_when_supplied():
    assert "read_artifact" not in {d["name"] for d in definitions({})}
    tool = next(d for d in definitions({"artifacts": {"trial": {}}}) if d["name"] == "read_artifact")
    assert tool["inputSchema"]["properties"]["name"]["enum"] == ["trial"]


def test_paged_pointer_read_preserves_exact_content():
    value = {"a/b": {"~key": "x" * 17000}}
    context = {"artifacts": {"trial": value}}
    chunks, offset = [], 0
    while True:
        result = invoke(
            context, "read_artifact", {"name": "trial", "pointer": "/a~1b/~0key", "offset": offset}
        )
        chunks.append(result["text"])
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert json.loads("".join(chunks)) == value["a/b"]["~key"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "/etc/passwd", "pointer": ""},
        {"name": "trial", "pointer": "/missing"},
        {"name": "trial", "pointer": "", "length": 16001},
        {"name": "trial", "pointer": "", "offset": -1},
        {"name": "trial", "pointer": None},
        {"name": "trial", "pointer": 123},
        {"name": "trial", "pointer": "missing-slash"},
        {"name": "trial", "pointer": "/rows/-1"},
        {"name": "trial", "pointer": "/rows/01"},
        {"name": "trial", "pointer": "/rows/9"},
        {"name": "trial", "pointer": "/rows/~9"},
    ],
)
def test_unsupported_artifact_or_page_is_structured_error(arguments):
    incoming = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read_artifact", "arguments": arguments},
            }
        )
        + "\n"
    )
    outgoing = io.StringIO()
    serve({"artifacts": {"trial": {"rows": [0, 1]}}}, incoming, outgoing)
    assert json.loads(outgoing.getvalue())["result"]["isError"]
