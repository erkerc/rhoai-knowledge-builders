"""HTML -> Markdown, tuned for documentation pages.

Deliberately narrow: it keeps headings, code blocks, tables, lists and links,
and throws away navigation, feedback widgets and other page chrome. Fidelity
for prose matters less than keeping `oc` commands and YAML blocks byte-exact,
which is the thing PDF extraction destroys.
"""

from __future__ import annotations

import re
from typing import List, Optional

from bs4 import BeautifulSoup, NavigableString, Tag

# Anything matching these is page furniture, not content.
DROP_TAGS = {"script", "style", "nav", "header", "footer", "noscript", "svg", "form", "button"}
DROP_PATTERNS = re.compile(
    r"(toc|breadcrumb|feedback|sidebar|navigation|banner|cookie|search|skip-link|"
    r"page-settings|legal-notice|social|footer|masthead|announce|md-nav|md-header|"
    r"md-footer|md-sidebar|md-search|edit-this-page|rate-this)",
    re.IGNORECASE,
)
CONTENT_SELECTORS = [
    "div.rh-doc-content", "main article", "article", "main", "[role=main]",
    "div.md-content", "div#main-content", "div.section",
]


def make_soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


FENCE_RE = re.compile(r"^\s*(```|~~~)")
VERSION_ONLY_RE = re.compile(r"\d+(\.\d+)*(\.x)?")
PURE_LINK_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+\[[^\]]+\]\(([^)\s]+)\)\s*$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().casefold()


def _title_segments(soup: BeautifulSoup) -> List[str]:
    """The meaningful parts of <title>: no site name, no bare version."""
    raw = soup.title.string if soup.title and soup.title.string else ""
    segments: List[str] = []
    for part in raw.split("|"):
        part = re.sub(r"\s+", " ", part).strip()
        if not part or "red hat documentation" in part.lower():
            continue
        if VERSION_ONLY_RE.fullmatch(part):
            continue
        segments.append(part)
    return segments


def page_title(soup: BeautifulSoup) -> str:
    """The document's own title.

    Not simply the first <h1>: on the newer topic-page products the first <h1>
    is the *product* name inside the navigation block, so every page would get
    the same title. The right heading is the last <h1> that <title> also names;
    otherwise fall back to the first <h1>, then to the first <title> segment.
    """
    known = {_norm(s) for s in _title_segments(soup)}
    headings = [re.sub(r"\s+", " ", h.get_text(" ", strip=True)) for h in soup.find_all("h1")]
    headings = [h for h in headings if h]
    for text in reversed(headings):
        if _norm(text) in known:
            return text
    if headings:
        return headings[0]
    segments = _title_segments(soup)
    return segments[0] if segments else ""


def trim_to_title(md: str, title: str, min_keep: int = 200) -> str:
    """Drop everything before the last `# <title>` heading.

    On topic pages the navigation tree, breadcrumbs and a second copy of the
    tree all come *before* the page's own heading, so cutting there removes
    them without having to guess at CSS classes. Left untouched when the
    heading is not found, or when cutting would leave almost nothing.
    """
    wanted = _norm(title)
    if not wanted:
        return md
    lines = md.splitlines()
    in_fence = False
    cut: Optional[int] = None
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match and _norm(match.group(1)) == wanted:
            cut = index
    if not cut:
        return md
    kept = "\n".join(lines[cut:]).strip() + "\n"
    return kept if len(kept) >= min_keep else md


def nav_leak_count(md: str, marker: str) -> int:
    """Lines that are nothing but a link into the same product and version.

    A converted page with a dozen of these is almost certainly carrying the
    site's navigation tree, which would be repeated in every ingest brief.
    """
    count = 0
    in_fence = False
    for line in md.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = PURE_LINK_RE.match(line)
        if match and marker in match.group(1):
            count += 1
    return count


def _is_decomposed(node: Tag) -> bool:
    """True once bs4 has torn this tag down.

    decompose() clears the tag's __dict__, and Tag.__getattr__ then resolves
    `attrs` to a child search that returns None - so `node.get(...)` raises
    "'NoneType' object has no attribute 'get'". Descendants of a decomposed
    parent hit this too, which matters because we iterate over a list captured
    before any decomposing happened.
    """
    return getattr(node, "_decomposed", False) or not isinstance(getattr(node, "attrs", None), dict)


def _attr(node: Tag, name: str) -> str:
    value = node.attrs.get(name)
    if value is None:
        return ""
    return " ".join(value) if isinstance(value, (list, tuple)) else str(value)


def _is_chrome(node: Tag) -> bool:
    if _is_decomposed(node):
        return False
    if node.name in DROP_TAGS:
        return True
    ident = " ".join(filter(None, [
        _attr(node, "id"), _attr(node, "class"),
        _attr(node, "data-testid"), _attr(node, "role"),
    ]))
    return bool(ident and DROP_PATTERNS.search(ident))


def extract_content(soup: BeautifulSoup) -> Tag:
    """Find the article body, then strip chrome out of it."""
    body: Optional[Tag] = None
    for selector in CONTENT_SELECTORS:
        try:
            found = soup.select_one(selector)
        except Exception:
            found = None
        if found and len(found.get_text(strip=True)) > 200:
            body = found
            break
    if body is None:
        body = soup.body or soup

    for node in list(body.find_all(True)):
        if _is_decomposed(node):
            continue          # a parent we already removed took this one with it
        if _is_chrome(node):
            node.decompose()
    return body


# --- inline ---------------------------------------------------------------
def _inline(node) -> str:
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if not isinstance(node, Tag):
        return ""
    name = node.name.lower()
    if name in DROP_TAGS:
        return ""
    if name == "code":
        text = node.get_text()
        return f"`{text}`" if text else ""
    if name in ("strong", "b"):
        inner = _children(node).strip()
        return f"**{inner}**" if inner else ""
    if name in ("em", "i"):
        inner = _children(node).strip()
        return f"*{inner}*" if inner else ""
    if name == "br":
        return "\n"
    if name == "a":
        text = _children(node).strip()
        href = (node.get("href") or "").strip()
        if not text:
            return ""
        if not href or href.startswith("#"):
            return text
        return f"[{text}]({href})"
    if name == "img":
        alt = (node.get("alt") or "image").strip()
        return f"![{alt}]({node.get('src', '')})"
    return _children(node)


def _children(node: Tag) -> str:
    return "".join(_inline(child) for child in node.children)


# --- blocks ---------------------------------------------------------------
def _code_block(node: Tag) -> str:
    lang = ""
    inner = node.find("code")
    classes = _attr(node, "class").split() + (_attr(inner, "class").split() if inner is not None else [])
    for cls in classes:
        match = re.match(r"(?:language|lang|highlight)-([\w+]+)", str(cls))
        if match:
            lang = match.group(1)
            break
    text = node.get_text()
    text = text.replace("\u00a0", " ").rstrip()
    # docs.redhat.com puts a "Copy to clipboard" affordance inside <pre>.
    text = re.sub(r"^\s*(Copy to [Cc]lipboard|Copied!?)\s*$", "", text, flags=re.MULTILINE)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")
    return f"```{lang}\n{text}\n```" if text else ""


def _table(node: Tag) -> str:
    rows: List[List[str]] = []
    for tr in node.find_all("tr"):
        cells = [re.sub(r"\s+", " ", _children(td)).strip() or " "
                 for td in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [" "] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    for row in rows[1:]:
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
    return "\n".join(out)


def _list(node: Tag, depth: int = 0) -> str:
    ordered = node.name.lower() == "ol"
    lines: List[str] = []
    for index, item in enumerate(node.find_all("li", recursive=False), start=1):
        nested = [child for child in item.find_all(["ul", "ol"], recursive=False)]
        for child in nested:
            child.extract()
        marker = f"{index}." if ordered else "-"
        text = re.sub(r"\s+", " ", _children(item)).strip()
        indent = "  " * depth
        if text:
            lines.append(f"{indent}{marker} {text}")
        for child in nested:
            lines.append(_list(child, depth + 1))
        for block in item.find_all("pre", recursive=False):
            code = _code_block(block)
            if code:
                lines.append("\n".join("  " * (depth + 1) + l for l in code.splitlines()))
    return "\n".join(l for l in lines if l.strip())


def _block(node: Tag) -> str:
    name = node.name.lower()
    if name in DROP_TAGS:
        return ""
    if re.fullmatch(r"h[1-6]", name):
        level = int(name[1])
        text = re.sub(r"\s+", " ", _children(node)).strip()
        text = re.sub(r"\s*(Copy link|#)\s*$", "", text)
        return f"{'#' * level} {text}" if text else ""
    if name == "pre":
        return _code_block(node)
    if name == "table":
        return _table(node)
    if name in ("ul", "ol"):
        return _list(node)
    if name == "blockquote":
        inner = re.sub(r"\s+", " ", _children(node)).strip()
        return "\n".join(f"> {line}" for line in inner.splitlines()) if inner else ""
    if name == "hr":
        return "---"
    if name in ("p", "div", "section", "dd", "dt", "figcaption", "aside", "details", "summary"):
        text = re.sub(r"[ \t]+", " ", _children(node)).strip()
        return text
    return ""


BLOCK_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "table", "ul", "ol",
              "blockquote", "hr", "div", "section"]


def to_markdown(html: str) -> str:
    """Convert a documentation page to markdown."""
    soup = make_soup(html)
    body = extract_content(soup)

    blocks: List[str] = []
    for node in list(body.find_all(BLOCK_TAGS)):
        if _is_decomposed(node):
            continue
        # Skip containers whose children we will visit anyway.
        if node.name in ("div", "section") and node.find(BLOCK_TAGS):
            continue
        if node.find_parent(["pre", "table", "li"]):
            continue
        rendered = _block(node)
        if rendered:
            blocks.append(rendered)

    out: List[str] = []
    for block in blocks:
        if out and out[-1] == block:
            continue  # nested containers can yield the same paragraph twice
        out.append(block)

    text = "\n\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
