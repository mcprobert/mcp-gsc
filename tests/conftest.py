"""Pytest configuration: make the repo root importable so `import gsc_server` works."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

# Capture the unpatched ``get_gsc_service`` once at conftest load.
# The autouse shim uses identity comparison against this to decide
# whether a given test has monkey-patched the symbol. This is the
# cheaper/robuster alternative to asking pytest for the monkeypatch
# state, and works whether tests use ``monkeypatch.setattr`` or a
# plain ``gsc_server.get_gsc_service = ...`` assignment.
import gsc_server as _gsc_for_capture

_ORIGINAL_GET_GSC_SERVICE = _gsc_for_capture.get_gsc_service


@pytest.fixture(autouse=True)
def _resolver_legacy_shim(monkeypatch, tmp_path):
    """Isolation + gated back-compat for v1.2.0 tests.

    Two responsibilities, split by a patch-detection gate:

    1. **Isolation (always).** Redirect every manifest / token path
       constant at ``tmp_path``, skip the one-shot legacy-state
       migration, and clear the resolver's in-memory caches between
       tests. Without this the dev machine's real ``accounts/``
       directory would leak into test expectations.
    2. **Legacy-shim fallback (only when the test has patched
       ``gsc_server.get_gsc_service``).** Pre-v1.2.0 tests patch that
       symbol directly; the shim defers to it when no manifest is
       configured so those tests keep passing without rewrite.

    Gating on the patch matters: a new test that forgets BOTH to
    configure a manifest AND to patch ``get_gsc_service`` now falls
    through to the real resolver, which raises
    ``NO_ACCOUNTS_CONFIGURED`` — a loud, useful failure. Previously
    the shim would defer to the unpatched ``get_gsc_service`` which
    raised a confusing OAuth setup error, or worse, went looking for
    the dev machine's credentials.

    Not a production safety risk: prod manifests are always populated,
    so the shim's fallback branch never runs in prod.
    """
    import gsc_server

    # Isolation: redirect every account / token path constant at tmp_path.
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir()
    monkeypatch.setattr(gsc_server, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(gsc_server, "ACCOUNTS_DIR", str(accounts_dir))
    monkeypatch.setattr(
        gsc_server, "ACCOUNTS_MANIFEST", str(accounts_dir / "accounts.json"),
    )
    monkeypatch.setattr(gsc_server, "TOKEN_FILE", str(tmp_path / "token.json"))
    # Skip the one-shot legacy-token migration in tests.
    monkeypatch.setattr(gsc_server, "_migration_checked", True)
    monkeypatch.setattr(gsc_server, "_active_account", None)
    # Cache reset so one test's state can't leak into the next.
    gsc_server._account_property_state.clear()
    gsc_server._account_properties.clear()
    gsc_server._account_property_error.clear()
    gsc_server._account_property_refreshed_at.clear()
    gsc_server._alias_locks.clear()

    original = gsc_server.get_gsc_service_for_site

    async def _shim(site_url, account_alias):
        manifest = gsc_server._load_manifest()
        if manifest.get("accounts"):
            # New-style test (manifest configured) → real resolver.
            return await original(site_url, account_alias)
        # No manifest. Fall through to the monkey-patched
        # ``get_gsc_service`` ONLY when the test actually patched it.
        # Otherwise let the real resolver raise NO_ACCOUNTS_CONFIGURED
        # so a forgotten-setup test fails loudly instead of limping
        # through the legacy path.
        if gsc_server.get_gsc_service is _ORIGINAL_GET_GSC_SERVICE:
            return await original(site_url, account_alias)
        service = gsc_server.get_gsc_service()
        return ("test", service)

    monkeypatch.setattr(gsc_server, "get_gsc_service_for_site", _shim)
    yield
