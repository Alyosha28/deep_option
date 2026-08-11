"""Thread-safe local JSONL recording for legacy snapshots and gateway envelopes."""

from __future__ import annotations

import json
import pathlib
import re
import threading
from typing import TYPE_CHECKING, Iterator, Optional, Union

from .models import Snapshot

if TYPE_CHECKING:
    from .gateway import DataEnvelope

DEFAULT_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "snapshots"

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
_SAFE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_MAX_TAG_LENGTH = 64
_MAX_RECORD_BYTES = 4 * 1024 * 1024
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_RECORDS_PER_FILE = 10_000
_MAX_PATH_LOCKS = 1024
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[pathlib.Path, threading.Lock] = {}
_FALLBACK_PATH_LOCK = threading.Lock()


def _is_link_or_reparse(path: pathlib.Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _validated_source(path: Union[str, pathlib.Path]) -> pathlib.Path:
    source = pathlib.Path(path)
    if _is_link_or_reparse(source) or not source.is_file():
        raise ValueError("snapshot source must be a regular non-link file")
    if source.stat().st_size > _MAX_FILE_BYTES:
        raise ValueError("snapshot source exceeds the configured file-size limit")
    return source


def _path_lock(path: pathlib.Path) -> threading.Lock:
    """Return the process-local lock shared by all recorders for *path*."""
    resolved = path.resolve()
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(resolved)
        if lock is None:
            if len(_PATH_LOCKS) >= _MAX_PATH_LOCKS:
                return _FALLBACK_PATH_LOCK
            lock = threading.Lock()
            _PATH_LOCKS[resolved] = lock
        return lock


def _record_parts(record: Union[Snapshot, "DataEnvelope"]) -> tuple[str, str]:
    """Return ``(captured_at, json_line)`` for a supported record type."""
    if isinstance(record, Snapshot):
        return record.captured_at, record.to_json_line()

    # Kept local so importing the legacy recorder does not force gateway setup.
    from .gateway import DataEnvelope

    if isinstance(record, DataEnvelope):
        return record.captured_at_utc, record.to_json_line()
    raise TypeError("record() accepts only Snapshot or DataEnvelope")


class SnapshotRecorder:
    def __init__(self, target_dir: Union[str, pathlib.Path] = DEFAULT_DIR):
        requested = pathlib.Path(target_dir)
        if requested.exists() and _is_link_or_reparse(requested):
            raise ValueError("snapshot directory must not be a link or reparse point")
        requested.mkdir(parents=True, exist_ok=True)
        if not requested.is_dir():
            raise ValueError("snapshot target must be a directory")
        self.target_dir = requested.resolve()

    def record(
        self, snapshot: Union[Snapshot, "DataEnvelope"], tag: str = "default"
    ) -> pathlib.Path:
        """Append one complete JSON value, grouped by capture date and tag."""
        if (
            not isinstance(tag, str)
            or len(tag) > _MAX_TAG_LENGTH
            or not _SAFE_TAG.fullmatch(tag)
        ):
            raise ValueError("tag must contain only letters, numbers, '.', '_' or '-'")

        captured_at, json_line = _record_parts(snapshot)
        if not isinstance(captured_at, str) or not _DATE_PREFIX.match(captured_at):
            raise ValueError("record must have an ISO-8601 capture timestamp")
        record_size = len(json_line.encode("utf-8"))
        if record_size > _MAX_RECORD_BYTES:
            raise ValueError("record exceeds the configured serialized-size limit")
        date = captured_at[:10]
        path = self.target_dir / f"{date}_{tag}.jsonl"
        # Serialize and write while holding a per-path lock.  A fresh file handle
        # for each call also makes flushing/closing deterministic for readers.
        with _path_lock(path):
            if path.exists():
                if _is_link_or_reparse(path) or not path.is_file():
                    raise ValueError("snapshot output must be a regular non-link file")
                if path.stat().st_size + record_size + 1 > _MAX_FILE_BYTES:
                    raise ValueError("snapshot file exceeds the configured size limit")
                with path.open("r", encoding="utf-8") as existing:
                    if sum(1 for _ in existing) >= _MAX_RECORDS_PER_FILE:
                        raise ValueError("snapshot file exceeds the configured record limit")
            with path.open("a", encoding="utf-8", newline="") as fh:
                fh.write(json_line)
                fh.write("\n")
        return path

    def list_files(self, tag: Optional[str] = None) -> list:
        files = sorted(self.target_dir.glob("*.jsonl"))
        if tag:
            files = [f for f in files if f.name.endswith(f"_{tag}.jsonl")]
        return files


def iter_records(path: Union[str, pathlib.Path]) -> Iterator[Snapshot]:
    """读取 JSONL 快照文件，逐条还原为 Snapshot。"""
    source = _validated_source(path)
    with source.open("r", encoding="utf-8") as fh:
        for line_number, line in enumerate(fh, start=1):
            if line_number > _MAX_RECORDS_PER_FILE:
                raise ValueError("snapshot file exceeds the configured record limit")
            if len(line.encode("utf-8")) > _MAX_RECORD_BYTES:
                raise ValueError("snapshot record exceeds the configured size limit")
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("Snapshot JSONL records must be objects")
            yield Snapshot(
                source=data["source"],
                payload=data["payload"],
                captured_at=data.get("captured_at", ""),
            )


def iter_envelopes(path: Union[str, pathlib.Path]) -> Iterator["DataEnvelope"]:
    """Yield strictly validated :class:`DataEnvelope` records from JSONL.

    Validation, including the persisted content hash, is delegated to
    ``DataEnvelope.from_dict``.  Errors identify the source line while retaining
    the original exception as their cause.
    """
    from .gateway import DataEnvelope

    source = _validated_source(path)
    with source.open("r", encoding="utf-8") as fh:
        for line_number, raw_line in enumerate(fh, start=1):
            if line_number > _MAX_RECORDS_PER_FILE:
                raise ValueError("snapshot file exceeds the configured record limit")
            if len(raw_line.encode("utf-8")) > _MAX_RECORD_BYTES:
                raise ValueError("snapshot record exceeds the configured size limit")
            line = raw_line.strip()
            if not line:
                continue
            try:
                envelope = DataEnvelope.from_json_line(line)
                if not envelope.verify_integrity():
                    raise ValueError("DataEnvelope integrity verification failed")
            except (
                KeyError,
                RecursionError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise ValueError(
                    f"Invalid DataEnvelope at {source.name}:{line_number}: {exc}"
                ) from exc
            yield envelope
