"""
Admin blueprint package for anki-collection-server.

Exposes the Flask Blueprint ``admin_bp`` that is registered in ``server.py``
under the URL prefix controlled by the ``ADMIN_BASE_PATH`` environment variable
(default ``/admin``).

Auth scheme
-----------
All ``<base>/*`` and ``<base>/api/*`` routes are gated by a shared secret
read from the ``ADMIN_TOKEN`` environment variable at app startup.

If ``ADMIN_TOKEN`` is not set the blueprint disables itself: every request to
``<base>/*`` returns HTTP 503 with a plain-text explanation.

Token acceptance order (first match wins):
  1. ``X-Admin-Token`` request header (API / curl friendly).
  2. HTTP Basic Auth — any username, password == token (browser + ``curl -u``).
  3. ``token`` session cookie — set by POST <base>/login; allows browser
     navigation without re-sending credentials on every request.

Token comparison always uses ``hmac.compare_digest`` (constant time) to
prevent timing-side-channel oracle attacks.

What is NOT gated
-----------------
- ``POST /``      — AnkiConnect dispatch (existing, untouched)
- ``GET /health`` — liveness probe        (existing, untouched)
"""

from src.admin.routes import admin_bp  # noqa: F401 re-export

__all__ = ["admin_bp"]
