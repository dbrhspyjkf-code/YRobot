# Root Single-JPEG Photo Storage Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Save each new Reachy capture as one full JPEG directly in `/home/orangepi/图片/Reachy/`, with no date subdirectories and no thumbnail JPEG generation or upload.

**Architecture:** Preserve the existing SQLite metadata and SFTP safety model, but mark each row with whether it has a legacy thumbnail. New rows contain only the full JPEG and a root-level filename stem; old rows keep their recorded thumbnail behavior so they remain viewable/deletable without unapproved remote cleanup. Dashboard cards fetch the full image directly rather than a stored thumbnail.

**Tech Stack:** Python, SQLite, FastAPI, OpenSSH SFTP, vanilla JavaScript, pytest.

---

### Task 1: Lock in single-file behavior with failing tests

**Files:**
- Modify: `tests/test_photos.py`
- Modify: `tests/test_photos_sftp.py`

**Step 1: Write failing tests**

Add tests that a new capture:
- writes only `<id>.full.jpg` locally;
- stores a root-level `remote_rel` with no `/`;
- uploads and deletes only the full JPEG;
- fetches the full image for a new row and rejects a nonexistent thumbnail.

Add an SFTP batch test asserting a root-level remote stem results in only the configured root `-mkdir` command and a single `*.full.jpg` target.

**Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=. uvx --from pytest --with cryptography --with fastapi --with httpx --with opencv-python-headless --with websockets --with numpy pytest -q tests/test_photos.py tests/test_photos_sftp.py
```

Expected: FAIL because current capture creates/uploads two variants and constructs date paths.

### Task 2: Implement compatible metadata and SFTP change

**Files:**
- Modify: `yrobot/photos.py`
- Modify: `yrobot/photos_sftp.py`
- Modify: `yrobot/app_config.py`
- Modify: `yrobot/static/main.js`
- Modify: `README.md`

**Step 1: Add a schema-compatible legacy thumbnail marker**

Add `has_thumbnail INTEGER NOT NULL DEFAULT 1` through the existing SQLite migration list. Existing rows therefore remain legacy-compatible; new rows insert `0`.

**Step 2: Change new capture and upload flow**

Remove thumbnail encoding and spool writes for new captures. Use a timestamp + opaque ID filename stem with no path separator. Upload full only, remove full local data only after upload succeeds, and preserve the retry/error behavior.

**Step 3: Preserve legacy rows**

Only legacy rows upload/delete/fetch a thumbnail. New rows return no thumbnail and delete only their full JPEG. Do not proactively delete remote legacy thumbnails.

**Step 4: Change Dashboard image fetch**

Use the full JPEG for the card and preview. Default the image route to `full`, while retaining explicitly requested legacy thumb reads for old rows.

**Step 5: Update README**

Describe root-level single-JPEG storage and the revised single-file cleanup invariant.

### Task 3: Verify and deploy safely

**Files:**
- Modify: `AGENT_HANDOFF.md` after verified deployment

**Step 1: Run focused checks**

```bash
PYTHONPATH=. uvx --from pytest --with cryptography --with fastapi --with httpx --with opencv-python-headless --with websockets --with numpy pytest -q tests/test_photos.py tests/test_photos_sftp.py tests/test_photo_feedback.py
python3 -m py_compile yrobot/photos.py yrobot/photos_sftp.py yrobot/app_config.py
node --check yrobot/static/main.js
uvx ruff check yrobot/photos.py yrobot/photos_sftp.py yrobot/app_config.py tests/test_photos.py tests/test_photos_sftp.py
git diff --check
```

**Step 2: Commit and dry-run**

Commit the tested source, tests, docs, and plan; push `feat/reachy-photo-sync`; run `scripts/deploy.sh --dry-run`.

**Step 3: Deploy only with approval**

After approval, update only this line in the robot-local mode-0600 config without printing or copying any secret:

```text
YROBOT_PHOTO_SFTP_REMOTE_DIR=/home/orangepi/图片/Reachy
```

Run the standard deployment script, which backs up current runtime code and restarts YRobot. Do not take a test photo or delete any existing remote thumbnail.
