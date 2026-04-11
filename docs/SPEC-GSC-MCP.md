# GSC MCP — Enhancement Spec

**Source:** Tier 0 SEO audit execution (2026-04-11)
**Server:** gsc MCP
**Issue:** #2 (query row limit on `get_search_by_page_query`)

---

## Enhancement #2: Row Limit for `get_search_by_page_query`

**Priority: MEDIUM — quick win, causes silent data accuracy issues**

### The Problem

`get_search_by_page_query` returns only the top 20 queries per page. There's no way to increase this limit. This causes dramatically underreported impression and click totals.

**Real example from the audit:**

| Method | Page | Impressions |
|--------|------|-------------|
| `get_search_by_page_query` (top 20 queries) | /seo-services | 1,131 |
| `get_advanced_search_analytics` (all queries) | /seo-services | 23,169 |

That's a **20x undercount**. Any agent workflow that uses `get_search_by_page_query` to assess page performance, detect decay, or prioritise content refreshes will make wrong decisions based on incomplete data.

### Current Workaround

Use `get_advanced_search_analytics` with a dimension filter for the specific page URL. This returns accurate totals but requires the caller to know about the limitation and construct the query manually. The simpler, purpose-built tool gives wrong numbers silently.

### Proposed Changes

#### 1. Add `row_limit` parameter

```python
def get_search_by_page_query(
    page_url: str,
    date_range: str = "28d",
    row_limit: int = 20,        # NEW — default preserves backward compat
    ...
)
```

Pass this through to the Google Search Console API request body as the `rowLimit` field. The GSC API supports up to 25,000 rows per request.

#### 2. Add summary totals to response

Currently the response only contains individual query rows. Add aggregate fields:

```json
{
  "page_url": "/seo-services",
  "queries": [ ... ],
  "row_limit": 500,
  "total_rows_returned": 487,
  "summary": {
    "total_clicks": 342,
    "total_impressions": 23169,
    "average_position": 14.2,
    "average_ctr": 0.0148
  }
}
```

The `summary` fields should sum/average across ALL returned rows. This lets callers get accurate page-level totals without manual aggregation.

### Acceptance Criteria

```
GIVEN /seo-services ranks for 400+ queries in GSC (28-day window)
WHEN  get_search_by_page_query(page_url="/seo-services", row_limit=500)
THEN  response contains up to 500 query rows (not capped at 20)
  AND summary.total_impressions matches get_advanced_search_analytics result (within 5%)
  AND summary.total_clicks matches (within 5%)

GIVEN row_limit is not specified
WHEN  get_search_by_page_query(page_url="/seo-services")
THEN  behaviour is identical to current (20 rows) — backward compatible
```

### Implementation Notes

1. **GSC API mapping:** The request body field is `rowLimit` (camelCase). Set it to the value of `row_limit`. The API max is 25,000.

2. **Response size consideration:** At `row_limit=1000`, the response could be large. Consider whether the tool already has response size handling (pagination, truncation). If so, apply the same pattern. If not, 1000 rows of query data is typically under 100KB — manageable.

3. **Default value:** Keep default at 20 for backward compatibility. Callers that need accuracy can pass `row_limit=500` or `row_limit=1000`.

4. **Summary calculation:** Sum `clicks` and `impressions` across all returned rows. Average `position` weighted by impressions. Calculate `ctr` as `total_clicks / total_impressions`.
