import importlib
import sys
import types

import pytest


@pytest.fixture()
def main_module(monkeypatch):
    apps_module = types.ModuleType("reachy_mini.apps.app")
    reachy_module = types.ModuleType("reachy_mini.reachy_mini")
    app_config_module = types.ModuleType("yrobot.app_config")

    class ReachyMiniApp:
        def __init__(self, running_on_wireless=False):
            self.settings_app = object()

    class ReachyMini:
        pass

    class _MediaHolder:
        media = None

    def register_settings_routes(settings_app, media_holder=None):
        return None

    apps_module.ReachyMiniApp = ReachyMiniApp
    reachy_module.ReachyMini = ReachyMini
    app_config_module._MediaHolder = _MediaHolder
    app_config_module.audio_input_controller_singleton = object()
    app_config_module.register_settings_routes = register_settings_routes
    monkeypatch.setitem(sys.modules, "reachy_mini", types.ModuleType("reachy_mini"))
    monkeypatch.setitem(sys.modules, "reachy_mini.apps", types.ModuleType("reachy_mini.apps"))
    monkeypatch.setitem(sys.modules, "reachy_mini.apps.app", apps_module)
    monkeypatch.setitem(sys.modules, "reachy_mini.reachy_mini", reachy_module)
    monkeypatch.setitem(sys.modules, "yrobot.app_config", app_config_module)
    sys.modules.pop("yrobot.main", None)
    return importlib.import_module("yrobot.main")


def test_cli_forces_failure_exit_when_wrapped_run_raises(monkeypatch, main_module):
    class FakeApp:
        def __init__(self):
            self.stopped = False

        def wrapped_run(self):
            raise TimeoutError("Status not received in time.")

        def stop(self):
            self.stopped = True

    exits: list[int] = []
    fake_app = FakeApp()
    monkeypatch.setattr(main_module, "Yrobot", lambda: fake_app)
    monkeypatch.setattr(main_module.os, "_exit", exits.append)

    main_module.cli()

    assert fake_app.stopped is True
    assert exits == [1]


def test_cli_keeps_keyboard_interrupt_as_clean_stop(monkeypatch, main_module):
    class FakeApp:
        def __init__(self):
            self.stopped = False

        def wrapped_run(self):
            raise KeyboardInterrupt

        def stop(self):
            self.stopped = True

    fake_app = FakeApp()
    monkeypatch.setattr(main_module, "Yrobot", lambda: fake_app)
    monkeypatch.setattr(main_module.os, "_exit", pytest.fail)

    main_module.cli()

    assert fake_app.stopped is True
