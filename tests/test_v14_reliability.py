"""v1.4.0 reliability: retrying executor, error envelopes, atomic token
writes, the inspection ledger/cache, async inspection jobs and the
health-check additions."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

import gsc_server
from gsc_server import (
    ErrorCode,
    _gsc_execute_sync,
    _http_error_envelope,
    gsc_health_check,
    gsc_inspect_start,
    gsc_inspect_status,
    gsc_inspect_url_enhanced,
)

SITE = "sc-domain:example.com"


def _http_error(status, message="err", reason=None, retry_after=None):
    resp = MagicMock()
    resp.status = status
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    resp.get = MagicMock(side_effect=lambda k, d=None: headers.get(k, d))
    err = {"message": message}
    if reason:
        err["errors"] = [{"reason": reason}]
    return HttpError(resp=resp, content=json.dumps({"error": err}).encode())


def _flaky(outcomes):
    """Zero-arg callable that raises/returns each outcome in turn."""
    calls = {"n": 0}

    def fn():
        r = outcomes[calls["n"]]
        calls["n"] += 1
        if isinstance(r, BaseException):
            raise r
        return r
    fn.calls = calls
    return fn


# ---------------------------------------------------------------------------
# Retrying executor
# ---------------------------------------------------------------------------


class TestExecutor:
    def test_retries_5xx_then_succeeds(self, monkeypatch):
        delays = []
        monkeypatch.setattr(gsc_server, "_retry_sleep", delays.append)
        fn = _flaky([_http_error(503), _http_error(500), {"ok": 1}])
        assert _gsc_execute_sync(fn, step="s") == {"ok": 1}
        assert fn.calls["n"] == 3
        assert len(delays) == 2 and 0.5 <= delays[0] <= 1.0 and 1.0 <= delays[1] <= 2.0

    def test_gives_up_after_max_attempts_and_tags_step(self):
        fn = _flaky([_http_error(503)] * 4)
        with pytest.raises(HttpError) as ei:
            _gsc_execute_sync(fn, step="searchanalytics.query")
        assert fn.calls["n"] == 4
        env = _http_error_envelope(ei.value, tool="t")
        assert env["step"] == "searchanalytics.query" and env["attempts"] == 4
        assert env["http_status"] == 503 and env["error_code"] == ErrorCode.SERVICE_UNAVAILABLE

    @pytest.mark.parametrize("err", [
        _http_error(400), _http_error(403), _http_error(404),
        _http_error(429, "Quota exceeded for quota metric 'Queries' per day"),
        _http_error(403, "Daily Limit Exceeded", reason="dailyLimitExceeded"),
    ])
    def test_no_retry_on_caller_errors_or_daily_quota(self, err):
        fn = _flaky([err, {"ok": 1}])
        with pytest.raises(HttpError):
            _gsc_execute_sync(fn, step="s")
        assert fn.calls["n"] == 1

    def test_rate_limit_retried_and_retry_after_respected(self, monkeypatch):
        delays = []
        monkeypatch.setattr(gsc_server, "_retry_sleep", delays.append)
        fn = _flaky([_http_error(429, "slow down", retry_after="3"), {"ok": 1}])
        assert _gsc_execute_sync(fn, step="s") == {"ok": 1}
        assert delays == [3.0]

    def test_long_retry_after_is_handed_back_not_slept(self, monkeypatch):
        delays = []
        monkeypatch.setattr(gsc_server, "_retry_sleep", delays.append)
        fn = _flaky([_http_error(429, "slow down", retry_after="30")])
        with pytest.raises(HttpError) as ei:
            _gsc_execute_sync(fn, step="s")
        assert delays == []
        assert _http_error_envelope(ei.value, tool="t")["retry_after"] == 30.0

    def test_timeout_becomes_timeout_envelope(self):
        fn = _flaky([TimeoutError("read timed out")] * 4)
        with pytest.raises(HttpError) as ei:
            _gsc_execute_sync(fn, step="urlInspection.inspect")
        env = _http_error_envelope(ei.value, tool="t")
        assert env["error_code"] == ErrorCode.TIMEOUT
        assert env["retryable"] is True and env["step"] == "urlInspection.inspect"
        assert "timed out" in env["error"]


class TestErrorEnvelopes:
    def test_daily_quota_is_quota_exhausted_until_pt_midnight(self):
        env = _http_error_envelope(
            _http_error(429, "Quota exceeded per day", reason="dailyLimitExceeded"), tool="t",
        )
        assert env["error_code"] == ErrorCode.QUOTA_EXHAUSTED
        assert env["retryable"] is False
        assert 0 < env["retry_after"] <= 86400
        assert env["google_reason"] == "dailyLimitExceeded"

    def test_403_rate_limit_is_quota_exceeded(self):
        env = _http_error_envelope(_http_error(403, "slow", reason="userRateLimitExceeded"), tool="t")
        assert env["error_code"] == ErrorCode.QUOTA_EXCEEDED and env["retryable"] is True

    def test_502_504_map_to_service_unavailable(self):
        for status in (502, 504):
            assert _http_error_envelope(_http_error(status), tool="t")["error_code"] == ErrorCode.SERVICE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Atomic token writes
# ---------------------------------------------------------------------------


class TestTokenWrites:
    def test_replacement_keeps_existing_mode(self, tmp_path):
        tmp_path = tmp_path / "tokens"
        tmp_path.mkdir()
        path = tmp_path / "token.json"
        path.write_text("{}")
        os.chmod(path, 0o666)
        gsc_server._write_token_file(str(path), '{"token": "x"}')
        assert stat.S_IMODE(path.stat().st_mode) == 0o666
        assert json.loads(path.read_text()) == {"token": "x"}
        assert [p.name for p in tmp_path.iterdir()] == ["token.json"]  # no temp left behind

    def test_new_file_mode_follows_directory(self, tmp_path):
        private = tmp_path / "private"
        private.mkdir(mode=0o700)
        gsc_server._write_token_file(str(private / "t.json"), "{}")
        assert stat.S_IMODE((private / "t.json").stat().st_mode) == 0o600
        shared = tmp_path / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        gsc_server._write_token_file(str(shared / "t.json"), "{}")
        assert stat.S_IMODE((shared / "t.json").stat().st_mode) == 0o666


# ---------------------------------------------------------------------------
# Inspection ledger and cache
# ---------------------------------------------------------------------------


def _inspection_service(by_url=None, delay_event=None):
    """Mock urlInspection service answering per URL; records call count."""
    service = MagicMock()
    service.calls = []
    lock = threading.Lock()

    def _inspect(body):
        url = body["inspectionUrl"]
        req = MagicMock()

        def _execute():
            with lock:
                service.calls.append(url)
            if delay_event is not None:
                delay_event.wait(5)
            r = (by_url or {}).get(url)
            if isinstance(r, BaseException):
                raise r
            return r or {"inspectionResult": {"indexStatusResult": {
                "verdict": "PASS", "coverageState": "Submitted and indexed",
                "lastCrawlTime": "2026-09-20T10:00:00Z",
                "googleCanonical": url, "userCanonical": url,
            }}}
        req.execute.side_effect = _execute
        return req

    service.urlInspection.return_value.index.return_value.inspect.side_effect = _inspect
    return service


class TestLedger:
    def test_daily_and_minute_limits(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "_INSPECTION_DAILY_LIMIT", 3)
        monkeypatch.setattr(gsc_server, "_INSPECTION_MINUTE_LIMIT", 2)
        assert gsc_server._reserve_inspection(SITE)[0] is True
        assert gsc_server._reserve_inspection(SITE)[0] is True
        ok, refused, _ = gsc_server._reserve_inspection(SITE)
        assert (ok, refused) == (False, "minute")
        monkeypatch.setattr(gsc_server, "_INSPECTION_MINUTE_LIMIT", 10)
        assert gsc_server._reserve_inspection(SITE)[0] is True
        ok, refused, counts = gsc_server._reserve_inspection(SITE)
        assert (ok, refused, counts["day_used"]) == (False, "day", 3)
        quota = gsc_server._inspection_quota(SITE)
        assert quota["day_used"] == 3 and quota["day_remaining"] == 0

    def test_one_slot_two_racers_one_winner(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "_INSPECTION_DAILY_LIMIT", 1)
        results = []
        barrier = threading.Barrier(2)

        def racer():
            barrier.wait()
            results.append(gsc_server._reserve_inspection(SITE)[0])
        threads = [threading.Thread(target=racer) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, True]

    def test_state_db_uses_rollback_journal(self):
        conn = gsc_server._state_db()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        finally:
            conn.close()


class TestInspectOne:
    async def test_cache_hit_costs_no_quota_and_force_bypasses(self):
        service = _inspection_service()
        _, cached_at = await gsc_server._inspect_one(service, SITE, "https://example.com/a")
        assert cached_at is None and len(service.calls) == 1
        _, cached_at = await gsc_server._inspect_one(service, SITE, "https://example.com/a")
        assert cached_at is not None and len(service.calls) == 1
        await gsc_server._inspect_one(service, SITE, "https://example.com/a", force=True)
        assert len(service.calls) == 2
        assert gsc_server._inspection_quota(SITE)["day_used"] == 2

    async def test_retries_reserve_each_attempt(self):
        service = _inspection_service({"https://example.com/a": None})
        outcomes = [_http_error(503), None]

        def _inspect(body):
            req = MagicMock()
            r = outcomes.pop(0)
            if r is not None:
                req.execute.side_effect = r
            else:
                req.execute.return_value = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
            return req
        service.urlInspection.return_value.index.return_value.inspect.side_effect = _inspect
        await gsc_server._inspect_one(service, SITE, "https://example.com/a")
        assert gsc_server._inspection_quota(SITE)["day_used"] == 2

    async def test_daily_quota_raises_quota_exhausted(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "_INSPECTION_DAILY_LIMIT", 0)
        with pytest.raises(HttpError) as ei:
            await gsc_server._inspect_one(_inspection_service(), SITE, "https://example.com/a")
        assert _http_error_envelope(ei.value, tool="t")["error_code"] == ErrorCode.QUOTA_EXHAUSTED


# ---------------------------------------------------------------------------
# Async jobs (acceptance test 6's inspection half)
# ---------------------------------------------------------------------------


async def _wait_done(job_id, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        st = await gsc_inspect_status(job_id)
        if st["state"] in ("done", "quota_exhausted", "failed"):
            return st
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


class TestInspectJobs:
    async def test_many_urls_complete_with_canonical_flags(self, monkeypatch):
        urls = [f"https://example.com/p{i}" for i in range(33)]
        mismatches = {
            urls[3]: {"inspectionResult": {"indexStatusResult": {
                "verdict": "PASS", "googleCanonical": "https://example.com/other", "userCanonical": urls[3],
                "sitemap": ["https://example.com/sitemap.xml"],
            }}},
            urls[7]: {"inspectionResult": {"indexStatusResult": {
                "verdict": "NEUTRAL", "googleCanonical": "https://example.com/x", "userCanonical": urls[7],
            }}},
        }
        service = _inspection_service(mismatches)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        start = await gsc_inspect_start(SITE, urls + [urls[0]])  # duplicate dropped
        assert start["ok"] is True and start["total"] == 33 and start["queued"] == 33
        st = await _wait_done(start["job_id"])
        assert st["state"] == "done"
        errors = [r for r in st["results"] if r["status"] != "done"]
        assert not errors, errors
        assert st["progress"]["done"] == 33 and st["progress"]["canonical_mismatches"] == 2
        page = st["results"]
        assert len(page) == 33 and page[3]["canonical_mismatch"] is True
        assert page[3]["sitemaps"] == ["https://example.com/sitemap.xml"]
        # A re-run is answered from the cache: no new Google calls.
        calls = len(service.calls)
        again = await gsc_inspect_start(SITE, urls)
        assert again["cached"] == 33 and again["state"] == "done"
        assert len(service.calls) == calls

    async def test_status_answers_while_inspections_are_slow(self, monkeypatch):
        gate = threading.Event()
        service = _inspection_service(delay_event=gate)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        start = await gsc_inspect_start(SITE, ["https://example.com/a", "https://example.com/b"])
        await asyncio.sleep(0.05)
        st = await asyncio.wait_for(gsc_inspect_status(start["job_id"]), timeout=1.0)
        assert st["state"] == "running" and st["progress"]["pending"] == 2
        gate.set()
        assert (await _wait_done(start["job_id"]))["progress"]["done"] == 2

    async def test_quota_exhausted_keeps_partial_results(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "_INSPECTION_DAILY_LIMIT", 5)
        for _ in range(3):  # another client already used 3 of today's 5
            assert gsc_server._reserve_inspection(SITE)[0]
        service = _inspection_service()
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        start = await gsc_inspect_start(SITE, [f"https://example.com/p{i}" for i in range(5)], concurrency=1)
        st = await _wait_done(start["job_id"])
        assert st["state"] == "quota_exhausted"
        assert st["progress"]["done"] == 2
        statuses = [r["status"] for r in st["results"]]
        assert statuses.count("done") == 2 and "skipped_quota" in statuses
        err = next(r for r in st["results"] if r["status"] == "error")
        assert err["error"]["error_code"] == ErrorCode.QUOTA_EXHAUSTED

    async def test_per_url_error_does_not_stop_job(self, monkeypatch):
        service = _inspection_service({"https://example.com/bad": _http_error(404, "nope")})
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        start = await gsc_inspect_start(SITE, ["https://example.com/bad", "https://example.com/ok"])
        st = await _wait_done(start["job_id"])
        assert st["state"] == "done"
        by_url = {r["url"]: r for r in st["results"]}
        assert by_url["https://example.com/bad"]["error"]["error_code"] == ErrorCode.NOT_FOUND
        assert by_url["https://example.com/ok"]["verdict"] == "PASS"

    async def test_unknown_job_is_job_not_found(self):
        out = await gsc_inspect_status("insp-missing")
        assert out["ok"] is False and out["error_code"] == ErrorCode.JOB_NOT_FOUND
        assert "gsc_inspect_start" in out["hint"]

    async def test_markdown_status(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _inspection_service())
        start = await gsc_inspect_start(SITE, ["https://example.com/a"])
        await _wait_done(start["job_id"])
        out = await gsc_inspect_status(start["job_id"], response_format="markdown")
        assert out.startswith(f"Inspection job {start['job_id']}") and "https://example.com/a | done" in out

    async def test_empty_url_list_rejected(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _inspection_service())
        out = await gsc_inspect_start(SITE, [])
        assert out["error_code"] == ErrorCode.BAD_REQUEST


class TestLegacyInspectionTools:
    async def test_single_url_cached_note_and_force(self, monkeypatch):
        service = _inspection_service()
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        await gsc_inspect_url_enhanced(SITE, "https://example.com/a")
        out = await gsc_inspect_url_enhanced(SITE, "https://example.com/a", response_format="json")
        assert out["cached_at"] is not None and len(service.calls) == 1
        out = await gsc_inspect_url_enhanced(SITE, "https://example.com/a", response_format="json", force=True)
        assert out["cached_at"] is None and len(service.calls) == 2

    async def test_batch_surfaces_quota_instead_of_burying_it(self, monkeypatch):
        service = _inspection_service({"https://example.com/a": _http_error(429, "Quota exceeded per day")})
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        out = await gsc_server.gsc_batch_url_inspection(
            SITE, urls="https://example.com/a\nhttps://example.com/b", response_format="json",
        )
        assert out["ok"] is False and out["error_code"] == ErrorCode.QUOTA_EXHAUSTED


# ---------------------------------------------------------------------------
# Health check additions
# ---------------------------------------------------------------------------


class TestHealthCheck:
    def _service(self, appearances):
        service = MagicMock()
        service.sites.return_value.get.return_value.execute.return_value = {"permissionLevel": "siteOwner"}
        service.sitemaps.return_value.list.return_value.execute.return_value = {"sitemap": []}

        def _query(*, siteUrl, body):
            req = MagicMock()
            if body["dimensions"] == ["searchAppearance"]:
                req.execute.return_value = {"rows": [{"keys": [a]} for a in appearances]}
            else:
                req.execute.return_value = {"rows": [{"keys": ["2026-09-25"]}]}
            return req
        service.searchanalytics.return_value.query.side_effect = _query
        return service

    async def test_reports_version_freshness_limits_and_no_unknowns(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service",
                            lambda: self._service(["VIDEO", "TRANSLATED_RESULT", "REVIEW_SNIPPET"]))
        out = await gsc_health_check(SITE)
        assert out["ok"] is True
        assert out["server_version"] == gsc_server._SERVER_VERSION
        assert out["latest_final_date"] == "2026-09-25"
        assert out["search_appearances_seen"] == ["REVIEW_SNIPPET", "TRANSLATED_RESULT", "VIDEO"]
        assert out["unknown_values"] == {}
        assert "request_indexing" in out["api_limits"]
        assert out["inspection_quota"]["day_limit"] == 2000

    async def test_flags_new_ai_looking_values(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: self._service(["VIDEO", "AI_OVERVIEW"]))
        monkeypatch.setattr(gsc_server, "_live_discovery_enums", lambda: {"type": ["WEB", "AI_MODE"]})
        out = await gsc_health_check(SITE)
        assert out["unknown_values"] == {"searchAppearance": ["AI_OVERVIEW"], "type": ["AI_MODE"]}
        assert "AI_OVERVIEW" in out["ai_features_signal"]


class TestReviewFixes:
    def test_retry_after_honoured_on_5xx(self, monkeypatch):
        delays = []
        monkeypatch.setattr(gsc_server, "_retry_sleep", delays.append)
        fn = _flaky([_http_error(503, retry_after="5"), {"ok": 1}])
        assert _gsc_execute_sync(fn, step="s") == {"ok": 1}
        assert delays == [5.0]

    async def test_inspection_retries_connection_reset(self):
        service = _inspection_service()
        outcomes = [ConnectionResetError("reset"), None]

        def _inspect(body):
            req = MagicMock()
            r = outcomes.pop(0)
            if r is not None:
                req.execute.side_effect = r
            else:
                req.execute.return_value = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
            return req
        service.urlInspection.return_value.index.return_value.inspect.side_effect = _inspect
        resp, _ = await gsc_server._inspect_one(service, SITE, "https://example.com/a")
        assert resp["inspectionResult"]["indexStatusResult"]["verdict"] == "PASS"

    async def test_google_daily_quota_stops_job_after_drain(self, monkeypatch):
        daily = _http_error(429, "Quota exceeded for quota metric per day")
        urls = [f"https://example.com/p{i}" for i in range(6)]
        service = _inspection_service({urls[1]: daily})
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: service)
        start = await gsc_inspect_start(SITE, urls, concurrency=1)
        st = await _wait_done(start["job_id"])
        assert st["state"] == "quota_exhausted"
        assert [r["status"] for r in st["results"]] == ["done", "error"] + ["skipped_quota"] * 4
        assert len(service.calls) == 2  # nothing started after the quota error

    async def test_cached_batch_larger_than_daily_limit_is_allowed(self, monkeypatch):
        monkeypatch.setattr(gsc_server, "_INSPECTION_DAILY_LIMIT", 1)
        monkeypatch.setattr(gsc_server, "get_gsc_service", lambda: _inspection_service())
        start = await gsc_inspect_start(SITE, ["https://example.com/a", "https://example.com/b"])
        assert start["ok"] is True
