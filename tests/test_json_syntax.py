import json

from company_envs.world.json_syntax import escape_csv_quotes
from company_envs.world.state_seed import parse_json


def test_embedded_csv_quotes_are_repaired_without_model_or_changed_cells():
    csv = 'Account,Description\nA1,"Shirts, hats and bags"\nA2,Embroidery'
    correct = {"items": {"file": {"id": "file", "content": csv, "size": 100}}}
    raw = json.dumps(correct).replace(r"\"Shirts, hats and bags\"", '"Shirts, hats and bags"')
    assert parse_json(raw, "Drive") == correct


def test_unrelated_invalid_json_is_not_guessed():
    assert escape_csv_quotes('{"content":"Hello "there"."}') is None
    assert escape_csv_quotes('{"content":"Account,Name\\nA1,"Joe"') is None


def test_valid_quoted_csv_needs_no_repair():
    text = json.dumps({"content": 'A,B\nx,"y,z"'})
    assert escape_csv_quotes(text) is None


def test_multisheet_authoring_source_keeps_its_sheet_header():
    content = '## Sheet: Accounts\nAccount,Description\nA1,"Shirts, hats and bags"'
    value = {"content": content}
    raw = json.dumps(value).replace(r"\"Shirts, hats and bags\"", '"Shirts, hats and bags"')
    assert parse_json(raw, "Drive") == value
