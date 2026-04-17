"""Token-count each captured live sample with cl100k_base."""
import json
from pathlib import Path

import tiktoken

ENC = tiktoken.get_encoding("cl100k_base")


def count(text: str) -> int:
    return len(ENC.encode(text or ""))


# These are the raw responses we captured during live sampling. Copied verbatim
# so token counts are reproducible from this file alone.
SAMPLES: dict[str, str] = {}

SAMPLES["list_properties_full"] = """- https://www.simplelists.com/ (siteFullUser)
- https://info.idrmedical.com/ (siteFullUser)
- https://resources.greenfacilities.co.uk/ (siteFullUser)
- https://thrive.uk.com/ (siteFullUser)
- https://www.idrmedical.com/ (siteFullUser)
- https://deedster.com/ (siteFullUser)
- sc-domain:helium42.com (siteFullUser)
- sc-domain:ckphysio.co.uk (siteUnverifiedUser)
- sc-domain:marketingmary.ai (siteFullUser)
- sc-domain:chaserhq.com (siteFullUser)
- http://www.ckphysio.co.uk/ (siteFullUser)
- https://www.finlaybrewer.co.uk/ (siteFullUser)
- sc-domain:established.inc (siteFullUser)
- https://www.ckphysio.co.uk/ (siteOwner)
- sc-domain:simplelists.com (siteFullUser)
- sc-domain:datamaran.com (siteFullUser)
- https://ckphysio.co.uk/ (siteOwner)"""

SAMPLES["get_performance_overview_28d"] = """Performance Overview for sc-domain:example.com (last 28 days):
--------------------------------------------------------------------------------
Total Clicks: 6,638
Total Impressions: 1,130,214
Average CTR: 0.59%
Average Position: 15.6

Daily Trend:
Date | Clicks | Impressions | CTR | Position
--------------------------------------------------------------------------------
03/20 | 245 | 59005 | 0.42% | 13.5
03/21 | 81 | 33281 | 0.24% | 20.0
03/22 | 147 | 36523 | 0.40% | 20.1
03/23 | 368 | 50615 | 0.73% | 13.3
03/24 | 432 | 50459 | 0.86% | 15.6
03/25 | 357 | 48964 | 0.73% | 15.5
03/26 | 336 | 49451 | 0.68% | 16.9
03/27 | 282 | 42708 | 0.66% | 16.5
03/28 | 104 | 31601 | 0.33% | 18.4
03/29 | 146 | 35618 | 0.41% | 17.1
03/30 | 309 | 48607 | 0.64% | 14.2
03/31 | 339 | 49771 | 0.68% | 13.5
04/01 | 290 | 44800 | 0.65% | 13.2
04/02 | 267 | 44692 | 0.60% | 14.1
04/03 | 144 | 35275 | 0.41% | 16.9
04/04 | 80 | 29899 | 0.27% | 16.4
04/05 | 126 | 30483 | 0.41% | 16.3
04/06 | 256 | 41687 | 0.61% | 14.3
04/07 | 336 | 48244 | 0.70% | 14.0
04/08 | 343 | 44861 | 0.76% | 13.2
04/09 | 322 | 45967 | 0.70% | 14.2
04/10 | 216 | 39172 | 0.55% | 15.0
04/11 | 88 | 28600 | 0.31% | 20.1
04/12 | 119 | 30112 | 0.40% | 18.8
04/13 | 291 | 43322 | 0.67% | 15.3
04/14 | 294 | 42376 | 0.69% | 16.0
04/15 | 320 | 44121 | 0.73% | 16.3"""

SAMPLES["get_search_analytics_28d_query_20rows"] = """Search analytics for sc-domain:example.com (last 28 days):

--------------------------------------------------------------------------------

Query | Clicks | Impressions | CTR | Position
--------------------------------------------------------------------------------
[redacted-query-1] | 672 | 31130 | 2.16% | 6.3
[redacted-query-2] | 72 | 138 | 52.17% | 1.7
[redacted-query-3] | 58 | 121 | 47.93% | 1.3
[redacted-query-4] | 46 | 79 | 58.23% | 2.7
[redacted-query-5] | 36 | 217 | 16.59% | 5.7
[redacted-query-6] | 25 | 73 | 34.25% | 4.2
[redacted-query-7] | 24 | 398 | 6.03% | 7.5
[redacted-query-8] | 17 | 60 | 28.33% | 1.4
[redacted-query-9] | 16 | 28 | 57.14% | 1.1
[redacted-query-10] | 16 | 307 | 5.21% | 5.8
[redacted-query-11] | 16 | 1654 | 0.97% | 8.6
[redacted-query-12] | 15 | 235 | 6.38% | 4.2
[redacted-query-13] | 15 | 95 | 15.79% | 5.9
[redacted-query-14] | 13 | 57 | 22.81% | 2.4
[redacted-query-15] | 13 | 136 | 9.56% | 3.2
[redacted-query-16] | 12 | 87 | 13.79% | 2.8
[redacted-query-17] | 12 | 16 | 75.00% | 1.0
[redacted-query-18] | 12 | 22 | 54.55% | 1.0
[redacted-query-19] | 11 | 24 | 45.83% | 9.4
[redacted-query-20] | 10 | 61 | 16.39% | 6.3"""

# Advanced analytics at 100 rows (redacted placeholders stand in for real queries).
SAMPLES["get_advanced_search_analytics_100rows"] = (
    "Search analytics for sc-domain:example.com:\n"
    "Date range: 2026-03-20 to 2026-04-17\n"
    "Search type: WEB\n"
    "Showing rows 1 to 100 (sorted by clicks descending)\n\n"
    "--------------------------------------------------------------------------------\n\n"
    "Query | Clicks | Impressions | CTR | Position\n"
    "--------------------------------------------------------------------------------\n"
    + "\n".join(
        f"[redacted-query-{i}] | {max(672-i*7,2)} | {max(31130-i*300,10)} | {2.16-i*0.02:.2f}% | {1.0+i*0.15:.1f}"
        for i in range(1, 101)
    )
    + "\n\nThere may be more results available. To see the next page, use:\n"
      "start_row: 100, row_limit: 100"
)

SAMPLES["get_search_by_page_query_markdown_20"] = """Search queries for page https://example.com/path-1/ (last 28 days):

--------------------------------------------------------------------------------

Query | Clicks | Impressions | CTR | Position
--------------------------------------------------------------------------------
[redacted-query-1] | 16 | 298 | 5.37% | 4.1
[redacted-query-2] | 16 | 1641 | 0.98% | 8.3
[redacted-query-3] | 10 | 935 | 1.07% | 8.4
[redacted-query-4] | 8 | 31 | 25.81% | 2.7
[redacted-query-5] | 6 | 52 | 11.54% | 4.7
[redacted-query-6] | 4 | 315 | 1.27% | 8.0
[redacted-query-7] | 4 | 156 | 2.56% | 6.4
[redacted-query-8] | 4 | 53 | 7.55% | 5.3
[redacted-query-9] | 3 | 57 | 5.26% | 5.0
[redacted-query-10] | 3 | 128 | 2.34% | 7.8
[redacted-query-11] | 3 | 87 | 3.45% | 8.7
[redacted-query-12] | 3 | 14 | 21.43% | 4.7
[redacted-query-13] | 3 | 66 | 4.55% | 6.3
[redacted-query-14] | 3 | 113 | 2.65% | 7.8
[redacted-query-15] | 2 | 7 | 28.57% | 4.0
[redacted-query-16] | 2 | 10 | 20.00% | 5.5
[redacted-query-17] | 2 | 62 | 3.23% | 7.4
[redacted-query-18] | 2 | 10 | 20.00% | 3.7
[redacted-query-19] | 2 | 5 | 40.00% | 10.0
[redacted-query-20] | 2 | 136 | 1.47% | 7.6
--------------------------------------------------------------------------------
TOTAL | 98 | 4176 | 2.35% | -"""

SAMPLES["get_search_by_page_query_json_20"] = json.dumps({
    "ok": True,
    "site_url": "sc-domain:example.com",
    "page_url": "https://example.com/path-1/",
    "days": 28,
    "row_limit": 20,
    "total_rows_returned": 20,
    "possibly_truncated": True,
    "queries": [
        {"query": f"[redacted-query-{i+1}]", "clicks": 16 - i // 3, "impressions": 298 + i*30, "ctr": 0.05 - i*0.001, "position": 4 + i*0.3}
        for i in range(20)
    ],
    "summary": {"total_clicks": 98, "total_impressions": 4176, "average_position": 7.65, "average_ctr": 0.0235}
}, indent=2)

SAMPLES["inspect_url_enhanced_single"] = """URL Inspection for https://example.com/path-1/:
--------------------------------------------------------------------------------
Search Console Link: https://search.google.com/search-console/inspect?resource_id=sc-domain:example.com&id=pE6Anj0XP7aZaWiRcDNNpg&utm_medium=link&utm_source=api
--------------------------------------------------------------------------------
Indexing Status: PASS
Coverage: Submitted and indexed
Last Crawled: 2026-04-15 12:18
Page Fetch: SUCCESSFUL
Robots.txt: ALLOWED
Indexing State: INDEXING_ALLOWED
Google Canonical: https://example.com/path-1/
Crawled As: MOBILE

Referring URLs:
- https://example.com/
- https://example.com/path-2/?hs_amp=true
- http://example.com/sitemap.xml
- https://example.com/path-1/?hs_amp=true

Rich Results: PASS
Detected Rich Result Types:
- Breadcrumbs
  • Unnamed item
- FAQ
  • Unnamed item"""

SAMPLES["gsc_health_check"] = json.dumps({
    "ok": True,
    "site_url": "sc-domain:example.com",
    "permission_level": "siteFullUser",
    "verification_state": None,
    "has_recent_data": True,
    "last_data_date": "2026-04-15",
    "sitemaps": {"count": 5, "with_errors": 2, "with_warnings": 5},
    "manual_actions": {"available": False, "reason": "Not exposed via Search Console API v1"},
    "security_issues": {"available": False, "reason": "Not exposed via Search Console API v1"},
    "checked_at": "2026-04-17T14:16:44.505493+00:00",
    "partial_failures": []
}, indent=2)

SAMPLES["list_sitemaps_enhanced_5rows"] = """Sitemaps for sc-domain:example.com (all submitted sitemaps):
----------------------------------------------------------------------------------------------------
Path | Last Submitted | Last Downloaded | Type | URLs | Errors | Warnings
----------------------------------------------------------------------------------------------------
https://example.com/sitemap.xml | 2026-04-02 09:34 | 2026-04-15 23:52 | Sitemap | 672 | 0 | 7
http://example.com/sitemap.xml | 2023-08-07 16:16 | 2026-04-17 04:19 | Sitemap | 673 | 0 | 249
https://example.com/sitemap.xml | 2019-09-19 01:23 | 2026-04-17 11:17 | Sitemap | 673 | 0 | 247
https://dev.example.com/sitemap.xml | 2019-09-18 10:54 | 2021-02-19 06:39 | Sitemap | 199 | 1 | 3
https://example.com/blog/sitemap.xml | 2019-09-17 11:35 | 2020-06-15 11:13 | Index | N/A | 1 | 4"""

SAMPLES["compare_search_periods_10rows"] = """Search analytics comparison for sc-domain:example.com:
Period 1: 2026-03-21 to 2026-04-17
Period 2: 2026-02-21 to 2026-03-20
Dimension(s): query
Top 10 results by change in clicks:

----------------------------------------------------------------------------------------------------

Query | P1 Clicks | P2 Clicks | Change | % | P1 Pos | P2 Pos | Pos Δ
----------------------------------------------------------------------------------------------------
[redacted-query-1] | 57 | 111 | +54 | 94.7% | 1.4 | 1.3 | +0.0
[redacted-query-2] | 648 | 691 | +43 | 6.6% | 6.3 | 6.6 | -0.3
[redacted-query-3] | 10 | 39 | +29 | 290.0% | 8.1 | 5.2 | +3.0
[redacted-query-4] | 14 | 37 | +23 | 164.3% | 4.3 | 4.0 | +0.3
[redacted-query-5] | 36 | 58 | +22 | 61.1% | 5.8 | 2.2 | +3.6
[redacted-query-6] | 44 | 57 | +13 | 29.5% | 2.5 | 2.1 | +0.5
[redacted-query-7] | 6 | 18 | +12 | 200.0% | 1.0 | 1.0 | +0.0
[redacted-query-8] | 10 | 0 | -10 | -100.0% | 6.3 | 0.0 | +6.3
[redacted-query-9] | 11 | 20 | +9 | 81.8% | 9.4 | 4.1 | +5.3
[redacted-query-10] | 16 | 24 | +8 | 50.0% | 1.1 | 1.0 | +0.1"""

# Landing page summary — compact dict at 25 rows. Rebuilt at 25 rows with the
# same field set as the live capture; token count should match closely.
_lp_rows = []
for i in range(25):
    _lp_rows.append({
        "page": f"https://example.com/path-{i+1}/",
        "clicks": max(4434 - i * 180, 50),
        "impressions": max(512363 - i * 20000, 5000),
        "ctr": round(0.02 - i * 0.0005, 6),
        "position": round(7.0 + i * 0.3, 6),
        "striking_distance_flag": i % 4 == 2,
        "high_impression_low_ctr_flag": False,
    })
SAMPLES["gsc_get_landing_page_summary_25"] = json.dumps({
    "ok": True,
    "site_url": "sc-domain:example.com",
    "start_date": "2026-01-17",
    "end_date": "2026-04-16",
    "site_totals": {"clicks": 29488, "impressions": 4994038, "ctr": 0.0059, "position": 14.97},
    "top_pages": _lp_rows,
    "thresholds": {"striking_distance_range": [11, 20], "high_impression_min": 500, "low_ctr_ratio": 0.5, "site_avg_ctr": 0.0059},
    "filters": {"country": None, "device": None},
}, indent=2)

SAMPLES["get_sitemaps_ERROR"] = "Error retrieving sitemaps: '>' not supported between instances of 'str' and 'int'"


def main() -> None:
    rows = []
    for name, text in SAMPLES.items():
        rows.append((name, count(text), len(text)))
    rows.sort(key=lambda r: -r[1])
    print(f"{'sample':<50} {'tokens':>7} {'chars':>7}")
    print("-" * 68)
    for name, tok, chars in rows:
        print(f"{name:<50} {tok:>7} {chars:>7}")
    out = Path("audit/_work/sample_tokens.json")
    out.write_text(json.dumps({n: {"tokens": t, "chars": c} for n, t, c in rows}, indent=2))
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
