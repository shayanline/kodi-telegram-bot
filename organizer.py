"""Media file categorization & path builder.

Environment driven (see config) lightweight organizer that:
 - Classifies filename as movie / series / other.
 - Normalizes title tokens (replace dots/underscores, trim junk tags).
 - Builds human friendly final path under configured subdirectories.

Parsing heuristics are intentionally simple + deterministic (no network).
If classification is ambiguous caller can ask user; this module only provides
pure functions so it's easy to test.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

import config

_YEAR_RE = re.compile(r"^(19\d{2}|20\d{2}|21\d{2})$")  # conservative upper bound

# Core patterns we will try in order for series tokens
_PAT_SXXEYY_MULTI = re.compile(r"^[Ss]([0O]?\d{1,2})E(\d{2})(?:E\d{2}|-\d{2})+$")  # S02E05E06 / S02E05-06 (take first)
_PAT_SXXEYY = re.compile(r"^[Ss]([0O]?\d{1,2})E(\d{1,2})$")  # S02E5 or S2E05 variants
_PAT_SEP_X = re.compile(r"^(\d{1,2})[xX](\d{1,2})$")  # 1x05 / 2x5
_PAT_NUM3 = re.compile(r"^(\d)(\d{2})$")  # 205 => S02E05 heuristic (avoid years)


# Accept tokens like SO4E24 (letter O as 0) by normalizing O->0 in season part.
def _normalize_season_digits(raw: str) -> str:
    return raw.upper().replace("O", "0")


_MULTI_EP_RE = re.compile(r"[Ss]([0O]?\d{1,2})((?:E\d{2,3})+(?:-\d{2,3})?)", re.IGNORECASE)


def _normalize_multi_ep_tag(tok: str) -> str:
    """Normalize S02E05E06 / S2E05-06 into a canonical Sxx... form."""
    m = _MULTI_EP_RE.match(tok)
    if not m:
        return tok.upper()
    season = int(_normalize_season_digits(m.group(1)))
    return f"S{season:02d}{m.group(2).upper()}"


# Common technical / quality / group tokens (lower-case) stripped from titles
_JUNK = {
    # Resolutions / quality
    "1080p",
    "720p",
    "480p",
    "2160p",
    "1440p",
    "360p",
    "4k",
    "8k",
    "10bit",
    "uhd",
    "hdr",
    "hdr10",
    "hdr10plus",
    "dv",
    "dovi",
    "sdr",
    # Sources / codecs
    "webrip",
    "web",
    "web-dl",
    "bluray",
    "brrip",
    "hdrip",
    "dvdrip",
    "hdtv",
    "x264",
    "x265",
    "h264",
    "h265",
    "avc",
    "hevc",
    "xvid",
    "remux",
    # Audio / channels
    "aac",
    "dd5",
    "dd5.1",
    "ddp5",
    "ddp5.1",
    "dts",
    "atmos",
    "truehd",
    "ac3",
    "mp3",
    "flac",
    "6ch",
    "8ch",
    # Release / language / misc tags
    "multi",
    "farsi",
    "dubbed",
    "dual",
    "audio",
    "subs",
    "esubs",
    "hc",
    "proper",
    "repack",
    "internal",
    "cut",
    "uncut",
    "colorized",
    # Groups / common scene names
    "pahe",
    "yts",
    "rarbg",
    "galaxyrg",
    "alphadl",
    "lama",
    "psa",
    "ntb",
    "evo",
    "tgx",
    "fg0",
    "geckos",
    "cmrg",
    "amzn",
    # Misc ephemeral
    "sample",
    "ad",
}

# Edition tokens we might *optionally* keep to append after title (normalized form)
_EDITION_KEEP = {"extended", "remastered", "unrated", "imax", "directors", "director", "ultimate"}


@dataclass(slots=True)
class ParsedMedia:
    category: str  # movie|series|other|unknown
    title: str  # movie title or series episode show title
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    # Final normalized filename (without directory) suggestion (no extension)
    normalized_stem: str | None = None
    edition: str | None = None  # e.g. Extended, Remastered (movies only currently)


def _tokenize(name: str) -> list[str]:
    # Unify separators (dots, underscores, whitespace) into a single dot then split
    name = re.sub(r"[._\s]+", ".", name)
    name = name.strip(".")
    return [t for t in name.split(".") if t]


def _clean_tokens(tokens: Iterable[str]) -> list[str]:
    out: list[str] = []
    for t in tokens:
        low = t.lower()
        if low in _JUNK:
            continue
        if re.fullmatch(r"\d+ch", low):
            continue
        # Strip obvious release group token if it's the very last and made only of letters/numbers (common pattern)
        out.append(t)
    return out


def _norm_word(w: str) -> str:
    if not w:
        return w
    # Preserve short all-caps tokens as potential acronyms (FBI, NCIS, HBO)
    if w.isupper() and len(w) <= 4:
        return w
    return w.capitalize()


def _build_title(tokens: list[str]) -> str:
    # When ALL alpha tokens are uppercase the filename is just all-caps (e.g.
    # THE.BIG.SHORT) — capitalize every word uniformly instead of preserving
    # short tokens like "THE" or "BIG" which aren't real acronyms.
    alpha = [t for t in tokens if t.isalpha()]
    all_upper = len(alpha) > 1 and all(t.isupper() for t in alpha)
    if all_upper:
        return re.sub(r"\s+", " ", " ".join(t.capitalize() for t in tokens).strip())
    return re.sub(r"\s+", " ", " ".join(_norm_word(t) for t in tokens).strip())


def _detect_year(tokens: list[str]) -> tuple[int | None, int]:
    for i, tok in enumerate(tokens):
        if _YEAR_RE.match(tok):
            return int(tok), i
    return None, -1


def _detect_series(tokens: list[str]) -> tuple[int | None, int | None, int, str | None]:
    """Return (season, episode, token_index, ep_tag).

    ep_tag is the normalized SxxEyy(Ezz) string to embed in the filename.
    For multi-episode tokens the full tag (e.g. S02E05E06) is preserved so
    Kodi can associate the file with every episode in the range.
    """
    for i, tok in enumerate(tokens):
        if _PAT_SXXEYY_MULTI.match(tok):
            m = _PAT_SXXEYY.match(tok[: tok.upper().find("E", 2) + 3])
            if m:
                season = int(_normalize_season_digits(m.group(1)))
                episode = int(m.group(2))
                tag = _normalize_multi_ep_tag(tok)
                return season, episode, i, tag
        m = _PAT_SXXEYY.match(tok)
        if m:
            season = int(_normalize_season_digits(m.group(1)))
            episode = int(m.group(2))
            return season, episode, i, None
        mx = _PAT_SEP_X.match(tok)
        if mx:
            season = int(mx.group(1))
            episode = int(mx.group(2))
            return season, episode, i, None
        m3 = _PAT_NUM3.match(tok)
        if m3:
            season = int(m3.group(1))
            episode = int(m3.group(2))
            if episode < 60:  # heuristic safety
                return season, episode, i, None
    return None, None, -1, None


def _extract_edition(tokens: list[str]) -> tuple[str | None, list[str]]:
    # Look for edition keywords near the end (before junk removal) e.g. Extended, Remastered
    edition_tokens: list[str] = []
    remaining = []
    for t in tokens:
        low = t.lower()
        if low in _EDITION_KEEP:
            # Avoid duplicates / combine consecutive
            if low not in (et.lower() for et in edition_tokens):
                edition_tokens.append(t)
            continue
        remaining.append(t)
    edition = None
    if edition_tokens:
        # Normalize ordering (Extended Remastered etc.)
        edition = " ".join(_norm_word(e) for e in edition_tokens)
    return edition, remaining


_MOVIE_LINE_RE = re.compile(r"^🎬\s+(.+?)\s*\((\d{4})\)\s*$")
_SERIES_HEADER_RE = re.compile(r"^🎬\s+سریال\s+(.+?)\s+محصول سال\s+(\d{4})\s*$")
_SERIES_EP_RE = re.compile(r"^📁\s+فصل\s+(\d{1,2})\s+قسمت\s+(\d{1,3})\s*$")


def _parse_caption(text: str | None) -> ParsedMedia | None:
    """Best-effort extraction from known caption templates.

    Only returns a ParsedMedia when patterns *strictly* match the provided
    (movie / series) examples to avoid false positives. Otherwise returns None
    so caller can fall back to filename parsing.
    """
    if not text:
        return None
    # Normalize newlines + trim whitespace lines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    # Movie pattern: requires first line 🎬 Title (Year) and at least one more line starting with 🖥
    m = _MOVIE_LINE_RE.match(lines[0])
    if m and any(ln.startswith("🖥") for ln in lines[1:3]):  # keep it tight (next couple of lines)
        title = m.group(1).strip()
        year = int(m.group(2))
        norm_stem = f"{title} ({year})"
        return ParsedMedia("movie", title, year, None, None, norm_stem, None)
    # Series pattern requires header + episode line
    sh = _SERIES_HEADER_RE.match(lines[0])
    if sh:
        title = sh.group(1).strip()
        year = int(sh.group(2))
        # find episode line (usually second non-empty)
        ep_line = next((ln for ln in lines[1:4] if ln.startswith("📁")), None)
        if ep_line:
            epm = _SERIES_EP_RE.match(ep_line)
            if epm:
                season = int(epm.group(1))
                episode = int(epm.group(2))
                norm_stem = f"{title} S{season:02d}E{episode:02d}"
                return ParsedMedia("series", title, year, season, episode, norm_stem, None)
    return None


def _parse_from_tokens(tokens: list[str]) -> ParsedMedia:
    """Filename token heuristic parsing (legacy path)."""
    year, year_index = _detect_year(tokens)
    season, episode, series_index, ep_tag = _detect_series(tokens)
    edition, base_tokens = _extract_edition(tokens)
    tokens = base_tokens
    if season is not None or episode is not None:
        season, episode, series_index, ep_tag = _detect_series(tokens)
    if year is not None:
        year, year_index = _detect_year(tokens)
    if series_index != -1 and season is not None and episode is not None:
        show_tokens = tokens[:series_index]
        show_year = None
        if year_index != -1 and year_index < series_index:
            show_year = year
            show_tokens = [t for i, t in enumerate(show_tokens) if i != year_index]
        cleaned = _clean_tokens(show_tokens)
        title = _build_title(cleaned or show_tokens)
        suffix = ep_tag or f"S{season:02d}E{episode:02d}"
        norm_stem = f"{title} {suffix}"
        return ParsedMedia("series", title, show_year, season, episode, norm_stem, edition)
    if year is not None and year_index > 0:
        title_tokens = tokens[:year_index]
        cleaned = _clean_tokens(title_tokens)
        base_title = _build_title(cleaned or title_tokens)
        title = base_title
        norm_title = base_title
        if edition:
            title = f"{base_title} {edition}"
            norm_title = base_title
        norm_stem = f"{norm_title} ({year})"
        return ParsedMedia("movie", title, year, None, None, norm_stem, edition)
    cleaned_all = _clean_tokens(tokens)
    title = _build_title(cleaned_all or tokens)
    return ParsedMedia("other", title, None, None, None, None, edition)


def parse_filename(filename: str, text: str | None = None) -> ParsedMedia:
    # Caption path
    parsed_caption = _parse_caption(text)
    if parsed_caption:
        return parsed_caption
    stem, _ext = os.path.splitext(filename)
    stem = re.sub(r"_(\d+)$", "", stem)
    tokens = _tokenize(stem)
    if not tokens:
        return ParsedMedia("unknown", filename)
    return _parse_from_tokens(tokens)


def build_final_path(
    filename: str, base_dir: str | None = None, forced_category: str | None = None, text: str | None = None
) -> tuple[str, str]:
    """Return (final_path, final_filename).

    If organization disabled returns original path/filename.
    """
    base_dir = base_dir or config.DOWNLOAD_DIR
    if not config.ORGANIZE_MEDIA:
        return os.path.join(base_dir, filename), filename

    parsed = parse_filename(filename, text=text)
    if forced_category and forced_category in {"movie", "series", "other"}:
        parsed.category = forced_category
        # Synthesize minimal normalized stem if missing for movie/series
        if forced_category == "movie" and not parsed.normalized_stem:
            base_title = parsed.title or os.path.splitext(filename)[0]
            parsed.normalized_stem = f"{base_title}"
        if forced_category == "series" and not parsed.normalized_stem:
            base_title = parsed.title or os.path.splitext(filename)[0]
            parsed.normalized_stem = f"{base_title} S01E01"
            parsed.season = parsed.season or 1
            parsed.episode = parsed.episode or 1
    ext = os.path.splitext(filename)[1]

    if parsed.category == "movie" and parsed.normalized_stem:
        movies_root = os.path.join(base_dir, config.MOVIES_DIR_NAME)
        folder = f"{parsed.normalized_stem}"  # stem already '(Year)'
        final_dir = os.path.join(movies_root, folder)
        os.makedirs(final_dir, exist_ok=True)
        final_name = f"{parsed.normalized_stem}{ext}"
        return os.path.join(final_dir, final_name), final_name
    if parsed.category == "series" and parsed.normalized_stem:
        series_root = os.path.join(base_dir, config.SERIES_DIR_NAME)
        show_folder = parsed.title if not parsed.year else f"{parsed.title} ({parsed.year})"
        season_folder = f"Season {parsed.season}" if parsed.season else "Season 1"
        final_dir = os.path.join(series_root, show_folder, season_folder)
        os.makedirs(final_dir, exist_ok=True)
        final_name = f"{parsed.normalized_stem}{ext}"
        return os.path.join(final_dir, final_name), final_name

    # OTHER / unknown: place under OTHER dir (if enabled) else root
    other_root = os.path.join(base_dir, config.OTHER_DIR_NAME)
    os.makedirs(other_root, exist_ok=True)
    return os.path.join(other_root, filename), filename


__all__ = [
    "ParsedMedia",
    "build_final_path",
    "parse_filename",
]
