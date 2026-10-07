"""Local fault log (FR-2.6): one JSON Lines file, one line per injected fault.

Kept intentionally simple so it can be swapped for a cloud-backed logger
(e.g. DynamoDB) later without changing the engine's calling code.
"""

import json
from pathlib import Path
from typing import List, Optional, Union

DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "fault_log.jsonl"


class FaultLogger:
    def __init__(self, log_path: Optional[Union[str, Path]] = None):
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, fault) -> dict:
        entry = fault.to_log_dict()
        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def read_entries(self) -> List[dict]:
        if not self.log_path.exists():
            return []
        with open(self.log_path, "r") as f:
            return [json.loads(line) for line in f if line.strip()]
