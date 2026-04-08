import utils


def test_humanize_size_basic():
    assert utils.humanize_size(0) == "0B"
    assert utils.humanize_size(1024) == "1.0 KB"


def test_humanize_size_large_caps():
    # Larger than TB should still not crash and report TB
    val = 1024**6  # beyond defined units
    result = utils.humanize_size(val)
    assert result.endswith("TB")
    assert float(result.split()[0]) > 0
