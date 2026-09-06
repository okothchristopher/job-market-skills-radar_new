"""Response cache tests."""

from __future__ import annotations

import time

import pytest

from jobradar.fetch.cache import ResponseCache


@pytest.fixture
def cache(tmp_path):
    c = ResponseCache(tmp_path / "cache.sqlite", ttl_seconds=3600)
    yield c
    c.close()


def test_miss_then_hit(cache):
    url = "https://example.com/job/1"
    assert cache.get(url) is None
    cache.put(url, 200, {"Content-Type": "text/html"}, b"<html>hi</html>", "utf-8")
    hit = cache.get(url)
    assert hit is not None
    assert hit.status_code == 200
    assert hit.text == "<html>hi</html>"
    assert hit.headers["Content-Type"] == "text/html"


def test_expired_entries_are_misses(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite", ttl_seconds=0.01)
    cache.put("https://example.com/a", 200, {}, b"body")
    time.sleep(0.05)
    assert cache.get("https://example.com/a") is None
    cache.close()


def test_put_replaces_existing_entry(cache):
    url = "https://example.com/job/1"
    cache.put(url, 200, {}, b"old")
    cache.put(url, 200, {}, b"new")
    assert cache.get(url).body == b"new"
    assert cache.stats()["entries"] == 1


def test_survives_reopen(tmp_path):
    """The cache must persist across runs — that is its entire purpose."""
    path = tmp_path / "c.sqlite"
    first = ResponseCache(path)
    first.put("https://example.com/a", 200, {}, b"persisted")
    first.close()

    second = ResponseCache(path)
    assert second.get("https://example.com/a").body == b"persisted"
    second.close()


def test_bodies_are_compressed(cache):
    """A 600KB category page should not cost 600KB on disk."""
    body = b"<div class='job'>Software Engineer</div>" * 5000
    cache.put("https://example.com/big", 200, {}, body)
    assert cache.get("https://example.com/big").body == body
    assert cache.stats()["compressed_bytes"] < len(body) / 5


def test_purge_expired(tmp_path):
    cache = ResponseCache(tmp_path / "c.sqlite", ttl_seconds=0.01)
    cache.put("https://example.com/a", 200, {}, b"a")
    cache.put("https://example.com/b", 200, {}, b"b")
    time.sleep(0.05)
    assert cache.purge_expired() == 2
    assert cache.stats()["entries"] == 0
    cache.close()


def test_binary_and_unicode_bodies_round_trip(cache):
    cache.put("https://example.com/u", 200, {}, "Nairobi — Kenya · café".encode(), "utf-8")
    assert "Nairobi — Kenya · café" in cache.get("https://example.com/u").text
