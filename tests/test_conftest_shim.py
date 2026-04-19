"""F16: conftest shim's patch-detection gate.

The autouse shim in ``conftest.py`` falls back to the monkey-patched
``get_gsc_service`` ONLY when a test actually patched it. Without the
gate, a test that forgot both manifest setup AND the patch would
silently limp through the legacy path and yield a confusing error —
hiding the real bug (missing setup).

This module pins three contracts:

1. New test with manifest → real resolver runs. (Covered extensively
   elsewhere; not repeated here.)
2. Legacy test without manifest but WITH patch → fallback runs. (Also
   covered elsewhere; implied by every test_site_crud.py test.)
3. **Test with neither manifest nor patch → real resolver runs and
   raises NO_ACCOUNTS_CONFIGURED.** THIS is the new contract F16
   introduces and must be pinned.
"""
from __future__ import annotations

import pytest

from gsc_server import gsc_get_performance_overview


class TestForgottenSetupFailsLoudly:
    async def test_no_manifest_no_patch_raises_no_accounts_configured(self):
        """A routed tool called without either a configured manifest or
        a ``get_gsc_service`` monkey-patch must surface a clean
        ``NO_ACCOUNTS_CONFIGURED`` envelope. Before F16 the shim
        deferred to the unpatched ``get_gsc_service`` which raised a
        credential-setup error that wasn't about the real bug (the
        missing test setup). Now the real resolver runs and fails
        loudly."""
        out = await gsc_get_performance_overview(
            site_url="sc-domain:example.com",
            response_format="json",
        )
        # Auto-resolution with zero configured accounts.
        assert out["ok"] is False
        assert out["error_code"] == "NO_ACCOUNTS_CONFIGURED"
        # Hint names the remediation so a dev reading a broken test
        # sees the real problem.
        assert "gsc_add_account" in out["hint"]
