from datetime import datetime

from yrobot.local_info import LocalInfoController


def test_date_marker_returns_current_local_date():
    controller = LocalInfoController(
        enabled=True,
        now=lambda: datetime(2026, 8, 4, 23, 35),
    )

    result = controller.handle_text("当前日期", "resp-date")

    assert result is not None
    assert result.ok is True
    assert result.name == "本地日期"
    assert result.message == "今天是2026年8月4日，星期二。"


def test_time_marker_returns_current_local_time():
    controller = LocalInfoController(
        enabled=True,
        now=lambda: datetime(2026, 8, 4, 23, 35),
    )

    result = controller.handle_text("当前时间", "resp-time")

    assert result is not None
    assert result.ok is True
    assert result.name == "本地时间"
    assert result.message == "现在是23点35分。"


def test_split_marker_matches_after_accumulation():
    controller = LocalInfoController(
        enabled=True,
        now=lambda: datetime(2026, 8, 4, 23, 35),
    )

    assert controller.handle_text("当前", "resp-time") is None
    result = controller.handle_text("时间", "resp-time")

    assert result is not None
    assert result.message == "现在是23点35分。"


def test_duplicate_response_does_not_fire_twice():
    controller = LocalInfoController(
        enabled=True,
        now=lambda: datetime(2026, 8, 4, 23, 35),
    )

    assert controller.handle_text("当前时间", "resp-time") is not None
    assert controller.handle_text("当前时间", "resp-time") is None


def test_disabled_local_info_ignores_text():
    controller = LocalInfoController(enabled=False)

    assert controller.handle_text("当前时间", "resp-time") is None


def test_plain_today_statement_does_not_trigger_date():
    controller = LocalInfoController(
        enabled=True,
        now=lambda: datetime(2026, 8, 4, 23, 35),
    )

    assert controller.handle_text("今天是个好日子", "resp-chat") is None
