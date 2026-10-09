from pathlib import Path

UNITS = Path(__file__).resolve().parents[1] / "systemd"


def _read(name):
    return (UNITS / name).read_text(encoding="utf-8")


def test_reflect_service_runs_mem_reflect():
    s = _read("memd-reflect.service")
    assert "ExecStart=" in s
    assert "mem reflect" in s or "memd.reflect" in s
    assert "Type=oneshot" in s


def test_reflect_timer_is_nightly_and_persistent():
    t = _read("memd-reflect.timer")
    assert "OnCalendar=" in t
    assert "Persistent=true" in t
    assert "WantedBy=timers.target" in t


def test_guard_service_follows_the_guard_service_convention():
    g = _read("memd-guard.service")
    assert "ExecStart=/usr/local/bin/memd-guard-loop" in g
    assert "Restart=always" in g
    assert "WantedBy=multi-user.target" in g
    assert "After=memd.service" in g


def test_guard_loop_probes_health_and_treats_upstream_as_degrade():
    loop = _read("memd-guard-loop")
    assert "/health" in loop
    # restarts memd's OWN service on its own failure
    assert "systemctl restart memd.service" in loop or "systemctl --user restart" in loop
    # treats upstream (reranker/embed) 5xx / refused as DEGRADE, not a memd crash
    assert "DEGRADE" in loop or "degrade" in loop
    # fires pushover on recovery
    assert "pushover" in loop
