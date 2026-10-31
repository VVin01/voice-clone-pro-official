"""Hardening batch — Family 5 (static caching, hunt F14).

The blanket no-store on /static/* made every Mini App open
re-download ~670 KB of JS/CSS. Asset URLs are versioned per release
in index.html (?v=...), and index.html itself keeps no-store, so:
  - `/` stays no-store (it is the version manifest);
  - `/static/*` becomes no-cache — revalidate via the ETag /
    Last-Modified support Starlette StaticFiles already provides,
    never blindly stale, but repeat opens get 304s instead of bytes;
  - API paths are untouched by the policy.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.webapp.server import add_no_cache_headers


async def _run(path):
    request = SimpleNamespace(url=SimpleNamespace(path=path))

    async def call_next(req):
        return SimpleNamespace(headers={})

    response = await add_no_cache_headers(request, call_next)
    return response.headers


@pytest.mark.asyncio
async def test_index_stays_no_store():
    headers = await _run("/")
    assert "no-store" in headers["Cache-Control"]
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_statics_revalidate_instead_of_no_store():
    headers = await _run("/static/js/app.js")
    assert headers["Cache-Control"] == "no-cache"
    assert "no-store" not in headers["Cache-Control"]
    assert "Pragma" not in headers
    assert headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.asyncio
async def test_api_paths_untouched():
    headers = await _run("/api/user/me")
    assert "Cache-Control" not in headers
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_assets_are_versioned_in_index():
    # The staleness argument depends on this: every asset index.html
    # references carries a ?v= release version, so a changed asset is
    # a changed URL. If a future edit drops versioning, this fails
    # and the caching policy must be revisited.
    import re
    from pathlib import Path

    html = Path("app/webapp/static/index.html").read_text(encoding="utf-8")
    refs = re.findall(r"/static/[^\"]+", html)
    assert refs, "index.html references no /static assets?"
    assert all("?v=" in r for r in refs), refs


def test_asset_versions_bumped_for_fix_release():
    # The fix release changed app.js and i18n.js heavily, so their
    # URLs must differ from the previous release's tokens
    # (20261030_release_v1 / _v2): under the no-cache policy a client
    # holding the old index.html would otherwise keep revalidating
    # the OLD asset URLs and never see the fix build. Every asset
    # now carries the fix-release token.
    import re
    from pathlib import Path

    html = Path("app/webapp/static/index.html").read_text(encoding="utf-8")
    refs = re.findall(r"/static/[^\"]+", html)
    assert refs
    for ref in refs:
        assert "20261030_release_v1" not in ref, ref
        assert "20261030_release_v2" not in ref, ref
        assert "?v=20261031_fix_v1" in ref, ref
    # The two scripts whose bytes changed must be on the new token.
    assert "/static/js/app.js?v=20261031_fix_v1" in html
    assert "/static/js/i18n.js?v=20261031_fix_v1" in html
