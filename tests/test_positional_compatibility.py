"""F4: positional-argument compatibility after inserting account_alias.

The review caught that inserting ``account_alias`` after ``site_url``
would have clobbered existing positional params on several tools —
``gsc_inspect_url_enhanced(site_url, page_url)``,
``gsc_manage_sitemaps(site_url, action, ...)``, etc. The fix was to
make ``account_alias`` keyword-only via a ``*`` barrier at the end of
each signature.

This test pins the contract: every routed tool still binds its
pre-v1.2.0 positional signature correctly. Inspect each signature at
collection time rather than calling the tools — we're checking
parameter kinds, not runtime behaviour.
"""
from __future__ import annotations

import inspect

import pytest

import gsc_server


# Tools that gained a keyword-only ``account_alias`` in v1.2.0 (F4).
# Every one of these must still accept its old positional form.
ROUTED_TOOLS_WITH_POSITIONAL_HISTORY = [
    # (tool_name, expected positional params including defaults)
    ("gsc_delete_site", ["site_url"]),
    ("gsc_get_search_analytics", ["site_url", "days", "dimensions", "row_limit", "response_format"]),
    ("gsc_get_site_details", ["site_url", "response_format"]),
    ("gsc_get_sitemaps", ["site_url", "response_format"]),
    ("gsc_inspect_url_enhanced", ["site_url", "page_url", "response_format"]),
    ("gsc_check_indexing_issues", ["site_url", "urls", "response_format"]),
    ("gsc_get_performance_overview", ["site_url", "days", "response_format"]),
    ("gsc_list_sitemaps_enhanced", ["site_url", "sitemap_index", "response_format"]),
    ("gsc_get_sitemap_details", ["site_url", "sitemap_url"]),
    ("gsc_submit_sitemap", ["site_url", "sitemap_url"]),
    ("gsc_delete_sitemap", ["site_url", "sitemap_url"]),
    ("gsc_manage_sitemaps", ["site_url", "action", "sitemap_url", "sitemap_index"]),
]


@pytest.mark.parametrize("tool_name,expected_positional", ROUTED_TOOLS_WITH_POSITIONAL_HISTORY)
def test_old_positional_params_still_positional(tool_name, expected_positional):
    """Every pre-v1.2.0 positional param must remain bindable positionally."""
    fn = getattr(gsc_server, tool_name)
    # MCP tool objects wrap fn; unwrap if needed.
    target = getattr(fn, "fn", fn)
    sig = inspect.signature(target)
    params = sig.parameters

    for name in expected_positional:
        assert name in params, f"{tool_name} is missing expected param {name!r}"
        kind = params[name].kind
        # POSITIONAL_OR_KEYWORD or POSITIONAL_ONLY — either is fine, both
        # accept the old call form.
        assert kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ), (
            f"{tool_name}.{name} became {kind!r}; old positional call sites "
            f"would break."
        )


@pytest.mark.parametrize("tool_name,_expected", ROUTED_TOOLS_WITH_POSITIONAL_HISTORY)
def test_account_alias_is_keyword_only(tool_name, _expected):
    """``account_alias`` must be keyword-only so adding it didn't
    clobber the positional slot previously occupied by e.g. ``page_url``
    or ``action``."""
    fn = getattr(gsc_server, tool_name)
    target = getattr(fn, "fn", fn)
    sig = inspect.signature(target)
    params = sig.parameters
    assert "account_alias" in params, f"{tool_name} missing account_alias"
    assert params["account_alias"].kind == inspect.Parameter.KEYWORD_ONLY, (
        f"{tool_name}.account_alias is {params['account_alias'].kind!r}; "
        f"must be KEYWORD_ONLY to avoid clobbering older positional params."
    )
    assert params["account_alias"].default is None, (
        f"{tool_name}.account_alias must default to None (auto-resolve)."
    )


def test_inspect_url_positional_page_url_sanity():
    """Spot check the exact regression the review flagged: if
    ``account_alias`` had been inserted after ``site_url``, this call
    would have bound ``http://x/`` to ``account_alias`` and raised a
    validation error during resolution. With keyword-only placement,
    ``page_url`` still captures the second positional."""
    fn = getattr(gsc_server, "gsc_inspect_url_enhanced")
    target = getattr(fn, "fn", fn)
    sig = inspect.signature(target)
    bound = sig.bind("sc-domain:example.com", "https://example.com/path")
    bound.apply_defaults()
    assert bound.arguments["site_url"] == "sc-domain:example.com"
    assert bound.arguments["page_url"] == "https://example.com/path"
    assert bound.arguments.get("account_alias") is None


def test_manage_sitemaps_positional_action_sanity():
    fn = getattr(gsc_server, "gsc_manage_sitemaps")
    target = getattr(fn, "fn", fn)
    sig = inspect.signature(target)
    bound = sig.bind("sc-domain:example.com", "list")
    bound.apply_defaults()
    assert bound.arguments["site_url"] == "sc-domain:example.com"
    assert bound.arguments["action"] == "list"
    assert bound.arguments.get("account_alias") is None
