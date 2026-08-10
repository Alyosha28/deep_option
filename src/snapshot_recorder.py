"""SnapshotRecorder：在授权范围内把 Snapshot 追加到本地 JSONL 缓存。"""

from __future__ import annotations

import pathlib
from typing import Iterator, Optional, Union

from .models import Snapshot

DEFAULT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"


class SnapshotRecorder:
    def __init__(self, target_dir: Union[str, pathlib.Path] = DEFAULT_DIR):
        self.target_dir = pathlib.Path(target_dir)
        self.target_dir.mkdir(parents=True, exist_ok=True)

    def record(self, snapshot: Snapshot, tag: str = "default") -> pathlib.Path:
        """追加一条快照；按日期+标签分文件。"""
        date = snapshot.captured_at[:10]
        path = self.target_dir / f"{date}_{tag}.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(snapshot.to_json_line() + "\n")
        return path

    def list_files(self, tag: Optional[str] = None) -> list:
        files = sorted(self.target_dir.glob("*.jsonl"))
        if tag:
            files = [f for f in files if f.name.endswith(f"_{tag}.jsonl")]
        return files


def iter_records(path: Union[str, pathlib.Path]) -> Iterator[Snapshot]:
    """读取 JSONL 快照文件，逐条还原为 Snapshot。"""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            import json

            data = json.loads(line)
            yield Snapshot(
                source=data["source"],
                payload=data["payload"],
                captured_at=data.get("captured_at", ""),
            )
