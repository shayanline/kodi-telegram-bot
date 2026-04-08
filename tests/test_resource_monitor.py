import utils


def test_memory_warning(monkeypatch):
    # Reset internal timer
    utils._last_mem_warn = 0

    class VM:  # simple structure to mimic psutil result
        def __init__(self, percent):
            self.percent = percent

    seq = [85, 95, 95]  # first below threshold -> False, then above -> True, then rate limited -> False

    def fake_vm():
        return VM(seq.pop(0))

    monkeypatch.setattr(utils.psutil, "virtual_memory", fake_vm)
    assert utils.maybe_memory_warning(90) is False
    assert utils.maybe_memory_warning(90) is True
    # Third call within 60s should be suppressed
    assert utils.maybe_memory_warning(90) is False


def test_memory_warning_disabled():
    # threshold 0 disables
    assert utils.maybe_memory_warning(0) is False
