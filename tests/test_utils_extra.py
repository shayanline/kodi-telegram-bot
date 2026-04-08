import utils


def test_humanize_size_basic():
    assert utils.humanize_size(0) == "0 B"
    assert utils.humanize_size(1024) == "1.0 KB"


def test_humanize_size_large_caps():
    # Larger than TB should still not crash and report TB
    val = 1024**6  # beyond defined units
    result = utils.humanize_size(val)
    assert result.endswith("TB")
    assert float(result.split()[0]) > 0


def test_memory_warning_message_disabled(monkeypatch):
    assert utils.memory_warning_message(0) is None
    assert utils.memory_warning_message(-5) is None


def test_memory_warning_message_below_threshold(monkeypatch):
    monkeypatch.setattr("psutil.virtual_memory", lambda: type("M", (), {"percent": 50.0})())
    assert utils.memory_warning_message(90) is None


def test_memory_warning_message_above_threshold(monkeypatch):
    monkeypatch.setattr("psutil.virtual_memory", lambda: type("M", (), {"percent": 95.3})())
    result = utils.memory_warning_message(90)
    assert result is not None
    assert "95%" in result
    assert "90%" in result


def test_memory_warning_message_psutil_error(monkeypatch):
    def _boom():
        raise RuntimeError("no psutil")

    monkeypatch.setattr("psutil.virtual_memory", _boom)
    assert utils.memory_warning_message(90) is None
