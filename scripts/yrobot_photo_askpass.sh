#!/usr/bin/env bash
# OpenSSH askpass helper for Reachy photo uploads.
#
# Reads the password from the inherited YROBOT_PHOTO_SFTP_PASSWORD environment
# variable and writes it once to stdout. Invoked only by the SFTP worker via
# SSH_ASKPASS_REQUIRE=force; the script does not accept any positional
# argument and never echoes anything but the password itself.
set -eu

if [[ "$#" -ne 0 ]]; then
  exit 64
fi

if [[ -z "${YROBOT_PHOTO_SFTP_PASSWORD:-}" ]]; then
  exit 64
fi

printf '%s' "${YROBOT_PHOTO_SFTP_PASSWORD}"
