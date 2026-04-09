import utils


def test_remove_empty_parents(tmp_path):
    base = tmp_path / "root"
    target_dir = base / "a" / "b" / "c"
    target_dir.mkdir(parents=True)
    f = target_dir / "file.bin"
    f.write_bytes(b"data")
    # Remove file then cleanup parents stopping at base
    f.unlink()
    removed = utils.remove_empty_parents(str(f), [str(base)])
    # a,b,c removed (3 levels)
    assert removed == 3
    assert base.exists()
