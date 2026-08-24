"""Loading and selecting sources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import yaml

from .util import LOG, compile_patterns, matches_any

TIERS = ("core", "platform", "upstream", "extra")


@dataclass
class Source:
    id: str
    type: str                      # redhat-docs | repo
    label: str = ""
    tier: str = "extra"
    enabled: bool = False
    note: str = ""

    # redhat-docs
    product: str = ""
    version: str = "latest"
    include: List[str] = field(default_factory=list)

    # repo
    repo: str = ""
    ref: str = "main"
    paths: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)

    @property
    def kind_label(self) -> str:
        return "docs" if self.type == "redhat-docs" else "repo"

    def matches(self, path: str) -> bool:
        """For repo sources: is this checked-out file wanted?"""
        if self.exclude and matches_any(path, compile_patterns(self.exclude)):
            return False
        return True


@dataclass
class Catalog:
    sources: List[Source]
    defaults: Dict[str, object] = field(default_factory=dict)
    path: Optional[Path] = None

    def by_id(self, source_id: str) -> Optional[Source]:
        return next((s for s in self.sources if s.id == source_id), None)

    def enabled(self) -> List[Source]:
        return [s for s in self.sources if s.enabled]

    def select(self, requested: Optional[Sequence[str]]) -> List[Source]:
        """Resolve ids / tiers / 'all' / 'enabled' to sources."""
        if not requested:
            return self.enabled()
        chosen: List[Source] = []
        for token in requested:
            for part in str(token).split(","):
                part = part.strip()
                if not part:
                    continue
                if part == "all":
                    chosen += self.sources
                elif part == "enabled":
                    chosen += self.enabled()
                elif part in TIERS:
                    chosen += [s for s in self.sources if s.tier == part]
                elif part in ("docs", "repo", "repos"):
                    want = "redhat-docs" if part == "docs" else "repo"
                    chosen += [s for s in self.sources if s.type == want]
                else:
                    found = self.by_id(part)
                    if found:
                        chosen.append(found)
                    else:
                        LOG.warning("unknown source %r (see: rhkb sources)", part)
        seen, out = set(), []
        for source in chosen:
            if source.id not in seen:
                seen.add(source.id)
                out.append(source)
        return out

    def int_default(self, key: str, fallback: int) -> int:
        try:
            return int(self.defaults.get(key, fallback))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return fallback


def load_catalog(path: Path) -> Catalog:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}")
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path} is not valid YAML: {exc}")

    known = set(Source.__dataclass_fields__)  # type: ignore[attr-defined]
    sources: List[Source] = []
    for raw in data.get("sources") or []:
        unknown = set(raw) - known
        if unknown:
            LOG.warning("source %s: ignoring unknown key(s) %s", raw.get("id"), ", ".join(sorted(unknown)))
        clean = {k: v for k, v in raw.items() if k in known}
        if not clean.get("id") or not clean.get("type"):
            LOG.warning("skipping a source with no id or type")
            continue
        sources.append(Source(**clean))

    ids = [s.id for s in sources]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise SystemExit(f"duplicate source id(s) in {path}: {', '.join(sorted(duplicates))}")

    return Catalog(sources=sources, defaults=data.get("defaults") or {}, path=path)


def set_enabled(path: Path, ids: Sequence[str], value: bool) -> List[str]:
    """Flip `enabled:` in place, preserving comments and layout."""
    text = path.read_text(encoding="utf-8")
    changed: List[str] = []
    for source_id in ids:
        # Find the block for this id, then the first `enabled:` after it.
        block = re.search(rf"(^\s*-\s+id:\s*{re.escape(source_id)}\s*$)(.*?)(?=^\s*-\s+id:|\Z)",
                          text, re.MULTILINE | re.DOTALL)
        if not block:
            LOG.warning("no source with id %r in %s", source_id, path.name)
            continue
        body = block.group(2)
        new_body, count = re.subn(r"(^\s*enabled:\s*)(true|false)\s*$",
                                  lambda m: f"{m.group(1)}{'true' if value else 'false'}",
                                  body, count=1, flags=re.MULTILINE)
        if count == 0:
            new_body = body.rstrip("\n") + f"\n    enabled: {'true' if value else 'false'}\n"
        if new_body != body:
            text = text[:block.start(2)] + new_body + text[block.end(2):]
            changed.append(source_id)
    if changed:
        path.write_text(text, encoding="utf-8")
    return changed
