"""QWEN voice picker must be gated on the QWEN backend.

The voice picker (select + 试听 preview) drives DashScope realtime voices
(`/api/conversation/voice*`), which only exist under the QWEN backend.
Under XIAOZHI the picker is dead UI. Regression guard: the section
defaults to hidden and is revealed only when the *configured* backend
is qwen, mirroring the face-panel gating.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_voice_section_defaults_hidden_in_html():
    html = (ROOT / "yrobot/static/index.html").read_text(encoding="utf-8")
    assert 'id="voice-section" class="voice-section hidden"' in html


def test_voice_section_visibility_follows_configured_backend():
    script = (ROOT / "yrobot/static/main.js").read_text(encoding="utf-8")
    assert "renderVoiceSectionVisibility" in script
    assert 'configuredBackend === "qwen"' in script
    assert 'voiceSection.classList.toggle("hidden"' in script
    # Both load paths must apply the gate: initial render and after save.
    render_src = script.split("function renderBackend")[1].split("\n}")[0]
    save_src = script.split("async function saveBackend")[1].split("\nasync")[0]
    assert "renderVoiceSectionVisibility" in render_src
    assert "renderVoiceSectionVisibility" in save_src


def test_hidden_voice_section_skips_voice_api_fetch():
    script = (ROOT / "yrobot/static/main.js").read_text(encoding="utf-8")
    load_src = script.split("async function loadVoice")[1].split("\n}")[0]
    assert 'voiceSection.classList.contains("hidden")' in load_src
