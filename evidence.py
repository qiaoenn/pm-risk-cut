"""Append-only run log.

Every probe run gets its own directory under evidence/.  Two artefacts:
a machine-readable JSONL event stream, and whatever the caller chooses to
print.  The JSONL is the thing that ends up in the evidence pack, so events
carry a timestamp and are written as they happen rather than at the end --
a run that dies mid-cut must still leave a usable trail.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent / "evidence"


class Run:
    def __init__(self, label: str):
        self.id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}"
        self.dir = ROOT / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "events.jsonl"

    def log(self, kind: str, **payload) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(),
                  "kind": kind, **payload}
        with self.path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def __str__(self) -> str:
        return str(self.dir)
