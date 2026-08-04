# Home Assistant Whitelist Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local, whitelist-only Home Assistant control to YRobot so approved spoken commands can call specific HA services.

**Architecture:** Keep the online realtime model as conversation-only. Local deterministic code inspects gated assistant text fragments, matches configured whitelist phrases, deduplicates per model response, and calls Home Assistant REST APIs only when HA is explicitly enabled. The HA token stays in environment configuration and is never committed or logged.

**Tech Stack:** Python 3.12, standard library `json`, `urllib.request`, `urllib.error`, existing `pytest` suite, existing frozen `Settings` dataclass.

## Global Constraints

- `YROBOT_HA_ENABLED` defaults to disabled behavior.
- `YROBOT_HA_URL` stores the Home Assistant base URL, for example `http://192.168.1.133:8123`.
- `YROBOT_HA_TOKEN` stores the Home Assistant long-lived access token and must not be committed or printed.
- Default whitelist path is `~/.config/yrobot/home_assistant_whitelist.json`.
- First version excludes locks, alarm panels, garage doors, covers, gas, heaters, and other safety-critical or high-risk entities.
- Use only the Python standard library for HA HTTP calls.
- Existing YRobot behavior must remain unchanged when HA is disabled.

---

## File Structure

- Modify `yrobot/config.py`: add HA fields to `Settings`, default them to disabled, and parse env values in `Settings.from_env()`.
- Create `yrobot/home_assistant.py`: focused HA settings checks, whitelist parsing, phrase matching, dedupe, and REST service calls.
- Modify `yrobot/main.py`: instantiate the HA controller and feed allowed assistant text fragments after the existing text gate.
- Create `tests/test_home_assistant.py`: unit tests for whitelist matching, dedupe, and REST client behavior.
- Modify `tests/test_config.py`: HA env parsing and default-disabled tests.
- Optionally create a sample whitelist after implementation only if needed for operator setup; do not include secrets.

---

### Task 1: Add HA Settings

**Files:**
- Modify: `yrobot/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Settings.ha_enabled: bool`, `Settings.ha_url: str | None`, `Settings.ha_token: str | None`, `Settings.ha_whitelist_path: str`
- Consumes: existing `_flag()`, `Settings.from_env()`

- [ ] **Step 1: Write failing default-disabled test**

Add this to `tests/test_config.py`:

```python
def test_home_assistant_defaults_disabled():
    settings = Settings()
    assert settings.ha_enabled is False
    assert settings.ha_url is None
    assert settings.ha_token is None
    assert settings.ha_whitelist_path == "~/.config/yrobot/home_assistant_whitelist.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_home_assistant_defaults_disabled -q`

Expected: FAIL with `AttributeError` because HA settings do not exist yet.

- [ ] **Step 3: Write failing env parsing test**

Add this to `tests/test_config.py`:

```python
def test_home_assistant_env_overrides(monkeypatch):
    monkeypatch.setenv("YROBOT_HA_ENABLED", "1")
    monkeypatch.setenv("YROBOT_HA_URL", "http://192.168.1.133:8123/")
    monkeypatch.setenv("YROBOT_HA_TOKEN", "secret-token")
    monkeypatch.setenv("YROBOT_HA_WHITELIST_PATH", "/home/pollen/ha.json")

    settings = Settings.from_env()

    assert settings.ha_enabled is True
    assert settings.ha_url == "http://192.168.1.133:8123"
    assert settings.ha_token == "secret-token"
    assert settings.ha_whitelist_path == "/home/pollen/ha.json"
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_home_assistant_env_overrides -q`

Expected: FAIL with `AttributeError` or assertion failure because env parsing does not exist yet.

- [ ] **Step 5: Implement minimal settings fields**

In `Settings`, add:

```python
    ha_enabled: bool = False
    ha_url: str | None = None
    ha_token: str | None = None
    ha_whitelist_path: str = "~/.config/yrobot/home_assistant_whitelist.json"
```

In `Settings.from_env()`, pass:

```python
            ha_enabled=_flag("YROBOT_HA_ENABLED", False, env),
            ha_url=(env.get("YROBOT_HA_URL") or "").rstrip("/") or None,
            ha_token=env.get("YROBOT_HA_TOKEN") or None,
            ha_whitelist_path=(
                env.get("YROBOT_HA_WHITELIST_PATH")
                or "~/.config/yrobot/home_assistant_whitelist.json"
            ),
```

- [ ] **Step 6: Run config tests**

Run: `pytest tests/test_config.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add yrobot/config.py tests/test_config.py
git commit -m "Add Home Assistant settings"
```

---

### Task 2: Implement Whitelist Matching and Dedupe

**Files:**
- Create: `yrobot/home_assistant.py`
- Test: `tests/test_home_assistant.py`

**Interfaces:**
- Produces: `HomeAssistantAction`, `HomeAssistantResult`, `HomeAssistantController.from_settings(settings)`, `HomeAssistantController.handle_text(text: str, response_id: str) -> HomeAssistantResult | None`
- Consumes: `Settings.ha_enabled`, `Settings.ha_url`, `Settings.ha_token`, `Settings.ha_whitelist_path`

- [ ] **Step 1: Write failing whitelist match test**

Create `tests/test_home_assistant.py`:

```python
import json

from yrobot.config import Settings
from yrobot.home_assistant import HomeAssistantController


def test_whitelist_phrase_matches_one_action(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅灯",
                    "phrases": ["打开客厅灯", "客厅灯打开"],
                    "service": "light.turn_on",
                    "entity_id": "light.living_room",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(settings, caller=lambda action: None)

    result = controller.handle_text("好的，我现在打开客厅灯。", "resp-1")

    assert result is not None
    assert result.action.name == "客厅灯"
    assert result.action.service == "light.turn_on"
    assert result.action.entity_id == "light.living_room"
    assert result.ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_home_assistant.py::test_whitelist_phrase_matches_one_action -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'yrobot.home_assistant'`.

- [ ] **Step 3: Write failing no-op and dedupe tests**

Add:

```python
def test_unknown_text_returns_none(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text("[]", encoding="utf-8")
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(settings, caller=lambda action: None)

    assert controller.handle_text("只是聊天，不控制设备。", "resp-1") is None


def test_duplicate_response_does_not_fire_same_action_twice(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅灯",
                    "phrases": ["打开客厅灯"],
                    "service": "light.turn_on",
                    "entity_id": "light.living_room",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = []
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(settings, caller=calls.append)

    first = controller.handle_text("打开客厅灯", "resp-1")
    second = controller.handle_text("打开客厅灯", "resp-1")

    assert first is not None
    assert second is None
    assert len(calls) == 1
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `pytest tests/test_home_assistant.py -q`

Expected: FAIL because production module does not exist yet.

- [ ] **Step 5: Implement minimal whitelist controller**

Create `yrobot/home_assistant.py` with:

```python
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yrobot.config import Settings

logger = logging.getLogger(__name__)

BLOCKED_DOMAINS = {"lock", "alarm_control_panel", "cover"}
DEFAULT_WHITELIST_PATH = "~/.config/yrobot/home_assistant_whitelist.json"


@dataclass(frozen=True)
class HomeAssistantAction:
    name: str
    phrases: tuple[str, ...]
    service: str
    entity_id: str


@dataclass(frozen=True)
class HomeAssistantResult:
    action: HomeAssistantAction
    ok: bool
    detail: str = ""


def _normalize(text: str) -> str:
    return "".join(text.split()).lower()


def _load_actions(path: str) -> tuple[HomeAssistantAction, ...]:
    source = Path(path).expanduser()
    if not source.exists():
        return ()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Home Assistant whitelist must be a list")
    actions = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Home Assistant whitelist entries must be objects")
        service = str(item["service"])
        domain = service.split(".", 1)[0]
        if domain in BLOCKED_DOMAINS:
            raise ValueError(f"Home Assistant domain is blocked: {domain}")
        phrases = tuple(str(p) for p in item["phrases"] if str(p).strip())
        if not phrases:
            raise ValueError("Home Assistant whitelist entry needs phrases")
        actions.append(
            HomeAssistantAction(
                name=str(item["name"]),
                phrases=phrases,
                service=service,
                entity_id=str(item["entity_id"]),
            )
        )
    return tuple(actions)


class HomeAssistantController:
    def __init__(
        self,
        actions: tuple[HomeAssistantAction, ...],
        caller: Callable[[HomeAssistantAction], None],
        enabled: bool,
    ) -> None:
        self._actions = actions
        self._caller = caller
        self._enabled = enabled
        self._fired: set[tuple[str, str]] = set()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        caller: Callable[[HomeAssistantAction], None] | None = None,
    ) -> "HomeAssistantController":
        enabled = bool(settings.ha_enabled and settings.ha_url and settings.ha_token)
        try:
            actions = _load_actions(settings.ha_whitelist_path) if enabled else ()
        except Exception as exc:
            logger.warning("Home Assistant whitelist disabled: %s", exc)
            actions = ()
            enabled = False
        return cls(actions, caller or (lambda action: None), enabled)

    def handle_text(self, text: str, response_id: str) -> HomeAssistantResult | None:
        if not self._enabled:
            return None
        normalized = _normalize(text)
        matches = [
            action
            for action in self._actions
            if any(_normalize(phrase) in normalized for phrase in action.phrases)
        ]
        if len(matches) != 1:
            return None
        action = matches[0]
        key = (response_id, action.name)
        if key in self._fired:
            return None
        self._fired.add(key)
        try:
            self._caller(action)
        except Exception as exc:
            return HomeAssistantResult(action, False, str(exc))
        return HomeAssistantResult(action, True)
```

- [ ] **Step 6: Run HA matching tests**

Run: `pytest tests/test_home_assistant.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add yrobot/home_assistant.py tests/test_home_assistant.py
git commit -m "Add Home Assistant whitelist matcher"
```

---

### Task 3: Add Home Assistant REST Caller

**Files:**
- Modify: `yrobot/home_assistant.py`
- Test: `tests/test_home_assistant.py`

**Interfaces:**
- Produces: `HomeAssistantClient.call(action: HomeAssistantAction) -> None`
- Consumes: `HomeAssistantAction.service`, `HomeAssistantAction.entity_id`

- [ ] **Step 1: Write failing REST call test**

Add:

```python
from yrobot.home_assistant import HomeAssistantAction, HomeAssistantClient


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return b"{}"


def test_rest_client_sends_expected_home_assistant_request():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    client = HomeAssistantClient("http://ha.local:8123/", "secret-token", opener=opener)
    action = HomeAssistantAction("客厅灯", ("打开客厅灯",), "light.turn_on", "light.living_room")

    client.call(action)

    assert captured["url"] == "http://ha.local:8123/api/services/light/turn_on"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["body"] == b'{"entity_id": "light.living_room"}'
    assert captured["timeout"] == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_home_assistant.py::test_rest_client_sends_expected_home_assistant_request -q`

Expected: FAIL with `ImportError` because `HomeAssistantClient` does not exist.

- [ ] **Step 3: Write failing HTTP failure test**

Add:

```python
def test_controller_converts_rest_failure_to_failed_result(tmp_path):
    path = tmp_path / "ha.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "客厅灯",
                    "phrases": ["打开客厅灯"],
                    "service": "light.turn_on",
                    "entity_id": "light.living_room",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(
        ha_enabled=True,
        ha_url="http://ha.local:8123",
        ha_token="secret",
        ha_whitelist_path=str(path),
    )
    controller = HomeAssistantController.from_settings(
        settings,
        caller=lambda action: (_ for _ in ()).throw(RuntimeError("HA unavailable")),
    )

    result = controller.handle_text("打开客厅灯", "resp-1")

    assert result is not None
    assert result.ok is False
    assert "HA unavailable" in result.detail
```

- [ ] **Step 4: Run test to verify it fails or confirm existing failure mapping**

Run: `pytest tests/test_home_assistant.py::test_controller_converts_rest_failure_to_failed_result -q`

Expected: PASS if Task 2 already catches caller exceptions; otherwise FAIL and fix in Step 6.

- [ ] **Step 5: Implement REST client**

Add imports:

```python
import urllib.error
import urllib.request
```

Add class:

```python
class HomeAssistantClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        opener: Callable = urllib.request.urlopen,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._opener = opener
        self._timeout = timeout

    def call(self, action: HomeAssistantAction) -> None:
        domain, service = action.service.split(".", 1)
        url = f"{self._base_url}/api/services/{domain}/{service}"
        body = json.dumps({"entity_id": action.entity_id}).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        with self._opener(request, timeout=self._timeout) as response:
            response.read()
```

Update `HomeAssistantController.from_settings()` default caller:

```python
        if caller is None and enabled:
            client = HomeAssistantClient(settings.ha_url or "", settings.ha_token or "")
            caller = client.call
        return cls(actions, caller or (lambda action: None), enabled)
```

- [ ] **Step 6: Run HA tests**

Run: `pytest tests/test_home_assistant.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add yrobot/home_assistant.py tests/test_home_assistant.py
git commit -m "Call Home Assistant services from whitelist actions"
```

---

### Task 4: Wire Controller Into Conversation Text Deltas

**Files:**
- Modify: `yrobot/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `HomeAssistantController.from_settings(settings)`, `HomeAssistantController.handle_text(text, response_id)`
- Produces: HA actions are attempted only for allowed assistant text fragments

- [ ] **Step 1: Write failing text-delta integration test**

Add to `tests/test_main.py`:

```python
def test_allowed_assistant_text_is_offered_to_home_assistant_controller():
    calls = []
    conversation = _conversation_without_hardware()

    class FakeHomeAssistant:
        def handle_text(self, text, response_id):
            calls.append((text, response_id))
            return None

    conversation._home_assistant = FakeHomeAssistant()

    conversation._on_delta(Delta(kind="text", text="打开客厅灯", response_id="resp-ha"))

    assert calls == [("打开客厅灯", "resp-ha")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main.py::test_allowed_assistant_text_is_offered_to_home_assistant_controller -q`

Expected: FAIL because `Conversation._on_delta()` does not call `_home_assistant`.

- [ ] **Step 3: Write failing stale/blocked text test**

Add:

```python
def test_blocked_assistant_text_is_not_offered_to_home_assistant_controller():
    calls = []
    conversation = _conversation_without_hardware()

    class FakeHomeAssistant:
        def handle_text(self, text, response_id):
            calls.append((text, response_id))
            return None

    conversation._home_assistant = FakeHomeAssistant()
    started = time.monotonic()
    conversation._begin_barge(started)

    conversation._on_delta(Delta(kind="text", text="打开客厅灯", response_id="old", received_at=started + 0.01))

    assert calls == []
```

- [ ] **Step 4: Run test to verify it fails**

Run: `pytest tests/test_main.py::test_blocked_assistant_text_is_not_offered_to_home_assistant_controller -q`

Expected: The test may PASS already if blocked text produces empty `fragment`; still keep it as regression coverage.

- [ ] **Step 5: Implement integration**

Import:

```python
from yrobot.home_assistant import HomeAssistantController
```

In `Conversation.__init__()` add:

```python
        self._home_assistant = HomeAssistantController.from_settings(settings)
```

In `_on_delta()` text branch, after `caption = fragment.strip()` and inside `if caption:` handling, add:

```python
                result = self._home_assistant.handle_text(caption, delta.response_id or "")
                if result is not None:
                    if result.ok:
                        logger.info("Home Assistant action succeeded: %s", result.action.name)
                    else:
                        logger.warning(
                            "Home Assistant action failed: %s: %s",
                            result.action.name,
                            result.detail,
                        )
```

Keep this inside the `if caption:` block so only gated, visible assistant text can trigger actions.

- [ ] **Step 6: Run main tests**

Run: `pytest tests/test_main.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add yrobot/main.py tests/test_main.py
git commit -m "Wire Home Assistant actions into conversation text"
```

---

### Task 5: Verify Full Suite and Add Operator Setup Notes

**Files:**
- Modify: `README.md`
- Create: optional `home_assistant_whitelist.example.json`

**Interfaces:**
- Consumes: final HA settings and whitelist shape
- Produces: human setup instructions without secrets

- [ ] **Step 1: Write README patch**

Add a short section to `README.md`:

````markdown
## Home Assistant Control

Home Assistant control is disabled by default. Enable it only with a local
whitelist:

```bash
export YROBOT_HA_ENABLED=1
export YROBOT_HA_URL=http://192.168.1.133:8123
export YROBOT_HA_TOKEN=...
export YROBOT_HA_WHITELIST_PATH=~/.config/yrobot/home_assistant_whitelist.json
```

Do not commit the token. The whitelist maps exact phrases to one allowed
Home Assistant service call. Do not include locks, alarm panels, garage doors,
covers, gas, heaters, or other high-risk devices in the first version.
````

- [ ] **Step 2: Create example whitelist if useful**

Create `home_assistant_whitelist.example.json`:

```json
[
  {
    "name": "客厅灯",
    "phrases": ["打开客厅灯", "客厅灯打开"],
    "service": "light.turn_on",
    "entity_id": "light.living_room"
  },
  {
    "name": "客厅灯",
    "phrases": ["关闭客厅灯", "客厅灯关闭"],
    "service": "light.turn_off",
    "entity_id": "light.living_room"
  }
]
```

- [ ] **Step 3: Run full verification**

Run:

```bash
pytest -q
ruff check .
```

Expected: PASS for both commands.

- [ ] **Step 4: Confirm no token leaked**

Run:

```bash
git diff --cached -- . ':!*.md' | grep -i 'secret-token\|YROBOT_HA_TOKEN='
git grep -n 'secret-token\|YROBOT_HA_TOKEN='
```

Expected: No real token values. Test placeholder strings are allowed only inside tests and must not resemble an actual HA token.

- [ ] **Step 5: Commit docs**

```bash
git add README.md home_assistant_whitelist.example.json
git commit -m "Document Home Assistant whitelist setup"
```

---

## Plan Self-Review

- Spec coverage: settings, whitelist, deterministic local execution, REST call, dedupe, no-op behavior, blocked domains, tests, and no-token logging are covered.
- Placeholder scan: no TBD/TODO/fill-in instructions remain; all code-touching steps include concrete snippets or commands.
- Type consistency: `HomeAssistantAction`, `HomeAssistantResult`, `HomeAssistantClient`, and `HomeAssistantController` names are consistent across tasks.
