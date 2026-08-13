from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_exposes_local_face_management_controls():
    html = (ROOT / "yrobot/static/index.html").read_text(encoding="utf-8")
    script = (ROOT / "yrobot/static/main.js").read_text(encoding="utf-8")

    assert 'id="face-panel"' in html
    assert 'id="face-name"' in html
    assert 'id="face-register"' in html
    assert 'id="face-list"' in html
    assert 'fetch("/api/face"' in script
    assert 'method: "DELETE"' in script
