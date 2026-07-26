"""
Wave 5f — structured stat collectors for the dev dashboard (data layer).
Run with: python -m pytest tests/test_devstats.py -v
"""

from kai.system import devstats


def test_collect_disk_shape():
    disks = devstats.collect_disk()
    assert isinstance(disks, list)
    assert disks, "expected at least one mounted partition (e.g. /)"
    d = disks[0]
    for k in ("mount", "fstype", "total_gb", "used_gb", "free_gb", "percent"):
        assert k in d
    assert isinstance(d["total_gb"], (int, float))
    assert 0 <= d["percent"] <= 100
    # Sorted largest-first.
    assert disks == sorted(disks, key=lambda x: x["total_gb"], reverse=True)


def test_collect_network_shape():
    nets = devstats.collect_network()
    assert isinstance(nets, list)
    for n in nets:
        assert set(n) == {"name", "up", "speed_mbps", "addresses"}
        assert isinstance(n["addresses"], list)
        assert n["name"] != "lo"  # loopback excluded


def test_collect_temps_shape():
    t = devstats.collect_temps()
    assert set(t) == {"cpu", "gpu"}
    assert set(t["cpu"]) == {"load_pct", "clock_mhz", "temp_c"}
    assert set(t["gpu"]) == {"temp_c"}
    # Values are either None or numeric — never raise, never strings.
    for v in (t["cpu"]["temp_c"], t["cpu"]["clock_mhz"], t["gpu"]["temp_c"]):
        assert v is None or isinstance(v, (int, float))


def test_collectors_never_raise():
    # Defensive contract: a dashboard poll must not blow up on odd hardware.
    devstats.collect_disk()
    devstats.collect_network()
    devstats.collect_temps()
