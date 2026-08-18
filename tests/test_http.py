"""The shared HTTP client: error taxonomy, caching and pacing."""

from __future__ import annotations

import time

import pytest
import responses

from miami_bot.util.http import (
    AuthError,
    HttpClient,
    HttpError,
    NotFound,
    RateLimited,
)


@responses.activate
def test_json_decoding():
    responses.add(responses.GET, "https://api.test/x", json={"ok": True}, status=200)
    assert HttpClient(rate_limit_seconds=0).get_json("https://api.test/x") == {"ok": True}


@responses.activate
@pytest.mark.parametrize(
    "status,exception",
    [(401, AuthError), (403, AuthError), (404, NotFound), (429, RateLimited), (500, HttpError)],
)
def test_status_codes_map_to_typed_errors(status, exception):
    responses.add(responses.GET, "https://api.test/x", json={}, status=status)
    with pytest.raises(exception):
        HttpClient(rate_limit_seconds=0, max_retries=0).get_json("https://api.test/x")


@responses.activate
def test_a_non_json_body_raises_with_a_preview():
    responses.add(responses.GET, "https://api.test/x", body="<html>nope</html>", status=200)
    with pytest.raises(HttpError) as caught:
        HttpClient(rate_limit_seconds=0).get_json("https://api.test/x")
    assert "non-JSON" in str(caught.value)
    assert "nope" in (caught.value.body or "")


@responses.activate
def test_the_disk_cache_prevents_a_second_call(tmp_path):
    responses.add(responses.GET, "https://api.test/x", json={"n": 1}, status=200)
    client = HttpClient(
        rate_limit_seconds=0, cache_enabled=True, cache_dir=str(tmp_path / "cache")
    )
    first = client.get_json("https://api.test/x")
    second = client.get_json("https://api.test/x")
    assert first == second == {"n": 1}
    assert len(responses.calls) == 1


@responses.activate
def test_an_expired_cache_entry_is_refetched(tmp_path):
    responses.add(responses.GET, "https://api.test/x", json={"n": 1}, status=200)
    responses.add(responses.GET, "https://api.test/x", json={"n": 2}, status=200)
    client = HttpClient(
        rate_limit_seconds=0, cache_enabled=True,
        cache_dir=str(tmp_path / "cache"), cache_ttl_seconds=0,
    )
    assert client.get_json("https://api.test/x") == {"n": 1}
    assert client.get_json("https://api.test/x") == {"n": 2}


@responses.activate
def test_different_query_params_cache_separately(tmp_path):
    responses.add(responses.GET, "https://api.test/x", json={"n": 1}, status=200)
    client = HttpClient(
        rate_limit_seconds=0, cache_enabled=True, cache_dir=str(tmp_path / "cache")
    )
    client.get_json("https://api.test/x", params={"zip": "33139"})
    client.get_json("https://api.test/x", params={"zip": "33154"})
    assert len(responses.calls) == 2


@responses.activate
def test_post_requests_are_never_cached(tmp_path):
    responses.add(responses.POST, "https://api.test/x", json={"n": 1}, status=200)
    client = HttpClient(
        rate_limit_seconds=0, cache_enabled=True, cache_dir=str(tmp_path / "cache")
    )
    client.post_json("https://api.test/x", json_body={"a": 1})
    client.post_json("https://api.test/x", json_body={"a": 1})
    assert len(responses.calls) == 2


@responses.activate
def test_per_host_pacing_is_enforced():
    responses.add(responses.GET, "https://api.test/x", json={}, status=200)
    client = HttpClient(rate_limit_seconds=0.25)
    started = time.monotonic()
    client.get_json("https://api.test/x")
    client.get_json("https://api.test/x")
    assert time.monotonic() - started >= 0.2


@responses.activate
def test_pacing_is_per_host_not_global():
    responses.add(responses.GET, "https://a.test/x", json={}, status=200)
    responses.add(responses.GET, "https://b.test/x", json={}, status=200)
    client = HttpClient(rate_limit_seconds=0.4)
    started = time.monotonic()
    client.get_json("https://a.test/x")
    client.get_json("https://b.test/x")
    assert time.monotonic() - started < 0.3


@responses.activate
def test_a_timeout_is_wrapped_in_an_http_error():
    import requests
    responses.add(responses.GET, "https://api.test/x", body=requests.exceptions.Timeout())
    with pytest.raises(HttpError, match="timeout"):
        HttpClient(rate_limit_seconds=0, max_retries=0).get_json("https://api.test/x")


@responses.activate
def test_a_connection_error_is_wrapped_in_an_http_error():
    import requests
    responses.add(responses.GET, "https://api.test/x",
                  body=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(HttpError, match="transport error"):
        HttpClient(rate_limit_seconds=0, max_retries=0).get_json("https://api.test/x")


def test_the_client_works_as_a_context_manager():
    with HttpClient(rate_limit_seconds=0) as client:
        assert client.session is not None
