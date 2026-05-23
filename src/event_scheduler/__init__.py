"""event_scheduler — agentic NYC event recommender.

On import, this module makes outbound HTTPS calls work on corporate networks
that run a TLS-intercepting proxy (Zscaler / Netskope / similar). The proxy
re-signs every cert with a corporate root CA that certifi doesn't know
about, so by default httpx raises CERTIFICATE_VERIFY_FAILED on every call
to Nimble / DeepSeek / Google.

We use a belt-and-suspenders fix:

1. **truststore** — delegates SSL verification to the OS trust store
   (Windows certificate store / macOS Keychain), which already has the
   corporate root CA. This is the clean path and works for `ssl`-based
   libraries that don't take a `verify=` kwarg (e.g. googleapiclient).

2. **httpx monkey-patch** — set `verify=False` as the default on every
   `httpx.Client`, `httpx.AsyncClient`, and module-level `httpx.{get,post,
   put,delete,...}` call. The corporate proxy is already inspecting every
   byte, so verification adds no real security on this network, and this
   covers the case where truststore can't find a usable CA.

If you ever ship this somewhere without a corporate proxy you can drop the
verify-disable monkey-patch; the truststore line is harmless to leave in.
"""
from __future__ import annotations

import warnings

try:
    import truststore as _truststore

    _truststore.inject_into_ssl()
except Exception:
    pass

# Silence urllib3's "Unverified HTTPS request" warning. The googleapiclient
# stack still goes through truststore above; this only affects httpx.
warnings.filterwarnings("ignore", message="Unverified HTTPS request.*")


def _disable_httpx_verify() -> None:
    try:
        import httpx
    except ImportError:
        return

    # Patch Client / AsyncClient defaults so any code constructing a client
    # without an explicit verify= argument gets verify=False.
    _orig_client_init = httpx.Client.__init__
    _orig_async_init = httpx.AsyncClient.__init__

    def _client_init(self, *args, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_client_init(self, *args, **kwargs)

    def _async_init(self, *args, **kwargs):
        kwargs.setdefault("verify", False)
        return _orig_async_init(self, *args, **kwargs)

    httpx.Client.__init__ = _client_init
    httpx.AsyncClient.__init__ = _async_init

    # Also patch module-level helpers (httpx.get / httpx.post / ...). The LLM
    # service uses these directly, not a Client instance.
    for _name in ("get", "post", "put", "delete", "request", "head",
                  "options", "patch"):
        _orig = getattr(httpx, _name, None)
        if _orig is None:
            continue

        def _make(orig=_orig):
            def _wrapped(*a, **kw):
                kw.setdefault("verify", False)
                return orig(*a, **kw)

            return _wrapped

        setattr(httpx, _name, _make())


_disable_httpx_verify()
