"""Regression tests for sitemap tools.

A.3 fix: the GSC Sitemaps API returns ``errors`` and ``warnings`` as
*strings* ("0", "7", ...), but ``get_sitemaps`` compared the raw value to
``0`` (``sitemap["errors"] > 0``), raising
``TypeError: '>' not supported between instances of 'str' and 'int'``.
The fix coerces both fields via ``int(...)`` with a defensive fallback.

Also covers the B.2/B.4 sitemap rollout: response_format enum on
``get_sitemaps`` and ``list_sitemaps_enhanced`` + error envelopes.
"""
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import get_sitemaps, list_sitemaps_enhanced


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


class TestGetSitemapsResponseFormat:
    """B.2 rollout — response_format enum on get_sitemaps."""

    PAYLOAD = {
        "sitemap": [
            {
                "path": "https://example.com/sitemap.xml",
                "lastDownloaded": "2026-04-15T23:52:00Z",
                "errors": "0",
                "warnings": "7",
                "contents": [{"type": "web", "submitted": "672"}],
            },
            {
                "path": "https://example.com/broken.xml",
                "errors": "3",
                "warnings": "0",
            },
        ]
    }

    async def test_markdown_golden_shape(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(self.PAYLOAD))
        out = await get_sitemaps("sc-domain:example.com")
        assert isinstance(out, str)
        assert "Sitemaps for sc-domain:example.com" in out
        assert "Path | Last Downloaded | Status | Indexed URLs | Errors" in out
        assert "| Valid |" in out
        assert "| Has errors |" in out

    async def test_json_shape(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(self.PAYLOAD))
        out = await get_sitemaps(
            "sc-domain:example.com", response_format="json"
        )
        assert isinstance(out, dict)
        assert out["ok"] is True
        assert out["row_count"] == 2
        assert out["rows"][0]["status"] == "Valid"
        assert out["rows"][1]["status"] == "Has errors"
        # Raw ints preserved in json, not stringified.
        assert out["rows"][0]["errors"] == 0
        assert out["rows"][1]["errors"] == 3
        assert out["meta"]["site_url"] == "sc-domain:example.com"

    async def test_csv_mode(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(self.PAYLOAD))
        out = await get_sitemaps(
            "sc-domain:example.com", response_format="csv"
        )
        assert isinstance(out, str)
        assert "Path,Last Downloaded,Status,Indexed URLs,Errors" in out

    async def test_http_error_returns_envelope_dict_in_json(self, monkeypatch):
        resp = MagicMock()
        resp.status = 403
        resp.get = MagicMock(return_value=None)
        http_err = HttpError(resp=resp, content=b'{"error": {"message": "no access"}}')
        service = MagicMock()
        service.sitemaps.return_value.list.side_effect = http_err
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)

        out = await get_sitemaps("sc-domain:example.com", response_format="json")
        assert isinstance(out, dict)
        assert out["ok"] is False
        assert "HTTP 403" in out["error"]
        assert "get_active_account" in out["hint"]


class TestListSitemapsEnhancedResponseFormat:
    """B.2 rollout — response_format enum on list_sitemaps_enhanced."""

    PAYLOAD = {
        "sitemap": [
            {
                "path": "https://example.com/sitemap.xml",
                "lastSubmitted": "2026-04-02T09:34:00Z",
                "lastDownloaded": "2026-04-15T23:52:00Z",
                "errors": "0",
                "warnings": "7",
                "contents": [{"type": "web", "submitted": "672"}],
            },
            {
                "path": "https://example.com/pending.xml",
                "isPending": True,
                "errors": "0",
                "warnings": "0",
            },
        ]
    }

    async def test_markdown_shape_with_pending_note(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(self.PAYLOAD))
        out = await list_sitemaps_enhanced("sc-domain:example.com")
        assert isinstance(out, str)
        assert "all submitted sitemaps" in out
        assert "Path | Last Submitted | Last Downloaded | Type | URLs | Errors | Warnings" in out
        # Pending-processing footnote must survive the helper migration.
        assert "1 sitemaps are still pending" in out

    async def test_json_shape_exposes_pending_count_in_meta(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(self.PAYLOAD))
        out = await list_sitemaps_enhanced(
            "sc-domain:example.com", response_format="json"
        )
        assert isinstance(out, dict)
        assert out["ok"] is True
        assert out["meta"]["pending_count"] == 1
        assert out["meta"]["count"] == 2

    async def test_empty_returns_plain_message(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service({}))
        out = await list_sitemaps_enhanced("sc-domain:example.com")
        assert "No sitemaps found" in out

    async def test_sitemap_index_param_changes_header(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _mock_service(self.PAYLOAD))
        out = await list_sitemaps_enhanced(
            "sc-domain:example.com",
            sitemap_index="https://example.com/sitemap-index.xml",
        )
        assert "child sitemaps from index" in out
        assert "sitemap-index.xml" in out

    async def test_http_error_404_envelope_hint_mentions_site_url(self, monkeypatch):
        resp = MagicMock()
        resp.status = 404
        resp.get = MagicMock(return_value=None)
        http_err = HttpError(resp=resp, content=b'{"error": {"message": "not found"}}')
        service = MagicMock()
        service.sitemaps.return_value.list.side_effect = http_err
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)

        out = await list_sitemaps_enhanced(
            "sc-domain:example.com", response_format="json"
        )
        assert out["ok"] is False
        assert "sc-domain:" in out["hint"]
