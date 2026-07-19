"""CSV encoding used by GS1 string-list commands and message codes."""

from __future__ import annotations

from collections.abc import Iterable


def gs1_csv_join(values: Iterable[str], force_quoted: bool = False) -> str:
    """Encode fields using the engine's string-list CSV dialect."""
    fields = []
    for value in values:
        value = str(value)
        if force_quoted or any(char in value for char in '"\\,'):
            escaped = value.replace("\\", "\\\\").replace('"', '""')
            fields.append(f'"{escaped}"')
        else:
            fields.append(value)
    return ",".join(fields)


def gs1_csv_split(value: str, ignore_leading_whitespace: bool = False) -> list[str]:
    """Decode the engine's quote-aware string-list CSV dialect."""
    tokens: list[str] = []
    token: list[str] = []
    word_start = True
    word_quoted = False
    i = 0

    while i < len(value):
        char = value[i]

        if (ignore_leading_whitespace and word_start
                and char in " \t"):
            i += 1
            continue

        if word_start and char == '"':
            word_start = False
            word_quoted = True
            i += 1
            continue

        if word_quoted:
            next_char = value[i + 1] if i + 1 < len(value) else None
            if char == "\\" and next_char == "\\":
                token.append("\\")
                i += 2
                continue
            if char == '"' and next_char == '"':
                token.append('"')
                i += 2
                continue
            if char == '"':
                tokens.append("".join(token))
                token.clear()
                word_start = True
                word_quoted = False
                comma = value.find(",", i + 1)
                if comma < 0:
                    break
                i = comma + 1
                continue
            token.append(char)
        elif char == ",":
            tokens.append("".join(token))
            token.clear()
            word_start = True
        else:
            token.append(char)
            word_start = False
        i += 1

    if token:
        tokens.append("".join(token))
    return tokens
