"""OpenSSH-backed SFTP runner for the Reachy photo library.

This module deliberately depends only on the system ``sftp`` binary and the
operator-supplied SSH ``known_hosts`` file. It never logs the password and never
lowers the host-key verification policy: unknown host keys, mismatched
fingerprints, or transient network errors all surface as ``SftpTransportError``
so the photo library can retry with backoff.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from yrobot.config import Settings
from yrobot.photos import SftpRemoteMissingError, SftpResult, install_askpass_script

logger = logging.getLogger(__name__)


class SftpTransportError(RuntimeError):
    """Raised when an SFTP upload or delete cannot complete safely."""


class OpensshSftpRunner:
    """Wrap the OpenSSH ``sftp`` client to upload and delete remote photos."""

    def __init__(
        self,
        settings: Settings,
        *,
        askpass_path: Path | None = None,
        command_timeout_s: float = 30.0,
        binary: str = "sftp",
    ) -> None:
        if not settings.photo_upload_enabled:
            raise SftpTransportError("photo upload is disabled in settings")
        for required in (
            settings.photo_sftp_host,
            settings.photo_sftp_username,
            settings.photo_sftp_password,
            settings.photo_sftp_remote_dir,
            settings.photo_sftp_known_hosts,
        ):
            if not required:
                raise SftpTransportError("incomplete SFTP configuration")
        self._settings = settings
        self._command_timeout_s = command_timeout_s
        self._binary = binary
        self._known_hosts = Path(os.path.expanduser(settings.photo_sftp_known_hosts))
        self._remote_dir = settings.photo_sftp_remote_dir.rstrip("/") or "/"
        if askpass_path is None:
            self._askpass_path = install_askpass_script(
                Path(os.path.expanduser("~/.local/state/yrobot/yrobot_photo_askpass.sh"))
            )
        else:
            self._askpass_path = askpass_path
        self._env_template = {
            "SSH_ASKPASS": str(self._askpass_path),
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": ":0",
            "YROBOT_PHOTO_SFTP_PASSWORD": settings.photo_sftp_password,
        }

    # ---------------------------------------------------------------- public API

    def upload(
        self,
        *,
        photo_id: str,
        variant: str,
        local_path: Path,
        remote_rel: str,
    ) -> SftpResult:
        if variant not in {"full", "thumb"}:
            raise SftpTransportError(f"unsupported variant: {variant}")
        target = self._remote_path(f"{remote_rel}.{variant}.jpg")
        tmp_target = f"{target}.part"
        self._ensure_remote_dir(remote_rel)
        commands = [
            f"-rm {_quote(tmp_target)}",
            f"put {_quote(local_path)} {_quote(tmp_target)}",
            f"rename {_quote(tmp_target)} {_quote(target)}",
            f"ls -l {_quote(target)}",
        ]
        self._run_batch(commands)
        size = local_path.stat().st_size
        return SftpResult(remote_rel=f"{remote_rel}.{variant}.jpg", byte_count=size)

    def delete(self, *, photo_id: str, remote_rel: str) -> None:
        target = self._remote_path(remote_rel)
        try:
            self._run_batch([f"rm {_quote(target)}"])
        except SftpTransportError as exc:
            if _is_remote_file_missing(str(exc)):
                raise SftpRemoteMissingError("remote photo file is already missing") from exc
            raise

    # ---------------------------------------------------------------- helpers

    def _remote_path(self, relative_path: str) -> str:
        relative_path = relative_path.lstrip("/")
        if self._remote_dir == "/":
            return f"/{relative_path}"
        return f"{self._remote_dir}/{relative_path}"

    def _ensure_remote_dir(self, remote_rel: str) -> None:
        parts = remote_rel.split("/")[:-1]
        target = self._remote_dir
        commands = [f"-mkdir {_quote(target)}"]
        current: list[str] = []
        for part in parts:
            current.append(part)
            target = self._remote_path("/".join(current))
            commands.append(f"-mkdir {_quote(target)}")
        self._run_batch(commands)

    def _run_batch(self, commands: Sequence[str]) -> None:
        argv = [
            self._binary,
            "-o",
            "BatchMode=no",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._known_hosts}",
            "-o",
            "ConnectTimeout=10",
            "-P",
            str(self._settings.photo_sftp_port),
            "-b",
            "-",
            f"{self._settings.photo_sftp_username}@{self._settings.photo_sftp_host}",
        ]
        env = dict(os.environ)
        env.update(self._env_template)
        try:
            result = subprocess.run(
                argv,
                input="\n".join(commands) + "\n",
                capture_output=True,
                text=True,
                timeout=self._command_timeout_s,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SftpTransportError(f"sftp timed out: {exc}") from exc
        if result.returncode != 0:
            sanitised = _sanitise_stderr(result.stderr)
            raise SftpTransportError(
                f"sftp failed: code={result.returncode} stderr={sanitised}"
            )

    def fetch(self, *, photo_id: str, remote_rel: str, local_path: Path) -> None:
        commands = [
            f"get {_quote(self._remote_path(remote_rel))} {_quote(local_path)}"
        ]
        self._run_batch(commands)


def _is_remote_file_missing(message: str) -> bool:
    """Recognize only OpenSSH's unambiguous absent-file response.

    This is called exclusively by ``delete``. Do not broaden it to generic
    server failures: permissions, host verification, connectivity, and all
    other errors must keep the Dashboard record intact for safe retry.
    """
    normalised = message.casefold()
    if "no such file" not in normalised:
        return False
    return (
        "remote delete" in normalised
        or "couldn't stat remote file" in normalised
    )


def _quote(value: str | Path) -> str:
    """Quote one value for the SFTP client's command parser.

    This quote handling is local to the OpenSSH SFTP client; it never invokes
    a remote shell. The password is never passed through here.
    """
    return shlex.quote(str(value))


def _sanitise_stderr(stderr: str) -> str:
    """Strip password-like fragments from stderr without revealing secrets."""
    if not stderr:
        return ""
    fragments = [line for line in stderr.splitlines() if line.strip()]
    safe: list[str] = []
    for line in fragments[:8]:
        if "Permission denied" in line or "Host key verification failed" in line:
            safe.append(line.strip())
        elif "password" in line.lower():
            safe.append("password prompt suppressed")
        else:
            safe.append(line.strip()[:160])
    return " | ".join(safe)
