"""Narrow, lossless syntax repair for CSV embedded in JSON content strings."""

import json
import re


def escape_csv_quotes(text):
    """Escape bare CSV quotation marks inside a content value, or decline.

    Only inserts backslashes inside a clearly tabular content string. No keys,
    records, values or closing delimiters are generated. The complete candidate
    must parse; ambiguous/truncated structures stay on the ordinary error path.
    """
    replacements = []
    for match in re.finditer(r'"content"\s*:\s*"', text):
        start = match.end()
        # A CSV header precedes the first escaped newline and contains no JSON
        # syntax. This excludes ordinary prose and nested serialized objects.
        newline = text.find(r"\n", start, start + 500)
        header = text[start:newline] if newline >= 0 else ""
        if re.fullmatch(r"## Sheet: [\w /.-]+", header):
            header_start = newline + 2
            newline = text.find(r"\n", header_start, header_start + 500)
            header = text[header_start:newline] if newline >= 0 else ""
        if not re.fullmatch(r"[\w /.-]+(?:,[\w /.-]+){1,29}", header):
            continue
        terminal = re.search(r'(?<!\\)"\s*(?=\}|,\s*"[\w]+"\s*:)', text[newline:])
        if terminal is None:
            continue
        end = newline + terminal.start()
        raw = text[start:end]
        fixed, slashes = [], 0
        for char in raw:
            if char == '"' and slashes % 2 == 0:
                fixed.append("\\")
            fixed.append(char)
            slashes = slashes + 1 if char == "\\" else 0
        replacement = "".join(fixed)
        if replacement != raw:
            replacements.append((start, end, replacement))
    if not replacements:
        return None
    candidate = text
    for start, end, replacement in reversed(replacements):
        candidate = candidate[:start] + replacement + candidate[end:]
    try:
        json.loads(candidate)
    except ValueError:
        return None
    return candidate
