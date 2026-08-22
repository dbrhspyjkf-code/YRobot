"""OpenSSH SFTP batch construction tests for the photo album."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from yrobot.config import Settings
from yrobot.photos_sftp import OpensshSftpRunner


def _settings(tmp_path):
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("orangepi.example.test ssh-ed25519 test-key\n")
    return Settings.from_env(
        {
            "YROBOT_PHOTO_UPLOAD_ENABLED": "1",
            "YROBOT_PHOTO_SFTP_HOST": "orangepi.example.test",
            "YROBOT_PHOTO_SFTP_PORT": "22",
            "YROBOT_PHOTO_SFTP_USERNAME": "photo-uploader",
            "YROBOT_PHOTO_SFTP_PASSWORD": "test-only-sftp-password",
            "YROBOT_PHOTO_SFTP_REMOTE_DIR": "/srv/reachy-photos",
            "YROBOT_PHOTO_SFTP_KNOWN_HOSTS": str(known_hosts),
        }
    )


def test_upload_constructs_safe_sftp_batches_without_conflicting_stdin(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("yrobot.photos_sftp.subprocess.run", fake_run)
    local_path = tmp_path / "capture.jpg"
    local_path.write_bytes(b"jpeg")
    runner = OpensshSftpRunner(
        _settings(tmp_path),
        askpass_path=tmp_path / "askpass",
    )

    result = runner.upload(
        photo_id="a" * 32,
        variant="full",
        local_path=local_path,
        remote_rel="2026/08/22/capture",
    )

    assert result.remote_rel == "2026/08/22/capture.full.jpg"
    assert result.byte_count == 4
    assert len(calls) == 2
    mkdir_argv, mkdir_kwargs = calls[0]
    assert "StrictHostKeyChecking=yes" in mkdir_argv
    assert "UserKnownHostsFile=" + str(tmp_path / "known_hosts") in mkdir_argv
    assert "stdin" not in mkdir_kwargs
    assert mkdir_kwargs["input"].splitlines() == [
        "-mkdir /srv/reachy-photos",
        "-mkdir /srv/reachy-photos/2026",
        "-mkdir /srv/reachy-photos/2026/08",
        "-mkdir /srv/reachy-photos/2026/08/22",
    ]
    _, upload_kwargs = calls[1]
    target = "/srv/reachy-photos/2026/08/22/capture.full.jpg"
    assert upload_kwargs["input"].splitlines() == [
        f"-rm {target}.part",
        f"put {local_path} {target}.part",
        f"rename {target}.part {target}",
        f"ls -l {target}",
    ]


def test_fetch_accepts_protocol_arguments_and_uses_one_get_command(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("yrobot.photos_sftp.subprocess.run", fake_run)
    runner = OpensshSftpRunner(
        _settings(tmp_path),
        askpass_path=tmp_path / "askpass",
    )
    destination = tmp_path / "download.jpg"

    runner.fetch(
        photo_id="b" * 32,
        remote_rel="2026/08/22/capture.thumb.jpg",
        local_path=destination,
    )

    assert len(calls) == 1
    _, kwargs = calls[0]
    assert "stdin" not in kwargs
    assert kwargs["input"].splitlines() == [
        f"get /srv/reachy-photos/2026/08/22/capture.thumb.jpg {destination}"
    ]
