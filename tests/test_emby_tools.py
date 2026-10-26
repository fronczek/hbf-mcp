"""Offline tests for bounded, multi-instance Emby MCP tools."""
from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

import emby_tools as emby


class Response:
    status = 200

    def __init__(self, payload):
        self._data = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, length):
        return self._data.read(length)


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    directory = tmp_path / ".credentials"
    directory.mkdir()
    monkeypatch.setattr(emby, "CREDENTIAL_DIR", directory)
    return directory


def config(directory, name, token, host="emby.example.org"):
    (directory / name).write_text(
        "emby_internal: http://172.16.10.33:8096/\n"
        f"emby_external: https://{host}\n"
        f"emby_token: {token}\n",
        encoding="utf-8",
    )


def test_one_instance_auto_selected(credentials):
    config(credentials, "emby-ds1522.yml", "test-token-ds1522-123456789")
    assert emby.list_instances() == {"instances": ["ds1522"], "count": 1}
    with patch.object(emby._OPENER, "open", return_value=Response({"ServerName": "DS1522"})) as opener:
        result = emby.server_status()
    assert result["instance"] == "ds1522"
    request = opener.call_args.args[0]
    assert request.get_method() == "GET"
    assert request.get_header("X-emby-token") == "test-token-ds1522-123456789"
    assert request.full_url == "http://172.16.10.33:8096/System/Info"


def test_multiple_require_explicit_instance(credentials):
    config(credentials, "emby-ds1522.yml", "test-token-ds1522-123456789")
    config(credentials, "emby-bedroom.yml", "test-token-bedroom-123456789", "other.example.org")
    assert emby.list_instances() == {"instances": ["bedroom", "ds1522"], "count": 2}
    with pytest.raises(ValueError, match="specify instance"):
        emby.server_status()
    with patch.object(emby._OPENER, "open", return_value=Response({"MovieCount": 5})) as opener:
        result = emby.library_counts(instance="bedroom", target="external")
    assert result == {
        "instance": "bedroom", "MovieCount": 5,
        "SeriesCount": None, "EpisodeCount": None, "ItemCount": None,
    }
    assert opener.call_args.args[0].full_url == "https://other.example.org/Items/Counts"
    assert opener.call_args.args[0].get_header("X-emby-token") == "test-token-bedroom-123456789"


def test_emby_yml_is_default_and_txt_not_discovered(credentials):
    config(credentials, "emby.yml", "test-token-default-123456789")
    (credentials / "emby-imdb.txt").write_text("ignored")
    (credentials / "emby-invalid!.yml").write_text("ignored")
    (credentials / "emby-orphan.yml").symlink_to(credentials / "emby.yml")
    assert emby.list_instances() == {"instances": ["default"], "count": 1}


@pytest.mark.parametrize("instance", [
    "../ds1522", "/tmp/test", "missing", "", "ds1522.yml", "x" * 100,
])
def test_rejects_invalid_instance_and_path_traversal(credentials, instance):
    config(credentials, "emby-ds1522.yml", "test-token-ds1522-123456789")
    with patch.object(emby._OPENER, "open") as opener:
        with pytest.raises(ValueError):
            emby.server_status(instance=instance)
        opener.assert_not_called()


def test_duplicate_alias_rejected(credentials):
    config(credentials, "emby-ds1522.yml", "test-token-ds1522-123456789")
    config(credentials, "embyds1522.yml", "test-token-duplicate-123456789")
    with pytest.raises(RuntimeError, match="Duplicate"):
        emby.list_instances()


def test_no_instances_raises(credentials):
    assert emby.list_instances() == {"instances": [], "count": 0}
    with pytest.raises(RuntimeError, match="No Emby instances"):
        emby.server_status()


def test_movie_list_has_filters_and_instance(credentials):
    config(credentials, "emby-cinema.yml", "test-token-cinema-123456789")
    payload = {"TotalRecordCount": 1, "Items": [{
        "Id": "11", "Name": "Cinema", "Path": "/films/test.mkv",
        "ProviderIds": {"Imdb": "tt123"}, "Extra": "not returned",
    }]}
    with patch.object(emby._OPENER, "open", return_value=Response(payload)) as opener:
        result = emby.movies(start_index=10, limit=25, search_term="Some & movie",
                             year=1977, missing_imdb_only=True, instance="cinema")
    from urllib.parse import parse_qs, urlsplit
    query = parse_qs(urlsplit(opener.call_args.args[0].full_url).query)
    assert query["SearchTerm"] == ["Some & movie"]
    assert query["Limit"] == ["25"]
    assert query["StartIndex"] == ["10"]
    assert query["Years"] == ["1977"]
    assert query["HasImdbId"] == ["false"]
    assert result["instance"] == "cinema"
    assert result["items"][0]["has_imdb_id"] is True
    assert "Extra" not in result["items"][0]


@pytest.mark.parametrize("kwargs", [
    {"limit": 101}, {"limit": 0}, {"limit": True},
    {"start_index": -1}, {"year": 1800},
    {"search_term": "\n"}, {"search_term": "a" * 121},
    {"missing_imdb_only": 1},
])
def test_movie_limits_rejected(credentials, kwargs):
    config(credentials, "emby.yml", "test-token-default-123456789")
    with patch.object(emby._OPENER, "open") as opener:
        with pytest.raises(ValueError):
            emby.movies(**kwargs)
        opener.assert_not_called()


def test_item_id_rejects_path_and_mismatch(credentials):
    config(credentials, "emby.yml", "test-token-default-123456789")
    with pytest.raises(ValueError):
        emby.movie_details("../etc/passwd")
    with patch.object(emby._OPENER, "open",
                      return_value=Response({"Items": [{"Id": "other"}]})):
        with pytest.raises(RuntimeError, match="unexpected movie"):
            emby.movie_details("123")


def test_network_failures_are_safe(credentials):
    config(credentials, "emby.yml", "test-token-default-123456789")
    with patch.object(emby._OPENER, "open", side_effect=urllib.error.URLError("dummy")):
        with pytest.raises(RuntimeError, match="unavailable") as err:
            emby.server_status()
    assert "test-token-default-123456789" not in str(err.value)


def test_redirect_handler_does_not_follow():
    assert emby._NoRedirect().redirect_request(
        None, None, 302, "redirect", {}, "https://other.example.org",
    ) is None


def test_readonly_tools_registered():
    from mcp.server import MCPServer
    mcp = MCPServer("Emby Tests")
    emby.register_tools(mcp)
    tools = asyncio.run(mcp.list_tools())
    assert {tool.name for tool in tools} == {
        "emby_instances", "emby_status", "emby_library_counts",
        "emby_movies", "emby_movie_details",
    }
    for tool in tools:
        assert tool.annotations.read_only_hint
        assert not tool.annotations.destructive_hint

def test_underscore_filename_is_a_valid_instance(credentials):
    config(credentials, 'emby_salon.yml', 'test-token-salon-123456789')
    assert emby.list_instances() == {'instances': ['salon'], 'count': 1}
    with patch.object(emby._OPENER, 'open', return_value=Response({'ServerName': 'Salon'})):
        result = emby.server_status(instance='salon')
    assert result['instance'] == 'salon'
