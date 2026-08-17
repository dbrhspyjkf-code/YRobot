"""Face panel must be gated on the QWEN backend.

Face recognition (`yrobot/faces.py`) is only wired into the QWEN session
path (`monitor_face_identity` in main.py). Under XIAOZHI the recognition
never runs, so the dashboard panel would show a permanently dead
"当前识别" area. Regression guard: the panel defaults to hidden and the
frontend reveals it only when the *configured* backend is qwen.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_face_panel_defaults_hidden_in_html():
    html = (ROOT / "yrobot/static/index.html").read_text(encoding="utf-8")
    # Default-hidden: a XIAOZHI-mode dashboard (project default backend)
    # must never render — or even flash — the face panel before JS runs.
    assert 'id="face-panel" class="panel face-panel hidden"' in html


def test_face_panel_visibility_follows_configured_backend():
    script = (ROOT / "yrobot/static/main.js").read_text(encoding="utf-8")
    # Visibility driven by the configured backend (same signal the
    # backend switch buttons use), not by the transient running backend.
    assert "renderFacePanelVisibility" in script
    assert 'configuredBackend === "qwen"' in script
    # Toggling reuses the existing `.panel.hidden { display: none; }` rule.
    assert 'facePanel.classList.toggle("hidden"' in script
    # Both load paths must apply the gate: initial render and after save.
    render_src = script.split("function renderBackend")[1].split("\n}")[0]
    save_src = script.split("async function saveBackend")[1].split("\nasync")[0]
    assert "renderFacePanelVisibility" in render_src
    assert "renderFacePanelVisibility" in save_src


def test_hidden_face_panel_skips_face_api_fetch():
    script = (ROOT / "yrobot/static/main.js").read_text(encoding="utf-8")
    load_src = script.split("async function loadFaces")[1].split("\n}")[0]
    # No pointless /api/face polling while the panel is hidden.
    assert 'facePanel.classList.contains("hidden")' in load_src
