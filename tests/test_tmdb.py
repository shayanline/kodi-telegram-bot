import asyncio
from unittest.mock import MagicMock, patch

import config
import tmdb


def test_search_sync_returns_id(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-key")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": [{"id": 4057}]}
    with patch("tmdb.requests.get", return_value=resp) as mock_get:
        result = tmdb._search_sync("Criminal Minds", 2005)
    assert result == 4057
    mock_get.assert_called_once()
    _args, kwargs = mock_get.call_args
    assert kwargs["params"]["query"] == "Criminal Minds"
    assert kwargs["params"]["first_air_date_year"] == 2005


def test_search_sync_no_results(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-key")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": []}
    with patch("tmdb.requests.get", return_value=resp):
        assert tmdb._search_sync("Nonexistent Show", None) is None


def test_search_sync_no_api_key(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    assert tmdb._search_sync("Anything", None) is None


def test_search_sync_network_error(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-key")
    with patch("tmdb.requests.get", side_effect=ConnectionError("offline")):
        assert tmdb._search_sync("Show", None) is None


def test_search_sync_non_200(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-key")
    resp = MagicMock()
    resp.status_code = 401
    with patch("tmdb.requests.get", return_value=resp):
        assert tmdb._search_sync("Show", None) is None


def test_search_show_async(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-key")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": [{"id": 66636}]}
    with patch("tmdb.requests.get", return_value=resp):
        result = asyncio.run(tmdb.search_show("Love Island"))
    assert result == 66636


def test_search_sync_without_year(monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "fake-key")
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": [{"id": 99}]}
    with patch("tmdb.requests.get", return_value=resp) as mock_get:
        tmdb._search_sync("Show", None)
    assert "first_air_date_year" not in mock_get.call_args.kwargs["params"]
