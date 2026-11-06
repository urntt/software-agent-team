"""Storage readers and rotation chain decoders for OpenClaw session transcripts.

This module provides functions to safely read, validate, and decode transcripts from
both the modern pinned SQLite storage backend and the legacy rotated JSONL files.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import zstandard

from .types import (
    _MAX_CHAIN_BYTES,
    _MAX_CHAIN_RECORDS,
    _MAX_RECORD_BYTES,
    _MAX_SESSION_BYTES,
    _MAX_SESSION_RECORDS,
    _MAX_SESSION_ROTATIONS,
    _MAX_SQLITE_BYTES,
    _MAX_ZSTD_WINDOW_BYTES,
    OpenClawSessionEvidenceError,
    _BoundOpenClawTranscript,
    _OpenClawSessionFileMissing,
)


def _sha256(value: bytes) -> str:
    """Compute sha256 hex digest of given bytes."""
    return hashlib.sha256(value).hexdigest()


def _read_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    """Safely open and read a regular non-symlink file up to limit bytes."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise _OpenClawSessionFileMissing(f"{label} is not published yet") from error
    except OSError as error:
        raise OpenClawSessionEvidenceError(f"cannot open {label} safely") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OpenClawSessionEvidenceError(f"{label} is not a regular file")
        if metadata.st_size > limit:
            raise OpenClawSessionEvidenceError(f"{label} exceeds its size limit")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise OpenClawSessionEvidenceError(f"{label} exceeds its size limit")
        return payload
    finally:
        os.close(descriptor)


def _require_safe_session_directory(state_dir: Path, agent_id: str) -> Path:
    """Verify that the sessions folder is an owned, non-symlink directory boundary."""
    if not state_dir.is_absolute():
        raise OpenClawSessionEvidenceError("OpenClaw state directory must be absolute")
    current = state_dir
    for part in ("agents", agent_id, "sessions"):
        try:
            metadata = current.lstat()
        except OSError as error:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session directory is unavailable"
            ) from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OpenClawSessionEvidenceError(
                "OpenClaw session directory is not a direct directory boundary"
            )
        current /= part
    try:
        metadata = current.lstat()
    except OSError as error:
        raise OpenClawSessionEvidenceError(
            "OpenClaw session directory is unavailable"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session directory is not a direct directory boundary"
        )
    return current


def _uses_sqlite_sessions(state_dir: Path, agent_id: str) -> bool:
    """Select the pinned runtime's store before any legacy JSONL fallback."""
    try:
        (state_dir / "agents" / agent_id / "agent").lstat()
    except FileNotFoundError:
        return False
    return True


def _require_safe_sqlite_database(state_dir: Path, agent_id: str) -> Path | None:
    """Verify that the SQLite database file and directory permissions are safe."""
    if not state_dir.is_absolute() or not re.fullmatch(r"[a-z][a-z0-9_]*", agent_id):
        raise OpenClawSessionEvidenceError("OpenClaw SQLite state identity is invalid")
    current = state_dir
    for part in ("agents", agent_id, "agent"):
        try:
            metadata = current.lstat()
        except OSError as error:
            raise OpenClawSessionEvidenceError(
                "OpenClaw SQLite session directory is unavailable"
            ) from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise OpenClawSessionEvidenceError(
                "OpenClaw SQLite session directory is unsafe"
            )
        current /= part
    metadata = current.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw SQLite session directory is unsafe"
        )
    database = current / "openclaw-agent.sqlite"
    try:
        metadata = database.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_size > _MAX_SQLITE_BYTES
        or metadata.st_mode & 0o022
    ):
        raise OpenClawSessionEvidenceError("OpenClaw SQLite session store is unsafe")
    return database


def _read_sqlite_bound_transcript(
    state_dir: Path,
    agent_id: str,
    session_key: str,
    *,
    expected_session_id: str | None = None,
    allow_initializing: bool = False,
) -> tuple[str, _BoundOpenClawTranscript | None] | None:
    """Read one exact current session using the pinned SQLite schema, read-only."""
    database = _require_safe_sqlite_database(state_dir, agent_id)
    if database is None:
        return None
    uri = f"file:{quote(str(database), safe='/')}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT current_session_id FROM session_nodes WHERE session_key=?",
                (session_key,),
            ).fetchone()
            if row is None:
                return None
            session_id = row[0]
            if not _safe_session_id(session_id):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw SQLite session ID is unsafe"
                )
            if expected_session_id is not None and session_id != expected_session_id:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw SQLite session does not bind the invocation"
                )
            window = connection.execute(
                "SELECT session_key FROM session_windows WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if window is None or window[0] != session_key:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw SQLite session window does not bind the invocation"
                )
            rows = connection.execute(
                "SELECT seq, event_json, event_zstd FROM transcript_events "
                "WHERE session_id=? ORDER BY seq LIMIT ?",
                (session_id, _MAX_CHAIN_RECORDS + 1),
            ).fetchall()
    except sqlite3.Error as error:
        if allow_initializing and (
            "no such table" in str(error) or "database is locked" in str(error)
        ):
            return None
        raise OpenClawSessionEvidenceError(
            "OpenClaw SQLite session could not be read safely"
        ) from error
    if not rows:
        return session_id, None
    if len(rows) > _MAX_CHAIN_RECORDS:
        raise OpenClawSessionEvidenceError("OpenClaw SQLite transcript is too long")
    digest = hashlib.sha256(b"SAT OpenClaw SQLite transcript v1\0")
    digest.update(session_id.encode("ascii") + b"\0")
    records: list[dict[str, object]] = []
    total_bytes = 0
    for expected_seq, (seq, event_json, event_zstd) in enumerate(rows):
        if seq != expected_seq:
            raise OpenClawSessionEvidenceError(
                "OpenClaw SQLite transcript contains an unsupported event"
            )
        if isinstance(event_json, str) and event_zstd is None:
            payload = event_json.encode("utf-8", errors="strict")
        elif event_json is None and isinstance(event_zstd, bytes):
            if len(event_zstd) > _MAX_RECORD_BYTES:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw SQLite transcript exceeds its size limit"
                )
            try:
                chunks: list[bytes] = []
                remaining = _MAX_RECORD_BYTES + 1
                with zstandard.ZstdDecompressor(
                    max_window_size=_MAX_ZSTD_WINDOW_BYTES
                ).stream_reader(
                    io.BytesIO(event_zstd), read_across_frames=True
                ) as reader:
                    while remaining:
                        chunk = reader.read(min(64 * 1024, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                if remaining == 0:
                    raise OpenClawSessionEvidenceError(
                        "OpenClaw SQLite transcript exceeds its size limit"
                    )
                payload = b"".join(chunks)
                payload.decode("utf-8", errors="strict")
            except (zstandard.ZstdError, UnicodeDecodeError) as error:
                raise OpenClawSessionEvidenceError(
                    "OpenClaw SQLite transcript contains invalid compressed event"
                ) from error
        else:
            raise OpenClawSessionEvidenceError(
                "OpenClaw SQLite transcript contains an unsupported event"
            )
        total_bytes += len(payload)
        if len(payload) > _MAX_RECORD_BYTES or total_bytes > _MAX_CHAIN_BYTES:
            raise OpenClawSessionEvidenceError(
                "OpenClaw SQLite transcript exceeds its size limit"
            )
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
        records.append(_load_json_object(payload, label="OpenClaw SQLite record"))
    if records[0].get("type") != "session" or records[0].get("id") != session_id:
        raise OpenClawSessionEvidenceError(
            "OpenClaw SQLite transcript identity differs from the invocation"
        )
    return session_id, _BoundOpenClawTranscript(
        tuple(records), digest.hexdigest(), True
    )


def _load_json_object(payload: bytes, *, label: str) -> dict[str, object]:
    """Parse bytes as valid UTF-8 JSON object."""
    try:
        decoded = payload.decode("utf-8", errors="strict")
        value = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise OpenClawSessionEvidenceError(
            f"{label} is not valid UTF-8 JSON"
        ) from error
    if not isinstance(value, dict):
        raise OpenClawSessionEvidenceError(f"{label} must be a JSON object")
    return value


def _load_session_records(payload: bytes) -> tuple[dict[str, object], ...]:
    """Parse newline-delimited JSONL lines into tuples of JSON objects."""
    if not payload or not payload.endswith(b"\n"):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session transcript is empty or incomplete"
        )
    lines = payload.splitlines()
    if not lines or len(lines) > _MAX_SESSION_RECORDS:
        raise OpenClawSessionEvidenceError(
            "OpenClaw session transcript has an invalid record count"
        )
    records: list[dict[str, object]] = []
    for line in lines:
        if not line or len(line) > _MAX_RECORD_BYTES:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session transcript contains an invalid record"
            )
        records.append(_load_json_object(line, label="OpenClaw session record"))
    return tuple(records)


def _safe_session_id(value: object) -> bool:
    """Validate that session ID is a bounded alphanumeric string."""
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= 128
        and all(
            character.isascii() and (character.isalnum() or character in "-_")
            for character in value
        )
    )


def _bound_session_path(sessions: Path, session_id: str, value: object) -> Path:
    """Validate and resolve an in-boundary session JSONL path."""
    expected = sessions / f"{session_id}.jsonl"
    if value is None:
        return expected
    if not isinstance(value, str):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session index points outside the expected transcript"
        )
    path = Path(value)
    rotated_name = re.fullmatch(
        rf"\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-\d{{3}}Z_{re.escape(session_id)}\.jsonl",
        path.name,
    )
    if (
        not path.is_absolute()
        or path.parent != sessions
        or (path != expected and rotated_name is None)
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw session index points outside the expected transcript"
        )
    return path


def _read_session_segment(
    path: Path, session_id: str, *, allow_incomplete: bool
) -> tuple[bytes, tuple[dict[str, object], ...], bool]:
    """Read one single JSONL session segment."""
    payload = _read_regular_file(
        path, limit=_MAX_SESSION_BYTES, label="OpenClaw session transcript"
    )
    complete = payload.endswith(b"\n")
    if allow_incomplete:
        lines = payload.splitlines()
        if not complete:
            lines = lines[:-1]
        if len(lines) > _MAX_SESSION_RECORDS:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session transcript has an invalid record count"
            )
        if any(not line or len(line) > _MAX_RECORD_BYTES for line in lines):
            raise OpenClawSessionEvidenceError(
                "OpenClaw session transcript contains an invalid record"
            )
        records = tuple(
            _load_json_object(line, label="OpenClaw session record") for line in lines
        )
    else:
        records = _load_session_records(payload)
    if records and (
        records[0].get("type") != "session" or records[0].get("id") != session_id
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw transcript identity differs from the invocation session"
        )
    return payload, records, complete


def _rotation_chain(
    sessions: Path,
    entry: dict[str, object],
    *,
    session_key: str,
    session_id: str,
) -> tuple[tuple[str, Path], tuple[dict[str, object], ...]]:
    """Determine sequence of rotated session files forming one logical transcript."""
    current = _bound_session_path(sessions, session_id, entry.get("sessionFile"))
    family = entry.get("usageFamilySessionIds")
    checkpoints = entry.get("compactionCheckpoints")
    if family is None and checkpoints is None:
        if current != sessions / f"{session_id}.jsonl":
            raise OpenClawSessionEvidenceError(
                "OpenClaw rotated transcript has no compaction lineage"
            )
        return ((session_id, current),), ()
    if (
        not isinstance(family, list)
        or not family
        or len(family) > _MAX_SESSION_ROTATIONS + 1
        or any(not _safe_session_id(value) for value in family)
        or len(set(family)) != len(family)
        or family[-1] != session_id
        or not isinstance(checkpoints, list)
        or len(checkpoints) != len(family) - 1
        or entry.get("compactionCount") != len(checkpoints)
    ):
        raise OpenClawSessionEvidenceError("OpenClaw compaction lineage is invalid")
    if len(family) == 1:
        if current != sessions / f"{session_id}.jsonl":
            raise OpenClawSessionEvidenceError(
                "OpenClaw rotated transcript has no compaction lineage"
            )
        return ((session_id, current),), ()
    chain = [(family[0], sessions / f"{family[0]}.jsonl")]
    for index, checkpoint in enumerate(checkpoints):
        if not isinstance(checkpoint, dict):
            raise OpenClawSessionEvidenceError("OpenClaw compaction lineage is invalid")
        before = checkpoint.get("preCompaction")
        after = checkpoint.get("postCompaction")
        if (
            checkpoint.get("sessionKey") != session_key
            or checkpoint.get("sessionId") != family[index + 1]
            or not isinstance(before, dict)
            or before.get("sessionId") != family[index]
            or not isinstance(after, dict)
            or after.get("sessionId") != family[index + 1]
            or not isinstance(before.get("entryId"), str)
            or not isinstance(after.get("entryId"), str)
        ):
            raise OpenClawSessionEvidenceError("OpenClaw compaction lineage is invalid")
        successor = _bound_session_path(
            sessions, family[index + 1], after.get("sessionFile")
        )
        chain.append((family[index + 1], successor))
    if chain[-1][1] != current:
        raise OpenClawSessionEvidenceError("OpenClaw compaction lineage is invalid")
    return tuple(chain), tuple(checkpoints)


def _join_rotated_records(
    previous: tuple[dict[str, object], ...],
    successor: tuple[dict[str, object], ...],
    checkpoint: dict[str, object],
    *,
    previous_path: Path,
) -> tuple[dict[str, object], ...]:
    """Validate compaction boundaries and merge successor continuation records."""
    before = checkpoint["preCompaction"]
    after = checkpoint["postCompaction"]
    marker_id = after["entryId"]
    for records in (previous, successor):
        identifiers = [record.get("id") for record in records]
        if any(not isinstance(identifier, str) for identifier in identifiers) or len(
            set(identifiers)
        ) != len(identifiers):
            raise OpenClawSessionEvidenceError(
                "OpenClaw compaction entries are invalid"
            )
    if (
        len(previous) < 2
        or previous[-1].get("type") != "compaction"
        or previous[-1].get("id") != marker_id
        or previous[-1].get("parentId") != before["entryId"]
        or previous[-2].get("id") != before["entryId"]
        or successor[0].get("parentSession") != str(previous_path)
    ):
        raise OpenClawSessionEvidenceError("OpenClaw compaction boundary is invalid")
    marker_positions = [
        index
        for index, record in enumerate(successor)
        if record.get("type") == "compaction" and record.get("id") == marker_id
    ]
    if len(marker_positions) != 1:
        raise OpenClawSessionEvidenceError("OpenClaw compaction boundary is invalid")
    marker_index = marker_positions[0]
    new_marker = successor[marker_index]
    old_marker = previous[-1]
    if {
        key: value for key, value in new_marker.items() if key != "firstKeptEntryId"
    } != {key: value for key, value in old_marker.items() if key != "firstKeptEntryId"}:
        raise OpenClawSessionEvidenceError("OpenClaw compaction boundary is invalid")
    kept_start = next(
        (
            index
            for index, record in enumerate(successor[1:marker_index], start=1)
            if record.get("type") == "message"
        ),
        marker_index,
    )
    if successor[1:kept_start] != previous[1:kept_start]:
        raise OpenClawSessionEvidenceError("OpenClaw compaction boundary is invalid")
    kept = successor[kept_start:marker_index]
    old_kept = previous[len(previous) - 1 - len(kept) : -1]
    if len(kept) != len(old_kept):
        raise OpenClawSessionEvidenceError("OpenClaw compaction boundary is invalid")
    for index, (new_record, old_record) in enumerate(zip(kept, old_kept, strict=True)):
        if index == 0:
            if {
                key: value for key, value in new_record.items() if key != "parentId"
            } != {
                key: value for key, value in old_record.items() if key != "parentId"
            } or new_record.get("parentId") != successor[kept_start - 1].get("id"):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw compaction boundary is invalid"
                )
        elif new_record != old_record:
            raise OpenClawSessionEvidenceError(
                "OpenClaw compaction boundary is invalid"
            )
    if checkpoint.get("firstKeptEntryId") not in {record.get("id") for record in kept}:
        raise OpenClawSessionEvidenceError("OpenClaw compaction boundary is invalid")
    continuation = successor[marker_index + 1 :]
    existing_ids = {record.get("id") for record in previous}
    if any(record.get("id") in existing_ids for record in continuation):
        raise OpenClawSessionEvidenceError("OpenClaw compaction repeats an entry")
    if any(
        successor[index].get("parentId") != successor[index - 1].get("id")
        for index in range(marker_index + 1, len(successor))
    ):
        raise OpenClawSessionEvidenceError(
            "OpenClaw compaction continuation is invalid"
        )
    return continuation


def _read_bound_transcript(
    sessions: Path,
    entry: dict[str, object],
    *,
    session_key: str,
    session_id: str,
    allow_incomplete: bool,
) -> _BoundOpenClawTranscript | None:
    """Read and assemble one bound transcript from its rotation chain."""
    chain, checkpoints = _rotation_chain(
        sessions, entry, session_key=session_key, session_id=session_id
    )
    combined: tuple[dict[str, object], ...] = ()
    prior_segment: tuple[dict[str, object], ...] = ()
    payloads: list[bytes] = []
    total_bytes = 0
    latest_complete = False
    for index, (segment_id, path) in enumerate(chain):
        try:
            payload, records, latest_complete = _read_session_segment(
                path,
                segment_id,
                allow_incomplete=allow_incomplete and index == len(chain) - 1,
            )
        except _OpenClawSessionFileMissing:
            if allow_incomplete and index == len(chain) - 1:
                return None
            if index == len(chain) - 1:
                raise
            raise OpenClawSessionEvidenceError(
                "OpenClaw compaction ancestor is unavailable"
            ) from None
        total_bytes += len(payload)
        if total_bytes > _MAX_CHAIN_BYTES:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session chain exceeds its size limit"
            )
        if not records:
            if allow_incomplete and index == len(chain) - 1:
                return None
            raise OpenClawSessionEvidenceError("OpenClaw session transcript is empty")
        if index:
            if (
                not any(
                    record.get("type") == "compaction"
                    and record.get("id")
                    == checkpoints[index - 1]["postCompaction"]["entryId"]
                    for record in records
                )
                and allow_incomplete
                and not latest_complete
            ):
                return None
            continuation = _join_rotated_records(
                prior_segment,
                records,
                checkpoints[index - 1],
                previous_path=chain[index - 1][1],
            )
            combined_ids = {record.get("id") for record in combined}
            if any(record.get("id") in combined_ids for record in continuation):
                raise OpenClawSessionEvidenceError(
                    "OpenClaw compaction repeats an entry"
                )
            combined += continuation
        else:
            combined = records
        prior_segment = records
        if len(combined) > _MAX_CHAIN_RECORDS:
            raise OpenClawSessionEvidenceError(
                "OpenClaw session chain has an invalid record count"
            )
        payloads.append(payload)
    if len(payloads) == 1:
        digest = _sha256(payloads[0])
    else:
        hasher = hashlib.sha256(b"SAT OpenClaw compaction chain v1\0")
        for payload in payloads:
            hasher.update(len(payload).to_bytes(8, "big"))
            hasher.update(payload)
        digest = hasher.hexdigest()
    return _BoundOpenClawTranscript(combined, digest, latest_complete)
