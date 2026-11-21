"""ECHO text-level SQL repair.

Generates CANDIDATE repairs for a broken statement and lets the caller decide which candidate
is acceptable. Nothing in here applies a repair on its own: a candidate only counts when the
caller's ``is_valid`` check (a real parse in the fixer) accepts it, and the fixer then plans
it on the database. Every edit works on the *code* of the statement only: string literals,
quoted identifiers and comments are never touched.

Two families:

* syntax repair   - typography (smart quotes), unterminated quotes, missing / extra
                    parentheses, stray commas, repeated keywords, ``==``, mistyped keywords.
* reference repair - an unknown table / column / function name is replaced by the database's own
                    "Did you mean ..." / "Candidate bindings" suggestion.

Pure Python, standard library only.
"""

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Tuple

KEYWORDS = (
    "SELECT", "FROM", "WHERE", "GROUP", "BY", "ORDER", "HAVING", "JOIN", "LEFT", "RIGHT", "INNER",
    "OUTER", "FULL", "CROSS", "ON", "USING", "AND", "OR", "NOT", "AS", "LIMIT", "OFFSET", "UNION",
    "ALL", "DISTINCT", "CASE", "WHEN", "THEN", "ELSE", "END", "IN", "IS", "NULL", "LIKE", "BETWEEN",
    "EXISTS", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE", "CREATE", "TABLE", "VIEW",
    "DROP", "ALTER", "WITH", "ASC", "DESC", "INTERSECT", "EXCEPT", "QUALIFY", "OVER", "PARTITION",
)
_KEYWORD_SET = frozenset(KEYWORDS)
_CLAUSE_WORDS = ("GROUP", "ORDER", "HAVING", "LIMIT", "UNION", "INTERSECT", "EXCEPT", "WHERE", "QUALIFY")
# Places where a forgotten ")" most often belongs: right before the next clause / join / alias.
_CLOSE_BEFORE = _CLAUSE_WORDS + ("FROM", "JOIN", "LEFT", "RIGHT", "INNER", "FULL", "CROSS", "AS", "ON")
_PLACEHOLDER = "\x01"

MAX_EDITS = 2
MAX_FRONTIER = 60
MAX_FOUND = 8

_TYPOGRAPHY = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": "'", "\u201d": "'", "\u201e": "'",
    "\u00a0": " ", "\u2009": " ", "\u200b": "",
    "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\uff0c": ",", "\uff08": "(", "\uff09": ")", "\uff1b": ";",
}


@dataclass
class Repair:
    sql: str
    labels: List[str] = field(default_factory=list)
    edits: int = 1
    ratio: float = 0.0  # similarity to the original text (higher = smaller change)


# --------------------------------------------------------------------------- #
# Scanning: separate code from literals / comments
# --------------------------------------------------------------------------- #
def mask_code(text: str) -> Tuple[str, Optional[str]]:
    """``(masked, open_quote)``: same length as ``text`` with every character of a string literal,
    quoted identifier or comment replaced by a placeholder. ``open_quote`` is the quote character
    of an unterminated literal (else None)."""
    out = list(text)
    i, n = 0, len(text)
    open_quote = None
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = text.find("\n", i)
            end = n if end == -1 else end
            for k in range(i, end):
                out[k] = _PLACEHOLDER
            i = end
        elif ch == "/" and nxt == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for k in range(i, end):
                out[k] = _PLACEHOLDER
            i = end
        elif ch in ("'", '"', "`"):
            j = i + 1
            closed = False
            while j < n:
                if text[j] == ch:
                    if j + 1 < n and text[j + 1] == ch:
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            end = j + 1 if closed else n
            for k in range(i, min(end, n)):
                out[k] = _PLACEHOLDER
            if not closed:
                open_quote = ch
            i = end
        else:
            i += 1
    return "".join(out), open_quote


def _splice(text: str, start: int, end: int, replacement: str) -> str:
    return text[:start] + replacement + text[end:]


# --------------------------------------------------------------------------- #
# Edit distance (optimal string alignment: counts a swap of neighbours as one edit)
# --------------------------------------------------------------------------- #
def edit_distance(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    rows, cols = len(a) + 1, len(b) + 1
    d = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        d[i][0] = i
    for j in range(cols):
        d[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


# --------------------------------------------------------------------------- #
# Single-edit generators: each returns [(new_text, label), ...]
# --------------------------------------------------------------------------- #
def _typography(text: str) -> List[Tuple[str, str]]:
    new = "".join(_TYPOGRAPHY.get(ch, ch) for ch in text)
    return [(new, "replaced typographic characters (smart quotes / odd spaces) by plain SQL ones")] if new != text else []


def _close_quote(text: str) -> List[Tuple[str, str]]:
    _, open_quote = mask_code(text)
    if open_quote is None:
        return []
    return [(text + open_quote, f"closed the unterminated {open_quote} quote")]


def _parens(text: str) -> List[Tuple[str, str]]:
    masked, _ = mask_code(text)
    stack, extra = [], []
    for i, ch in enumerate(masked):
        if ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                stack.pop()
            else:
                extra.append(i)
    out: List[Tuple[str, str]] = []
    if stack:
        count = len(stack)
        stripped = text.rstrip()
        out.append((stripped + ")" * count, f"added {count} missing closing parenthesis at the end"))
        first_open = stack[0]
        for match in re.finditer(r"\b(" + "|".join(_CLOSE_BEFORE) + r")\b", masked, re.IGNORECASE):
            if match.start() > first_open:
                position = len(text[:match.start()].rstrip())  # hug the previous token: "SUM(x)" not "SUM(x )"
                out.append((_splice(text, position, position, ")" * count),
                            f"added {count} missing closing parenthesis before {match.group(1).upper()}"))
    for position in reversed(extra[-3:]):
        out.append((_splice(text, position, position + 1, ""), "removed an unmatched closing parenthesis"))
    return out


_STOP_AFTER_COMMA = r"(?=\b(?:FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|UNION|INTERSECT|EXCEPT|QUALIFY|ON|USING|SET|VALUES)\b|\)|;|$)"


def _commas(text: str) -> List[Tuple[str, str]]:
    masked, _ = mask_code(text)
    spans = []
    for pattern in (r",(\s*)" + _STOP_AFTER_COMMA, r",(?=\s*,)", r"(?<=\()\s*,"):
        for match in re.finditer(pattern, masked, re.IGNORECASE):
            if masked[match.start():match.end()].strip():
                spans.append((match.start(), match.start() + masked[match.start():].index(",") + 1))
    spans = sorted(set(spans))
    if not spans:
        return []
    out = []
    for start, end in spans[:5]:
        out.append((_splice(text, start, end, ""), "removed a stray comma"))
    if len(spans) > 1:
        combined = text
        for start, end in reversed(spans):
            combined = _splice(combined, start, end, "")
        out.append((combined, "removed stray commas"))
    return out


_REPEATABLE = "SELECT|FROM|WHERE|AND|OR|JOIN|ON|BY|AS|GROUP|ORDER|HAVING|LIMIT|LEFT|RIGHT|INNER|OUTER|UNION"


def _repeated_keywords(text: str) -> List[Tuple[str, str]]:
    masked, _ = mask_code(text)
    out = []
    for match in re.finditer(r"\b(" + _REPEATABLE + r")\s+\1\b", masked, re.IGNORECASE):
        out.append((_splice(text, match.start(), match.end(), text[match.start():match.start() + len(match.group(1))]),
                    f"removed the repeated keyword {match.group(1).upper()}"))
    return out[:4]


def _comparison_typos(text: str) -> List[Tuple[str, str]]:
    masked, _ = mask_code(text)
    out = []
    for pattern, replacement, label in (
        (r"==", "=", "replaced == by ="),
        (r"=<", "<=", "replaced =< by <="),
        (r"=>(?!\s*[(\w])", ">=", "replaced => by >="),
    ):
        match = re.search(pattern, masked)
        if match:
            out.append((_splice(text, match.start(), match.end(), replacement), label))
    return out


def _case_like(original: str, keyword: str) -> str:
    if original.isupper():
        return keyword
    if original.islower():
        return keyword.lower()
    return keyword


def _keyword_typos(text: str) -> List[Tuple[str, str]]:
    masked, _ = mask_code(text)
    found = []
    for match in re.finditer(r"[A-Za-z_]+", masked):
        word = match.group(0)
        upper = word.upper()
        before = masked[match.start() - 1] if match.start() else ""
        after = masked[match.end():].lstrip()[:1]
        if upper in _KEYWORD_SET or len(word) < 3 or before == "." or after in ("(", "."):
            continue
        best = None
        for keyword in KEYWORDS:
            if abs(len(keyword) - len(word)) > 2:
                continue
            limit = 1 if len(keyword) <= 4 else 2
            distance = edit_distance(word, keyword)
            if 0 < distance <= limit and (best is None or distance < best[0]):
                best = (distance, keyword)
        if best:
            found.append((match.start(), match.end(), word, best[1]))
    out = [
        (_splice(text, s, e, _case_like(word, kw)), f"corrected the keyword '{word}' to {kw}")
        for s, e, word, kw in found[:6]
    ]
    if len(found) > 1:
        combined = text
        for s, e, word, kw in reversed(found):
            combined = _splice(combined, s, e, _case_like(word, kw))
        out.append((combined, "corrected mistyped keywords"))
    return out


_GENERATORS: Tuple[Callable[[str], List[Tuple[str, str]]], ...] = (
    _typography, _close_quote, _parens, _commas, _repeated_keywords, _comparison_typos, _keyword_typos,
)


def single_edits(text: str) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for generator in _GENERATORS:
        try:
            out.extend(generator(text))
        except Exception:  # noqa: BLE001 - one broken generator must not stop the others
            continue
    return out


# --------------------------------------------------------------------------- #
# Syntax repair search
# --------------------------------------------------------------------------- #
def repair_candidates(text: str, is_valid: Callable[[str], bool], max_edits: int = MAX_EDITS) -> List[Repair]:
    """Every valid statement reachable with the FEWEST edits (best first). ``[]`` = nothing found.

    ``is_valid`` is supplied by the caller (a real parse); a candidate that does not pass it is
    never returned. If ``text`` is already valid there is nothing to repair.
    """
    if not isinstance(text, str) or not text.strip() or is_valid(text):
        return []
    seen = {text}
    frontier: List[Tuple[str, List[str]]] = [(text, [])]
    for depth in range(1, max_edits + 1):
        found: List[Repair] = []
        next_frontier: List[Tuple[str, List[str]]] = []
        for base, labels in frontier:
            for candidate, label in single_edits(base):
                if candidate in seen or not candidate.strip():
                    continue
                seen.add(candidate)
                if is_valid(candidate):
                    ratio = difflib.SequenceMatcher(None, text, candidate).ratio()
                    found.append(Repair(candidate, labels + [label], depth, ratio))
                elif len(next_frontier) < MAX_FRONTIER:
                    next_frontier.append((candidate, labels + [label]))
        if found:
            found.sort(key=lambda r: -r.ratio)
            return found[:MAX_FOUND]
        frontier = next_frontier
    return []


# --------------------------------------------------------------------------- #
# Reference repair (names the database does not know)
# --------------------------------------------------------------------------- #
_BAD_NAME_PATTERNS = (
    re.compile(r"Table with name\s+\"?([^\s\"!]+)\"?\s+does not exist", re.IGNORECASE),
    re.compile(r"(?:Scalar |Aggregate |Table |Macro |Pragma )?Function with name\s+\"?([^\s\"!]+)\"?\s+does not exist", re.IGNORECASE),
    re.compile(r"Referenced column\s+\"([^\"]+)\"\s+not found", re.IGNORECASE),
    re.compile(r"does not have a column named\s+\"([^\"]+)\"", re.IGNORECASE),
    re.compile(r"Referenced table\s+\"([^\"]+)\"\s+not found", re.IGNORECASE),
    re.compile(r"Column\s+\"([^\"]+)\"\s+not found", re.IGNORECASE),
)


def _quoted(text: str) -> List[str]:
    return re.findall(r"\"([^\"]+)\"", text)


def parse_reference_error(message: str) -> Optional[Tuple[str, List[str]]]:
    """``(unknown_name, [suggestion, ...])`` from a database error message, else None."""
    if not isinstance(message, str) or not message:
        return None
    bad = None
    for pattern in _BAD_NAME_PATTERNS:
        match = pattern.search(message)
        if match:
            bad = match.group(1).strip().strip(".")
            break
    if not bad:
        return None
    suggestions: List[str] = []
    did_you_mean = re.search(r"Did you mean\s+(.*)", message, re.IGNORECASE | re.DOTALL)
    if did_you_mean:
        suggestions.extend(_quoted(did_you_mean.group(1).split("\n")[0]))
    bindings = re.search(r"Candidate bindings:\s*(.*)", message, re.IGNORECASE | re.DOTALL)
    if bindings:
        suggestions.extend(_quoted(bindings.group(1).split("\n")[0]))
    cleaned: List[str] = []
    for suggestion in suggestions:
        name = suggestion.split(".")[-1].strip()
        if name and name.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(name)
    bad_name = bad.split(".")[-1]
    return bad_name, cleaned


def acceptable_suggestion(bad: str, good: str) -> bool:
    """A suggestion is used only when it really is a near miss of the unknown name."""
    if not bad or not good or bad.lower() == good.lower():
        return False
    return edit_distance(bad, good) <= max(2, len(bad) // 2)


def replace_identifier(text: str, bad: str, good: str) -> str:
    """Replace the whole word ``bad`` by ``good`` in the CODE of ``text`` (literals / comments untouched)."""
    masked, _ = mask_code(text)
    pattern = re.compile(r"(?<![A-Za-z0-9_$])" + re.escape(bad) + r"(?![A-Za-z0-9_$])", re.IGNORECASE)
    pieces, cursor = [], 0
    for match in pattern.finditer(masked):
        pieces.append(text[cursor:match.start()])
        pieces.append(good)
        cursor = match.end()
    pieces.append(text[cursor:])
    return "".join(pieces)