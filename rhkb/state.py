"""Ingest state.

One JSON file recording, per unit, the content hash that was ingested and
when. A re-fetch that changes a section flips that unit back to `changed`;
everything untouched stays `done`, so a version bump costs you only the
sections that actually moved.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .units import Unit

STATE_NAME = "state.json"
PENDING, DONE, CHANGED, SKIPPED = "pending", "done", "changed", "skipped"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Record:
    sha: str = ""
    status: str = PENDING
    ingested_at: str = ""
    pages: List[str] = field(default_factory=list)   # wiki pages this unit touched
    note: str = ""


class State:
    def __init__(self, root: Path) -> None:
        self.path = Path(root) / STATE_NAME
        self.records: Dict[str, Record] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for key, raw in (data.get("units") or {}).items():
            known = {f for f in Record.__dataclass_fields__}  # type: ignore[attr-defined]
            self.records[key] = Record(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _now(),
            "counts": self.counts(),
            "units": {k: asdict(v) for k, v in sorted(self.records.items())},
        }
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(self.path.parent),
                                             prefix=".state-", suffix=".tmp", delete=False)
        try:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, self.path)
        except Exception:
            handle.close()
            Path(handle.name).unlink(missing_ok=True)
            raise

    # -- queries ----------------------------------------------------------
    def status_of(self, unit: Unit) -> str:
        record = self.records.get(unit.id)
        if record is None:
            return PENDING
        if record.status == SKIPPED:
            return SKIPPED
        if record.status == DONE and record.sha != unit.sha:
            return CHANGED
        return record.status

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for record in self.records.values():
            out[record.status] = out.get(record.status, 0) + 1
        return out

    def outstanding(self, units: List[Unit]) -> List[Unit]:
        return [u for u in units if self.status_of(u) in (PENDING, CHANGED)]

    # -- mutations --------------------------------------------------------
    def mark_done(self, unit: Unit, pages: Optional[List[str]] = None, note: str = "") -> None:
        self.records[unit.id] = Record(sha=unit.sha, status=DONE, ingested_at=_now(),
                                       pages=pages or [], note=note)

    def mark_skipped(self, unit: Unit, note: str = "") -> None:
        self.records[unit.id] = Record(sha=unit.sha, status=SKIPPED, ingested_at=_now(), note=note)

    def reset(self, prefix: str = "") -> int:
        keys = [k for k in self.records if not prefix or k.startswith(prefix)]
        for key in keys:
            del self.records[key]
        return len(keys)
