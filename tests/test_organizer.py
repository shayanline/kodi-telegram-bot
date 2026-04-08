import os
import tempfile

import config
from organizer import build_final_path, parse_filename


def test_parse_movie_basic():
    p = parse_filename("Bullet.Train.2022.1080p.BluRay.mkv")
    assert p.category == "movie"
    assert p.title == "Bullet Train"
    assert p.year == 2022
    assert p.normalized_stem == "Bullet Train (2022)"


def test_parse_movie_with_edition_and_group():
    p = parse_filename("The.Matrix.1999.1080p.Remastered.BluRay.x265.Group.mkv")
    assert p.category == "movie"
    assert p.year == 1999
    assert "Matrix" in p.title
    assert p.normalized_stem == "The Matrix (1999)"


def test_parse_series_alt_formats():
    # 1x05 pattern
    p = parse_filename("Show.Name.1x05.1080p.WEB-DL.mkv")
    assert p.category == "series" and p.season == 1 and p.episode == 5
    # 205 numeric pattern
    p2 = parse_filename("Show.Name.205.720p.HDTV.mkv")
    assert p2.category == "series" and p2.season == 2 and p2.episode == 5
    # multi-episode pattern preserves full range
    p3 = parse_filename("Show.Name.S02E05E06.1080p.WEB.mkv")
    assert p3.category == "series" and p3.season == 2 and p3.episode == 5
    assert "S02E05E06" in p3.normalized_stem


def test_parse_series_weird_season_token():
    p = parse_filename("The.Mentalist.SO4E24.720p.WEB-DL.mkv")
    assert p.category == "series"
    assert p.season == 4 and p.episode == 24
    assert p.title == "The Mentalist"
    assert p.normalized_stem.startswith("The Mentalist S04E24")


def test_parse_from_caption_movie():
    caption = """🎬 Ballerina (2025)\n🖥 BluRay 1080p YTS\n\n🤖 | @alphadlbot"""
    p = parse_filename("Some.Random.File.mkv", text=caption)
    assert p.category == "movie" and p.title == "Ballerina" and p.year == 2025
    assert p.normalized_stem == "Ballerina (2025)"


def test_parse_from_caption_series():
    caption = "🎬 سریال Rick and Morty محصول سال 2013\n\n📁 فصل 01 قسمت 05\n📥 کیفیت: BluRay 1080p Farsi Dubbed"
    p = parse_filename("Rick.and.Morty.random.mkv", text=caption)
    assert p.category == "series" and p.title == "Rick and Morty" and p.season == 1 and p.episode == 5
    assert p.year == 2013


def test_build_path_from_caption_series(monkeypatch):
    import tempfile

    import config as cfg

    caption = "🎬 سریال Rick and Morty محصول سال 2013\n\n📁 فصل 01 قسمت 05\n📥 کیفیت: BluRay 1080p Farsi Dubbed"
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(cfg, "DOWNLOAD_DIR", td)
        monkeypatch.setattr(cfg, "ORGANIZE_MEDIA", True)
        path, fname = build_final_path("Rick.And.Morty.S01E05.mkv", base_dir=td, text=caption)
        assert "(2013)" in path  # year folder used
        assert fname.startswith("Rick and Morty S01E05")


def test_build_final_path_movie(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DOWNLOAD_DIR", td)
        monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
        path, fname = build_final_path("Finch.2021.1080p.WEB-DL.mkv", base_dir=td)
        assert fname.startswith("Finch (2021)")
        assert path.endswith(f"{config.MOVIES_DIR_NAME}/Finch (2021)/Finch (2021).mkv")
        assert os.path.exists(os.path.dirname(path))


def test_build_final_path_series(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DOWNLOAD_DIR", td)
        monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
        path, fname = build_final_path("The.Mentalist.S02E06.720p.WEB-DL.mkv", base_dir=td)
        assert "Season 2" in path
        assert fname.startswith("The Mentalist S02E06")
        assert os.path.exists(os.path.dirname(path))


def test_build_final_path_other(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DOWNLOAD_DIR", td)
        monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
        path, fname = build_final_path("Random.File.Without.Year.mkv", base_dir=td)
        assert fname == "Random.File.Without.Year.mkv"  # unchanged
        assert path.startswith(os.path.join(td, config.OTHER_DIR_NAME))


def test_forced_category_override(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DOWNLOAD_DIR", td)
        monkeypatch.setattr(config, "ORGANIZE_MEDIA", True)
        # Ambiguous: no year but force movie
    path, _ = build_final_path("Some.Random.Name.mkv", base_dir=td, forced_category="movie")
    assert config.MOVIES_DIR_NAME in path


def test_parse_movie_space_separated():
    p = parse_filename("Bullet Train 2022 1080p BluRay.mkv")
    assert p.category == "movie"
    assert p.title == "Bullet Train"
    assert p.year == 2022
    assert p.normalized_stem == "Bullet Train (2022)"


def test_parse_series_space_separated():
    p = parse_filename("The Mentalist S02E06 720p WEB-DL.mkv")
    assert p.category == "series"
    assert p.season == 2 and p.episode == 6
    assert p.title == "The Mentalist"
    assert p.normalized_stem == "The Mentalist S02E06"


def test_multi_episode_preserved_in_stem():
    p = parse_filename("Breaking.Bad.S05E15E16.720p.mkv")
    assert p.category == "series"
    assert p.season == 5 and p.episode == 15
    assert p.normalized_stem == "Breaking Bad S05E15E16"


def test_multi_episode_dash_range():
    p = parse_filename("Show.S01E01-02.720p.mkv")
    assert p.category == "series"
    assert p.season == 1 and p.episode == 1
    assert "S01E01" in p.normalized_stem


def test_norm_word_stopwords_capitalized():
    p = parse_filename("THE.BIG.SHORT.2015.1080p.mkv")
    assert p.category == "movie"
    assert p.title == "The Big Short"
    assert p.normalized_stem == "The Big Short (2015)"


def test_norm_word_preserves_acronyms():
    p = parse_filename("FBI.S01E01.720p.mkv")
    assert p.category == "series"
    assert p.title == "FBI"
    assert p.normalized_stem == "FBI S01E01"


def test_edition_not_in_junk():
    """Edition tokens (extended, unrated, imax) should be extracted, not junked."""
    p = parse_filename("Aliens.1986.Extended.1080p.mkv")
    assert p.category == "movie"
    assert p.edition == "Extended"
    assert p.normalized_stem == "Aliens (1986)"
