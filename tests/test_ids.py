from downloader.ids import get_file_id


def test_get_file_id_stability_and_length():
    a1 = get_file_id("example.mp4")
    a2 = get_file_id("example.mp4")
    b = get_file_id("different.mp4")
    assert a1 == a2  # deterministic
    assert a1 != b  # extremely high probability
    assert len(a1) == 8 and all(c in "0123456789abcdef" for c in a1)
