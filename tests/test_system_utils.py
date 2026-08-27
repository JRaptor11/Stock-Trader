from collections import deque

from utils import system_utils


class _Memory:
    rss = 512 * system_utils.MB
    vms = 700 * system_utils.MB


class _Process:
    def memory_info(self):
        return _Memory()


def test_resource_snapshot_is_json_safe(monkeypatch):
    class _Buffer:
        def snapshot(self):
            return {"AMD": {"tick_count": 20, "live_1m_bar_count": 10, "live_5m_bar_count": 2}}

    monkeypatch.setitem(system_utils.app_state, "market_data", {"buffer": _Buffer()})
    monkeypatch.setitem(
        system_utils.app_state,
        "layers",
        {"execution_plan_history": deque(maxlen=10)},
    )

    result = system_utils.resource_snapshot(_Process())

    assert result["rss_mb"] == 512.0
    assert result["execution_plan_history_count"] == 0
    assert result["market_data"]["AMD"]["live_1m_bars"] == 10


def test_trim_process_memory_always_collects(monkeypatch):
    calls = []
    monkeypatch.setattr(system_utils.gc, "collect", lambda: calls.append(True))

    system_utils._trim_process_memory()

    assert calls == [True]
