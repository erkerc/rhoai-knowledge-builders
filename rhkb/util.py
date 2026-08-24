"""Small shared helpers."""

from __future__ import annotations

import hashlib
import logging
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

LOG = logging.getLogger("rhkb")

#: Rough characters-per-token for English prose plus YAML/CLI snippets. Good
#: enough for budgeting; nothing here needs to be exact.
CHARS_PER_TOKEN = 4.0


def setup_logging(verbose: int = 0, quiet: bool = False) -> None:
    level = logging.WARNING if quiet else (logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    LOG.handlers[:] = [handler]
    LOG.setLevel(level)
    LOG.propagate = False


def est_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def human_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def slug(text: str, maxlen: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower())
    text = re.sub(r"[\s_]+", "-", text).strip("-")
    return re.sub(r"-+", "-", text)[:maxlen] or "untitled"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def frontmatter(fields: Dict[str, object]) -> str:
    """Minimal YAML frontmatter writer - values are scalars or flat lists."""
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            rendered = ", ".join(str(v) for v in value)
            lines.append(f"{key}: [{rendered}]")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            text = str(value)
            if any(c in text for c in ":#{}[]") and not text.startswith('"'):
                text = '"' + text.replace('"', "'") + '"'
            lines.append(f"{key}: {text}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def split_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    """Return (fields, body). Values stay strings; this is only for reading back."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head = text[3:end]
    body = text[end + 4:].lstrip("\n")
    fields: Dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
    return fields, body


def compile_patterns(patterns: Optional[Iterable[str]]) -> List[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in (patterns or [])]


def matches_any(text: str, patterns: List[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
