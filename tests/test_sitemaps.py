"""Regression tests for sitemap tools.

A.3 fix: the GSC Sitemaps API returns ``errors`` and ``warnings`` as
*strings* ("0", "7", ...), but ``get_sitemaps`` compared the raw value to
``0`` (``sitemap["errors"] > 0``), raising
``TypeError: '>' not supported between instances of 'str' and 'int'``.
The fix coerces both fields via ``int(...)`` with a defensive fallback.
"""
from unittest.mock import MagicMock

import gsc_server
from gsc_server import get_sitemaps


def _mock_service(sitemap_payload):
    service = MagicMock()
    request = MagicMock()
    request.execute.return_value = sitemap_payload
    service.sitemaps.return_value.list.return_value = request
    return service


class TestGetSitemapsStringTypedCounts:
    async def test_errors_as_string_does_not_raise(self, monkeypatch):
        # Payload mirrors what the GSC v1 Sitemaps API actually returns.
        payload = {
            "sitemap": [
                {
                    "path": "https://example.com/sitemap.xml",
                    "lastDownloaded": "2026-04-15T23:52:00Z",
                    "errors": "0",
                    "warnings": "7",
                    "contents": [{"type": "web", "submitted": "672"}],
                }
            ]
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(payload))
        out = await get_sitemaps("sc-domain:example.com")
        assert "Error retrieving sitemaps" not in out
        assert "https://example.com/sitemap.xml" in out
        assert "Valid" in out  # errors=0 → Valid

    async def test_errors_nonzero_string_flags_has_errors(self, monkeypatch):
        payload = {
            "sitemap": [
                {
                    "path": "https://example.com/bad.xml",
                    "lastDownloaded": "2026-04-15T23:52:00Z",
                    "errors": "3",
                    "warnings": "0",
                }
            ]
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(payload))
        out = await get_sitemaps("sc-domain:example.com")
        assert "Has errors" in out
        assert "| 3" in out  # error count surfaced

    async def test_errors_as_int_still_works(self, monkeypatch):
        # Defensive: the API could theoretically return ints too.
        payload = {
            "sitemap": [
                {"path": "https://example.com/s.xml", "errors": 2, "warnings": 1}
            ]
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(payload))
        out = await get_sitemaps("sc-domain:example.com")
        assert "Has errors" in out

    async def test_errors_missing_defaults_to_zero(self, monkeypatch):
        payload = {"sitemap": [{"path": "https://example.com/s.xml"}]}
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(payload))
        out = await get_sitemaps("sc-domain:example.com")
        assert "Valid" in out
        # Pin the errors column rendering so a future regression that
        # returns "None" or blank is caught.
        assert "| 0" in out

    async def test_errors_non_numeric_string_falls_back_to_zero(self, monkeypatch):
        # If the API ever returns "n/a" or similar, we log Valid rather than crash.
        payload = {
            "sitemap": [{"path": "https://example.com/s.xml", "errors": "n/a"}]
        }
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(payload))
        out = await get_sitemaps("sc-domain:example.com")
        assert "Error retrieving sitemaps" not in out
        assert "Valid" in out

    async def test_empty_sitemap_list(self, monkeypatch):
        payload = {}
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(payload))
        out = await get_sitemaps("sc-domain:example.com")
        assert "No sitemaps found" in out
