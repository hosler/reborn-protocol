"""Conservative source routing; ambiguous scripts belong to the GS1 engine."""

import re

_LEXEMES = re.compile(
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*|/\*.*?(?:\*/|$)',
    re.DOTALL)
_GS1 = re.compile(
    r"(?:^|[;{}\n])\s*(?:setani|showcharacter|setcharprop|setstring|addstring"
    r"|toweapons|message|say2?|play|showimg|hideimg|putnpc|destroy|setplayerprop"
    r"|setlevel|lay|take|hitplayer|triggeraction|setimg|dontblock"
    r"|drawoverplayer|noplayerkilling)\b(?!\s*\()(?=\s|;|$)"
    r"|#[vseI]\s*\(|#c",
    re.IGNORECASE)
_GS2 = re.compile(
    r"\bfunction\s+(?:\w+\.)?on[A-Z]\w*\s*\("
    r"|\bnew\s+(?:TStaticVar|Gui\w+)\b|\bpublic\s+function\b"
    r"|\bwith\s*\(|\btemp\.\w+\s*=(?!=)|\bforeach\s*\("
    r"|\bthis\.\w+\s*\(")


def looks_like_gs2(source: str) -> bool:
    """Apply every GS1 veto before considering distinctive GS2 syntax.

    Strings and comments (including the shared clientside directive) provide
    no evidence. GS1 permits helper functions and member assignments, whereas
    a top-level conditional always selects GS1 regardless of its condition.
    Bare numeric timeout assignments select GS1 only outside function bodies.
    """
    source = source.replace("\xa7", "\n")
    code = _LEXEMES.sub(
        lambda match: '""' if match[0][0] in "\"'" else "\n", source)
    blocks = []
    for token in re.finditer(
            r"(?P<function>\bfunction\s+[\w.]+\s*\([^)]*\)\s*\{)"
            r"|[{}]|\bif\s*\("
            r"|(?P<timeout>(?i:(?<![\w.])timeout\s*=\s*[\d.]))", code):
        if token.lastgroup == "function" or token[0] == "{":
            blocks.append(token.lastgroup == "function")
        elif token[0] == "}":
            if blocks:
                blocks.pop()
        elif token.lastgroup == "timeout":
            if not any(blocks):
                return False
        elif not blocks:
            return False
    if _GS1.search(code):
        return False
    if _GS2.search(code):
        return True
    # Expression returns distinguish the GS2 helper-only QA fixtures. GS1
    # helper declarations alone remain ambiguous; every GS1 veto still wins.
    return bool(re.search(r"\bfunction\s+[\w.]+\s*\([^)]*\)\s*\{", code)
                and re.search(r"\breturn\s+[^;{}]+;", code))
