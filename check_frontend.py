"""Best-effort static checks on the React sources.

No transpiler is available offline, so this CANNOT prove the JSX compiles.
What it does catch is the failure mode that actually bites when writing JSX
by hand: an unbalanced delimiter or an unclosed element. It also verifies
every relative import points at a file that exists, which catches renames.

Treat a pass here as "no obvious structural error", not "verified".
"""
import os
import re
import sys

WEB = "/mnt/user-data/outputs/web"
FILES = ["src/main.jsx", "src/App.jsx", "src/api.js", "src/categories.js",
         "src/components/FilterPanel.jsx", "src/components/ResultCard.jsx"]

# Void/self-closing HTML elements that legitimately never have a closing tag.
VOID = {"input", "br", "hr", "img", "meta", "link", "source", "area", "base", "col"}


def strip_noise(src):
    """Remove comments and string/template literals so their contents can't
    be mistaken for code delimiters."""
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j == -1 else j
        elif two == "/*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif src[i] in "\"'`":
            quote = src[i]
            # An apostrophe directly after a letter or digit is a
            # contraction/possessive in JSX prose ("company's", "it's"),
            # not a string delimiter. Treating it as one swallows the rest
            # of the file and produces bogus unbalanced-paren reports.
            if quote == "'" and i > 0 and (src[i - 1].isalnum()):
                out.append(src[i])
                i += 1
                continue
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def check_delimiters(name, src):
    pairs = {")": "(", "]": "[", "}": "{"}
    stack, line = [], 1
    for ch in src:
        if ch == "\n":
            line += 1
        elif ch in "([{":
            stack.append((ch, line))
        elif ch in ")]}":
            if not stack:
                return f"{name}: unmatched '{ch}' at line {line}"
            open_ch, open_line = stack.pop()
            if open_ch != pairs[ch]:
                return (f"{name}: '{open_ch}' opened line {open_line} closed by "
                        f"'{ch}' at line {line}")
    if stack:
        ch, ln = stack[-1]
        return f"{name}: '{ch}' opened at line {ln} is never closed"
    return None


# attrs allow "=>" because arrow functions in JSX props legitimately
# contain ">" (onClick={() => ...}); a plain [^<>] class truncates the tag there.
TAG_RE = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9.]*)\b((?:=>|[^<>])*?)(/?)\s*>", re.S)


def check_tags(name, raw):
    """Balance JSX element tags. Operates on the raw source (JSX lives
    outside strings) but skips comment lines."""
    src = re.sub(r"//[^\n]*", "", raw)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    stack = []
    for m in TAG_RE.finditer(src):
        closing, tag, attrs, self_close = m.group(1), m.group(2), m.group(3), m.group(4)
        # `a < b` style comparisons won't match the tag pattern; generics
        # aren't used here. Lowercase non-void tags and Capitalised
        # components both need closing.
        if self_close or tag.lower() in VOID:
            continue
        line = src[:m.start()].count("\n") + 1
        if closing:
            if not stack:
                return f"{name}: closing </{tag}> at line {line} with nothing open"
            open_tag, open_line = stack.pop()
            if open_tag != tag:
                return (f"{name}: <{open_tag}> opened line {open_line} closed by "
                        f"</{tag}> at line {line}")
        else:
            stack.append((tag, line))
    if stack:
        tag, ln = stack[-1]
        return f"{name}: <{tag}> opened at line {ln} is never closed"
    return None


IMPORT_RE = re.compile(r"""from\s+["'](\.[^"']+)["']""")


def check_imports(name, path, raw):
    base = os.path.dirname(path)
    for m in IMPORT_RE.finditer(raw):
        rel = m.group(1)
        target = os.path.normpath(os.path.join(base, rel))
        if not os.path.exists(target):
            return f"{name}: import '{rel}' -> {target} does not exist"
    return None


def main():
    problems = []
    for rel in FILES:
        path = os.path.join(WEB, rel)
        if not os.path.exists(path):
            problems.append(f"{rel}: MISSING")
            continue
        raw = open(path, encoding="utf-8").read()
        cleaned = strip_noise(raw)
        for err in (check_delimiters(rel, cleaned),
                    check_tags(rel, raw),
                    check_imports(rel, path, raw)):
            if err:
                problems.append(err)
        # non-ASCII sweep: a stray lookalike character in an identifier or a
        # CSS/JS value is invisible in review and fatal at build time
        for ln, text in enumerate(raw.splitlines(), 1):
            for ch in text:
                if ord(ch) > 127 and ch not in "—·›↗α✕…–←→≥≤":
                    problems.append(f"{rel}: unexpected non-ASCII {ch!r} (U+{ord(ch):04X}) line {ln}")
        print(f"checked {rel} ({len(raw.splitlines())} lines)")

    css = os.path.join(WEB, "src/styles.css")
    css_raw = open(css, encoding="utf-8").read()
    if css_raw.count("{") != css_raw.count("}"):
        problems.append(f"styles.css: brace mismatch "
                        f"({css_raw.count('{')} open vs {css_raw.count('}')} close)")
    for ln, text in enumerate(css_raw.splitlines(), 1):
        for ch in text:
            if ord(ch) > 127:
                problems.append(f"styles.css: non-ASCII {ch!r} (U+{ord(ch):04X}) line {ln}")
    print(f"checked src/styles.css ({len(css_raw.splitlines())} lines)")

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("\nno structural problems found "
          "(delimiters balanced, JSX tags balanced, relative imports resolve)")


if __name__ == "__main__":
    main()
