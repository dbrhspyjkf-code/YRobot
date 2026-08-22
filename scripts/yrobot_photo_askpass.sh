#!/usr/bin/env bash
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
