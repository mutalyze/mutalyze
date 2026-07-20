"""Blank out comments and string literals before matching content patterns.

Naive content patterns (``:\\s*any\\b``) fire inside comments that mention the
thing, inside string literals, and throughout generated files. This is the
largest check category and the largest false-alarm source, so we strip first.

We replace comment/string *interiors* with spaces while preserving newlines and
overall length, so the caller can still cite the original line by index.
"""

from __future__ import annotations

# Line-comment markers and whether the language has C-style block comments and
# backtick/triple-quote strings, keyed by file extension.
_C_FAMILY = {"ts", "tsx", "js", "jsx", "mjs", "cjs", "rs", "go", "java", "c",
             "cc", "cpp", "h", "hpp", "cs", "swift", "kt", "scala", "css",
             "scss", "less", "php", "dart"}
_HASH_FAMILY = {"py", "rb", "sh", "bash", "zsh", "yaml", "yml", "toml", "pl",
                "r", "jl", "nim", "ex", "exs"}


def _blank(s: str) -> str:
    """Same length, newlines preserved, everything else -> space."""
    return "".join("\n" if ch == "\n" else " " for ch in s)


def _blank_heredocs(text: str) -> str:
    """Blank the BODY of shell heredocs (`cmd <<'EOF' … EOF`). The body is data
    or another language, not shell being executed — a `curl … | sh` written into
    a doc via a heredoc isn't a command being run. Keeps the opening line."""
    import re as _re

    out = []
    delim = None
    for line in text.split("\n"):
        if delim is None:
            out.append(line)
            m = _re.search(r"<<-?\s*[\"']?([A-Za-z_]\w*)[\"']?", line)
            if m:
                delim = m.group(1)
        else:
            if line.strip() == delim:
                delim = None
            out.append("")  # blank body and closing-delimiter line
    return "\n".join(out)


def strip_code(text: str, ext: str) -> str:
    ext = (ext or "").lower()
    c_family = ext in _C_FAMILY or ext == ""
    hash_family = ext in _HASH_FAMILY or ext == ""
    triple = ext in ("py",) or ext == ""

    if hash_family and "<<" in text:
        text = _blank_heredocs(text)

    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        two = text[i : i + 2]
        three = text[i : i + 3]

        # block comment /* ... */
        if c_family and two == "/*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(_blank(text[i:j]))
            i = j
            continue
        # line comment // ...
        if c_family and two == "//":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(_blank(text[i:j]))
            i = j
            continue
        # line comment # ...
        if hash_family and ch == "#":
            j = text.find("\n", i)
            j = n if j == -1 else j
            out.append(_blank(text[i:j]))
            i = j
            continue
        # triple-quoted strings (python)
        if triple and three in ('"""', "'''"):
            j = text.find(three, i + 3)
            j = n if j == -1 else j + 3
            out.append('"' + _blank(text[i + 1 : j - 1]) + '"' if j - i >= 2 else _blank(text[i:j]))
            i = j
            continue
        # single/double/backtick string literal
        if ch in ('"', "'") or (c_family and ch == "`"):
            quote = ch
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                if text[j] == "\n" and quote != "`":
                    break  # unterminated single-line string
                j += 1
            # keep the quote chars, blank the interior
            interior = text[i + 1 : max(i + 1, j - 1)]
            out.append(quote + _blank(interior) + (quote if j <= n and j - 1 >= i + 1 else ""))
            i = j
            continue

        out.append(ch)
        i += 1

    result = "".join(out)
    # Length can drift slightly around edge cases; realign line count so the
    # caller's index-based citation stays correct.
    orig_lines = text.count("\n")
    res_lines = result.count("\n")
    if res_lines < orig_lines:
        result += "\n" * (orig_lines - res_lines)
    return result
