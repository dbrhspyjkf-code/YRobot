# Dashboard Photo Pagination Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Show exactly six remote-album records per Dashboard page and let the user navigate remaining records with previous/next controls.

**Architecture:** Keep the photo metadata store as the source of truth. Extend the existing `GET /api/photos` endpoint with bounded offset pagination and total count; the Dashboard requests one six-record page at a time and derives button state from the response. This does not change SFTP, image fetch, capture, retry, or deletion semantics.

**Tech Stack:** Python/FastAPI, SQLite, vanilla JavaScript, CSS, pytest.

---

### Task 1: Specify server pagination with a regression test

**Files:**
- Modify: `tests/test_photos.py`
- Modify: `yrobot/photos.py`
- Modify: `yrobot/app_config.py`

**Step 1: Write the failing test**

Add a route test that creates seven photo records, requests `/api/photos?limit=6&offset=6`, and asserts one returned record plus `limit=6`, `offset=6`, and `total=7`.

**Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. uv run --no-project --with pytest python -m pytest -q tests/test_photos.py::test_photo_list_paginates_and_reports_total`

Expected: FAIL because the endpoint does not report pagination metadata or honor `offset`.

**Step 3: Write minimal implementation**

Add a bounded `offset` to `PhotoLibrary.list_metadata`, add a count query, and expose `offset` and `total` from `GET /api/photos` without returning remote connection details.

**Step 4: Run test to verify it passes**

Run the same command. Expected: PASS.

### Task 2: Render six-photo pages in Dashboard

**Files:**
- Modify: `yrobot/static/index.html`
- Modify: `yrobot/static/main.js`
- Modify: `yrobot/static/style.css`

**Step 1: Implement client state and controls**

Add accessible previous/next buttons and a page status below the grid. Request `/api/photos?limit=6&offset=<page * 6>`, render only returned records, disable previous on page one and next on the final page, and reset to page one after a new capture. If deletion makes the current page empty, move back one page and reload.

**Step 2: Validate JavaScript and style**

Run: `node --check yrobot/static/main.js`

Expected: exit status 0.

### Task 3: Verify, document, and commit

**Files:**
- Modify: `scripts/deploy.sh` only if the changed files are not already covered by its rsync/verification flow
- Modify: `AGENT_HANDOFF.md` after acceptance/deployment

**Step 1: Run focused verification**

Run:

```bash
PYTHONPATH=. uv run --no-project --with pytest python -m pytest -q tests/test_photos.py tests/test_photos_sftp.py tests/test_photo_feedback.py
python3 -m py_compile yrobot/photos.py yrobot/app_config.py
git diff --check
```

Expected: all focused tests pass and no whitespace errors.

**Step 2: Commit**

```bash
git add yrobot/photos.py yrobot/app_config.py yrobot/static/index.html yrobot/static/main.js yrobot/static/style.css tests/test_photos.py docs/plans/2026-08-23-dashboard-photo-pagination.md
git commit -m "feat(dashboard): paginate remote photo album"
```

**Step 3: Deploy only with approval**

Run the existing dry-run first. Ask before the service restart required by deployment.
