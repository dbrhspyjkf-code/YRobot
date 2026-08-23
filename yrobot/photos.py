"""Photo capture and OrangePi sync shared library.

The library owns the only mutable copy of a photo for as short as possible:

* When a user or dashboard asks for a new capture, the library atomically writes
  one high-quality JPEG to the local spool directory and records non-secret
  metadata in SQLite.
* A single background worker drains a SQLite-backed queue. Each retry uses an
  exponential backoff and uploads the full JPEG; only after the verified SFTP
  server has renamed that file does the worker unlink the Reachy-side spool
  file. Migrated legacy records retain their thumbnail behavior.
* Operators can replay a failed photo through the Dashboard retry action.
  Delete uses the same verified SFTP transport; metadata is only removed when
  the files recorded for that photo are gone.

The library keeps no password on disk, never embeds the operator's host name
or remote directory in metadata, and refuses to start any new capture when the
local spool would exceed the configured pending-byte cap. The Dashboard never
sees the remote host, user, directory, or password; it only addresses photos
by their opaque UUID.

The SFTP transport lives behind ``SftpRunner`` so tests can drive the queue
without invoking ``openssh-sftp`` and so an operator can later swap
implementations without changing ``PhotoLibrary``.
"""

from __future__ import annotations

import datetime as _dt
import enum
import hashlib
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import cv2
import numpy as np

from yrobot.app_config import CameraStreamer, _MediaHolder
from yrobot.config import Settings

logger = logging.getLogger(__name__)


PHOTO_JPEG_QUALITY = 85
PHOTO_LONG_EDGE = 1280
_PENDING_STATUSES: tuple[str, ...] = ("pending", "uploading", "failed")
_TERMINAL_STATUSES: tuple[str, ...] = ("uploaded",)
_QUEUE_TICK_S = 0.5
_METADATA_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS photos (
        photo_id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at REAL NOT NULL DEFAULT 0,
        last_error TEXT,
        full_path TEXT,
        thumb_path TEXT,
        has_thumbnail INTEGER NOT NULL DEFAULT 1,
        width INTEGER NOT NULL DEFAULT 0,
        height INTEGER NOT NULL DEFAULT 0,
        bytes_total INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT,
        remote_rel TEXT,
        uploaded_at REAL,
        last_access_at REAL
    )
    """
).strip()

_METADATA_COLUMNS = (
    "last_access_at REAL",
    # Existing rows came from the two-file layout, so the migration default
    # deliberately retains their thumbnail fetch/delete behavior.
    "has_thumbnail INTEGER NOT NULL DEFAULT 1",
)


class PhotoStatus(enum.StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    FAILED = "failed"
    UPLOADED = "uploaded"


@dataclass
class PhotoRecord:
    photo_id: str
    created_at: float
    source: str
    width: int = 0
    height: int = 0


@dataclass
class PhotoState:
    photo_id: str
    created_at: float
    source: str
    status: PhotoStatus
    attempts: int
    next_attempt_at: float
    last_error: str | None
    full_path: Path | None
    thumb_path: Path | None
    has_thumbnail: bool
    width: int
    height: int
    bytes_total: int
    sha256: str | None
    remote_rel: str | None
    uploaded_at: float | None

    def as_public_dict(self) -> dict[str, Any]:
        """Return JSON-safe, non-secret metadata for the Dashboard."""
        created_iso = _dt.datetime.fromtimestamp(
            self.created_at, tz=_dt.UTC
        ).astimezone().isoformat(timespec="seconds")
        uploaded_iso = (
            _dt.datetime.fromtimestamp(self.uploaded_at, tz=_dt.UTC)
            .astimezone()
            .isoformat(timespec="seconds")
            if self.uploaded_at
            else None
        )
        return {
            "id": self.photo_id,
            "created_at": created_iso,
            "source": self.source,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "width": self.width,
            "height": self.height,
            "bytes_total": self.bytes_total,
            "sha256": self.sha256,
            "uploaded_at": uploaded_iso,
        }


@dataclass
class PhotoSummary:
    enabled: bool
    pending_bytes: int
    queue_depth: int
    last_success_at: float | None
    last_error: str | None


@dataclass
class CaptureOutcome:
    photo_id: str
    accepted: bool
    message: str


@dataclass
class DeleteOutcome:
    ok: bool
    error: str | None = None


@dataclass
class SftpResult:
    remote_rel: str
    byte_count: int


class SftpRunner(Protocol):
    """Pluggable SFTP transport used by ``PhotoLibrary``.

    Tests provide a fake; production wires the OpenSSH-backed implementation
    from ``scripts/yrobot_photo_askpass.sh``.
    """

    def upload(
        self,
        *,
        photo_id: str,
        variant: str,
        local_path: Path,
        remote_rel: str,
    ) -> SftpResult: ...

    def delete(self, *, photo_id: str, remote_rel: str) -> None: ...

    def fetch(self, *, photo_id: str, remote_rel: str, local_path: Path) -> None: ...


def _utc_iso(timestamp: float) -> str:
    return (
        _dt.datetime.fromtimestamp(timestamp, tz=_dt.UTC)
        .astimezone()
        .isoformat(timespec="seconds")
    )


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = target.with_suffix(target.suffix + ".part")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise
    os.replace(tmp, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


class PhotoLibrary:
    """Local spool + SQLite queue for voice-triggered photo captures."""

    def __init__(
        self,
        *,
        settings: Settings,
        camera: CameraStreamer | None = None,
        media_holder: _MediaHolder | None = None,
        sftp: SftpRunner | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        # next_attempt_at is persisted in SQLite, so its default clock must
        # remain comparable after a process or device restart.
        self._clock = clock or time.time
        self._wall_clock = time.time
        self._camera = camera
        self._media_holder = media_holder
        self.sftp_runner: SftpRunner = sftp or _NullSftpRunner()
        self._spool_dir = Path(os.path.expanduser(settings.photo_spool_dir))
        self._spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._metadata_path = Path(os.path.expanduser(settings.photo_metadata_path))
        self._metadata_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._pending_max_bytes = settings.photo_pending_max_bytes
        self._retry_initial = settings.photo_retry_initial_s
        self._retry_max = settings.photo_retry_max_s
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._init_db()
        self._recover_interrupted_uploads()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping = False
            self._wake_event.set()
            self._worker = threading.Thread(
                target=self._run,
                name="yrobot-photo-sync",
                daemon=True,
            )
            self._worker.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            self._wake_event.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)
            self._worker = None

    # ------------------------------------------------------------------ DB helpers

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._metadata_path, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_METADATA_SCHEMA)
            for column in _METADATA_COLUMNS:
                column_name = column.split()[0]
                rows = conn.execute("PRAGMA table_info(photos)").fetchall()
                names = {str(row["name"]) for row in rows}
                if column_name not in names:
                    conn.execute(f"ALTER TABLE photos ADD COLUMN {column}")

    def _recover_interrupted_uploads(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE photos SET status = ?, last_error = ? WHERE status = ?",
                (
                    PhotoStatus.FAILED.value,
                    "interrupted by previous process",
                    PhotoStatus.UPLOADING.value,
                ),
            )

    # ------------------------------------------------------------------ capture

    def capture_from_voice(self, *, source: str) -> CaptureOutcome:
        return self._capture(source=source, voice_invoked=True)

    def capture_from_dashboard(self) -> CaptureOutcome:
        return self._capture(source="dashboard", voice_invoked=False)

    def _capture(self, *, source: str, voice_invoked: bool) -> CaptureOutcome:
        try:
            jpeg_full = self._capture_jpeg()
        except RuntimeError as exc:
            return CaptureOutcome(photo_id="", accepted=False, message=str(exc))
        if jpeg_full is None:
            return CaptureOutcome(
                photo_id="",
                accepted=False,
                message="拍照失败：摄像头当前没有可用画面",
            )
        bgr = cv2.imdecode(np.frombuffer(jpeg_full, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            return CaptureOutcome(
                photo_id="",
                accepted=False,
                message="拍照失败：图像解码失败",
            )
        height, width = bgr.shape[:2]
        sha256 = hashlib.sha256(jpeg_full).hexdigest()
        photo_id = uuid.uuid4().hex
        full_path = self._spool_dir / f"{photo_id}.full.jpg"
        bytes_total = len(jpeg_full)
        with self._lock:
            pending = self._pending_bytes_locked()
            if pending + bytes_total > self._pending_max_bytes:
                return CaptureOutcome(
                    photo_id="",
                    accepted=False,
                    message="暂存空间已满，请稍后重试或等待已有照片同步成功",
                )
            _atomic_write_bytes(full_path, jpeg_full)
            remote_rel = self._remote_rel(photo_id)
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO photos ("
                    " photo_id, created_at, source, status, attempts, next_attempt_at,"
                    " full_path, width, height, bytes_total, sha256, remote_rel, has_thumbnail"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        photo_id,
                        self._wall_clock(),
                        source,
                        PhotoStatus.PENDING.value,
                        0,
                        self._clock() + (0 if voice_invoked else self._retry_initial),
                        str(full_path),
                        width,
                        height,
                        bytes_total,
                        sha256,
                        remote_rel,
                        0,
                    ),
                )
            self._wake_event.set()
        logger.info(
            "photo capture queued id=%s source=%s bytes=%d",
            photo_id,
            source,
            bytes_total,
        )
        return CaptureOutcome(
            photo_id=photo_id,
            accepted=True,
            message=(
                "已拍照，正在上传到相册"
                if self._settings.photo_upload_enabled
                else "已拍照，等待相册同步"
            ),
        )

    def _capture_jpeg(self) -> bytes | None:
        """Capture a fresh archive-quality JPEG using the shared camera."""
        if self._camera is None:
            raise RuntimeError("机器人摄像头尚未就绪")
        try:
            return self._camera.capture_photo_jpeg(
                long_edge=PHOTO_LONG_EDGE,
                jpeg_quality=PHOTO_JPEG_QUALITY,
            )
        except Exception as exc:  # noqa: BLE001 - capture is best effort
            logger.info("photo camera raised: %s", exc)
            return None

    def _resolve_media(self) -> Any | None:
        if self._camera is not None:
            return self._camera
        if self._media_holder is not None:
            return self._media_holder
        return None

    def _remote_rel(self, photo_id: str) -> str:
        """Return a root-level, opaque stem for one newly captured JPEG."""
        now = _dt.datetime.fromtimestamp(self._wall_clock(), tz=_dt.UTC)
        return f"{now.strftime('%Y%m%dT%H%M%S')}_{photo_id[:12]}"

    # ------------------------------------------------------------------ queue

    def _pending_bytes_locked(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(bytes_total), 0) AS total FROM photos"
                " WHERE status IN (?, ?, ?)",
                _PENDING_STATUSES,
            ).fetchone()
        return int(row["total"] if row else 0)

    def _ready_photo_ids(self) -> list[tuple[str, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT photo_id, attempts FROM photos WHERE status IN (?, ?, ?)"
                " AND next_attempt_at <= ? ORDER BY next_attempt_at",
                (
                    *_PENDING_STATUSES,
                    self._clock(),
                ),
            ).fetchall()
        return [(str(r["photo_id"]), int(r["attempts"])) for r in rows]

    def enqueue_retry(self, photo_id: str) -> bool:
        """Requeue a failed upload only; pending/uploaded records are immutable."""
        if not self._is_valid_id(photo_id):
            return False
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE photos SET status = ?, last_error = NULL,"
                " next_attempt_at = ? WHERE photo_id = ? AND status = ?",
                (
                    PhotoStatus.PENDING.value,
                    self._clock(),
                    photo_id,
                    PhotoStatus.FAILED.value,
                ),
            )
            if result.rowcount != 1:
                return False
        with self._lock:
            self._wake_event.set()
        return True

    def _retry_delay(self, attempts: int) -> float:
        delay = self._retry_initial * (2 ** max(0, attempts))
        return min(delay, self._retry_max)

    # ------------------------------------------------------------------ worker

    def _run(self) -> None:
        while True:
            with self._lock:
                if self._stopping:
                    return
                if not self._settings.photo_upload_enabled:
                    self._wake_event.wait(timeout=_QUEUE_TICK_S)
                    self._wake_event.clear()
                    continue
                ready = self._ready_photo_ids()
                if not ready:
                    self._wake_event.wait(timeout=_QUEUE_TICK_S)
                    self._wake_event.clear()
                    continue
                photo_id, attempts = ready[0]
            try:
                self._upload_one(photo_id, attempts)
            except Exception as exc:  # noqa: BLE001 - keep worker alive
                logger.warning("photo worker iteration crashed: %s", exc)

    def _upload_one(self, photo_id: str, attempts: int) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT full_path, thumb_path, has_thumbnail, remote_rel"
                " FROM photos WHERE photo_id = ?",
                (photo_id,),
            ).fetchone()
            if row is None:
                return
            full_path = Path(row["full_path"])
            thumb_path = Path(row["thumb_path"]) if row["thumb_path"] else None
            has_thumbnail = bool(row["has_thumbnail"])
            remote_rel = str(row["remote_rel"])
            conn.execute(
                "UPDATE photos SET status = ?, attempts = attempts + 1,"
                " last_error = NULL WHERE photo_id = ?",
                (PhotoStatus.UPLOADING.value, photo_id),
            )
        try:
            self.sftp_runner.upload(
                photo_id=photo_id,
                variant="full",
                local_path=full_path,
                remote_rel=remote_rel,
            )
            if has_thumbnail:
                if thumb_path is None:
                    raise RuntimeError("legacy photo thumbnail is missing")
                self.sftp_runner.upload(
                    photo_id=photo_id,
                    variant="thumb",
                    local_path=thumb_path,
                    remote_rel=remote_rel,
                )
        except Exception as exc:
            next_attempt = self._clock() + self._retry_delay(attempts + 1)
            message = str(exc)[:200] or exc.__class__.__name__
            with self._connect() as conn:
                conn.execute(
                    "UPDATE photos SET status = ?, last_error = ?,"
                    " next_attempt_at = ? WHERE photo_id = ?",
                    (PhotoStatus.FAILED.value, message, next_attempt, photo_id),
                )
            logger.info(
                "photo upload failed id=%s attempts=%d err=%s",
                photo_id,
                attempts + 1,
                message,
            )
            return
        with self._connect() as conn:
            conn.execute(
                "UPDATE photos SET status = ?, uploaded_at = ?, last_error = NULL,"
                " full_path = NULL, thumb_path = NULL WHERE photo_id = ?",
                (PhotoStatus.UPLOADED.value, self._wall_clock(), photo_id),
            )
        self._unlink(full_path)
        if thumb_path is not None:
            self._unlink(thumb_path)
        logger.info("photo upload ok id=%s remote=%s", photo_id, remote_rel)

    @staticmethod
    def _unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _run_once_for_test(self, photo_id: str) -> None:
        """Run a single upload iteration for a given photo_id. Tests only."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM photos WHERE photo_id = ?", (photo_id,)
            ).fetchone()
        attempts = int(row["attempts"]) if row else 0
        # Simulate settings being enabled for the test runner.
        original = self._settings.photo_upload_enabled
        object.__setattr__(self._settings, "photo_upload_enabled", True) if isinstance(
            self._settings, Settings
        ) else None
        try:
            self._upload_one(photo_id, attempts)
        finally:
            if isinstance(self._settings, Settings):
                object.__setattr__(self._settings, "photo_upload_enabled", original)

    # ------------------------------------------------------------------ public ops

    def list_metadata(self, *, limit: int = 50, offset: int = 0) -> list[PhotoState]:
        """Return a bounded, newest-first slice of public album metadata."""
        limit = max(1, min(200, int(limit)))
        offset = max(0, min(1_000_000, int(offset)))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM photos ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_state(r) for r in rows]

    def metadata_count(self) -> int:
        """Return the number of locally indexed remote-album records."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM photos").fetchone()
        return int(row["count"] if row else 0)

    def get(self, photo_id: str) -> PhotoState | None:
        if not self._is_valid_id(photo_id):
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM photos WHERE photo_id = ?", (photo_id,)
            ).fetchone()
        return self._row_to_state(row) if row else None

    def status(self) -> PhotoSummary:
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT COALESCE(SUM(bytes_total), 0) AS total FROM photos"
                " WHERE status IN (?, ?, ?)",
                _PENDING_STATUSES,
            ).fetchone()
            depth = conn.execute(
                "SELECT COUNT(*) AS c FROM photos WHERE status IN (?, ?, ?)",
                _PENDING_STATUSES,
            ).fetchone()
            last_ok = conn.execute(
                "SELECT uploaded_at FROM photos WHERE status = ?"
                " ORDER BY uploaded_at DESC LIMIT 1",
                (PhotoStatus.UPLOADED.value,),
            ).fetchone()
            last_err = conn.execute(
                "SELECT last_error FROM photos WHERE status = ?"
                " ORDER BY next_attempt_at DESC LIMIT 1",
                (PhotoStatus.FAILED.value,),
            ).fetchone()
        return PhotoSummary(
            enabled=self._settings.photo_upload_enabled,
            pending_bytes=int(pending["total"] if pending else 0),
            queue_depth=int(depth["c"] if depth else 0),
            last_success_at=(
                float(last_ok["uploaded_at"])
                if last_ok and last_ok["uploaded_at"]
                else None
            ),
            last_error=(
                str(last_err["last_error"])
                if last_err and last_err["last_error"]
                else None
            ),
        )

    def delete_remote(self, photo_id: str) -> DeleteOutcome:
        if not self._is_valid_id(photo_id):
            return DeleteOutcome(ok=False, error="unknown photo")
        state = self.get(photo_id)
        if state is None:
            return DeleteOutcome(ok=False, error="unknown photo")
        if state.status != PhotoStatus.UPLOADED or not state.remote_rel:
            return DeleteOutcome(ok=False, error="photo not uploaded")
        rel_full = f"{state.remote_rel}.full.jpg"
        try:
            self.sftp_runner.delete(photo_id=photo_id, remote_rel=rel_full)
            if state.has_thumbnail:
                self.sftp_runner.delete(
                    photo_id=photo_id,
                    remote_rel=f"{state.remote_rel}.thumb.jpg",
                )
        except Exception as exc:  # noqa: BLE001
            message = str(exc)[:200] or exc.__class__.__name__
            return DeleteOutcome(ok=False, error=message)
        with self._connect() as conn:
            conn.execute("DELETE FROM photos WHERE photo_id = ?", (photo_id,))
        return DeleteOutcome(ok=True)

    def fetch_remote_bytes(self, photo_id: str, variant: str) -> bytes | None:
        """Pull one variant from the remote album and return its bytes.

        Returns ``None`` when the photo is unknown or not yet uploaded. Raises
        the underlying SFTP transport error (without secrets) so callers can
        surface a 502/504 to the Dashboard.
        """
        if not self._is_valid_id(photo_id):
            return None
        if variant not in {"full", "thumb"}:
            return None
        state = self.get(photo_id)
        if state is None or state.status != PhotoStatus.UPLOADED or not state.remote_rel:
            return None
        if variant == "thumb" and not state.has_thumbnail:
            return None
        remote_rel = f"{state.remote_rel}.{variant}.jpg"
        with self._connect() as conn:
            conn.execute(
                "UPDATE photos SET last_access_at = ? WHERE photo_id = ?",
                (self._wall_clock(), photo_id),
            )
        tmp_target = self._spool_dir / f".fetch-{uuid.uuid4().hex}.jpg"
        tmp_target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.sftp_runner.fetch(
                photo_id=photo_id, remote_rel=remote_rel, local_path=tmp_target
            )
            data = tmp_target.read_bytes()
        finally:
            self._unlink(tmp_target)
        return data

    @staticmethod
    def _is_valid_id(photo_id: str) -> bool:
        return bool(re.fullmatch(r"[0-9a-fA-F]{32}", photo_id))

    @staticmethod
    def _row_to_state(row: sqlite3.Row | None) -> PhotoState | None:
        if row is None:
            return None
        full_path = Path(row["full_path"]) if row["full_path"] else None
        thumb_path = Path(row["thumb_path"]) if row["thumb_path"] else None
        uploaded_at = float(row["uploaded_at"]) if row["uploaded_at"] else None
        return PhotoState(
            photo_id=str(row["photo_id"]),
            created_at=float(row["created_at"]),
            source=str(row["source"]),
            status=PhotoStatus(row["status"]),
            attempts=int(row["attempts"]),
            next_attempt_at=float(row["next_attempt_at"]),
            last_error=row["last_error"],
            full_path=full_path,
            thumb_path=thumb_path,
            has_thumbnail=bool(row["has_thumbnail"]),
            width=int(row["width"]),
            height=int(row["height"]),
            bytes_total=int(row["bytes_total"]),
            sha256=row["sha256"],
            remote_rel=row["remote_rel"],
            uploaded_at=uploaded_at,
        )


class _NullSftpRunner:
    """Default runner used when sync is disabled; rejects uploads gracefully."""

    def upload(
        self,
        *,
        photo_id: str,
        variant: str,
        local_path: Path,
        remote_rel: str,
    ) -> SftpResult:
        raise RuntimeError("photo upload is disabled")

    def delete(self, *, photo_id: str, remote_rel: str) -> None:
        raise RuntimeError("photo upload is disabled")

    def fetch(self, *, photo_id: str, remote_rel: str, local_path: Path) -> None:
        raise RuntimeError("photo upload is disabled")


# --------------------------------------------------------------------------- voice


_PHOTO_COMMAND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"帮我\s*拍\s*张\s*照"),
    re.compile(r"帮我\s*拍\s*個\s*照"),
    re.compile(r"帮我\s*拍\s*一\s*张\s*照"),
    re.compile(r"拍\s*张\s*照"),
    re.compile(r"拍\s*一\s*张\s*照"),
    re.compile(r"拍\s*個\s*照"),
    # Xiaozhi ASR often emits the complete command as just “拍照。”.
    # Keep this anchored so unrelated sentences containing the word do not
    # silently capture a photo.
    re.compile(r"^拍\s*照$"),
)


def _normalise_for_photo_command(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text or "").strip().lower()
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"[,.!?;。！？；,!?]", "", cleaned)
    return cleaned


class PhotoCommandController:
    """Backend-neutral gate that decides whether to capture a photo."""

    def __init__(
        self,
        *,
        cooldown_s: float | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._cooldown_s = cooldown_s if cooldown_s is not None else 4.0
        self._last_trigger_at: float = 0.0
        self._last_raw: str = ""
        self._lock = threading.Lock()
        self._now = now or time.monotonic

    def observe(self, transcript: str) -> bool:
        norm = _normalise_for_photo_command(transcript)
        if not norm:
            return False
        if not any(pattern.search(norm) for pattern in _PHOTO_COMMAND_PATTERNS):
            return False
        with self._lock:
            now = self._now()
            if now - self._last_trigger_at < self._cooldown_s:
                self._last_raw = norm
                return False
            self._last_trigger_at = now
            self._last_raw = norm
        return True


# ----------------------------------------------------------- askpass helper


ASSPASS_SCRIPT_TEMPLATE = """#!/usr/bin/env bash
# OpenSSH askpass helper for Reachy photo uploads.
#
# Reads the password from the inherited YROBOT_PHOTO_SFTP_PASSWORD environment
# variable and writes it once to stdout. OpenSSH supplies one prompt argument
# when SSH_ASKPASS_REQUIRE=force is used; the prompt is intentionally ignored.
# The script never echoes anything but the password itself.
set -eu

if [[ "$#" -gt 1 ]]; then
  exit 64
fi

if [[ -z "${YROBOT_PHOTO_SFTP_PASSWORD:-}" ]]; then
  exit 64
fi

printf '%s' "${YROBOT_PHOTO_SFTP_PASSWORD}"
"""


def install_askpass_script(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_text(ASSPASS_SCRIPT_TEMPLATE, encoding="utf-8")
    os.chmod(target, 0o700)
    return target


def openssh_runner(settings: Settings, *, askpass_path: Path | None = None) -> SftpRunner:
    """Build an SFTP runner backed by the system OpenSSH client."""
    from yrobot.photos_sftp import OpensshSftpRunner

    return OpensshSftpRunner(settings, askpass_path=askpass_path)


@dataclass
class _Unused:
    """Marker placeholder; previously hosted shared import housekeeping."""
