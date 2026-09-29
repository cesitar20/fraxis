# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Gateway settings, read from the ``Fraxis Settings`` Single DocType (database only — nothing
in ``site_config.json`` or any other file on disk).

``frappe.get_cached_doc`` keeps the Single in Frappe's request/Redis cache and Frappe clears
it on save, so reading it from ``before_request`` on every request is cheap. Until the
DocType is migrated onto a site, the defaults below apply and the gateway keeps working.
"""

import re

import frappe
from frappe import _

SETTINGS = "Fraxis Settings"
JWT_ALGORITHM = "HS256"
JWT_AUDIENCE = "fraxis"

DEFAULT_BASE_PATH = "/fraxis"
# Frappe's own top-level routes: a gateway prefix here would shadow or be shadowed by them.
RESERVED_PREFIXES = ("/api", "/app", "/assets", "/files", "/private", "/backups", "/socket.io", "/login", "/desk")

DEFAULTS = {
    "disable_gateway": 0,
    "base_path": DEFAULT_BASE_PATH,
    "public_docs": 0,
    "access_token_ttl": 15 * 60,
    "refresh_token_ttl": 30 * 24 * 60 * 60,
    "odata_page_size": 100,
    "odata_max_page_size": 1000,
    "odata_apps": None,
    "odata_include_doctypes": [],
    "odata_exclude_doctypes": [],
    "odata_functions": [],
    "routes": {},
}

# Sub-paths the gateway itself owns under the base path; a clean route cannot take them.
GATEWAY_OWNED_PATHS = ("/auth", "/api", "/odata", "/docs", "/openapi.json")


INT_KEYS = ("access_token_ttl", "refresh_token_ttl", "odata_page_size", "odata_max_page_size")
_MISSING = object()


def _settings():
    """``Fraxis Settings`` for this request (memoised: ``before_request`` runs on every request)."""
    settings = getattr(frappe.local, "fraxis_settings", _MISSING)
    if settings is _MISSING:
        try:
            settings = frappe.get_cached_doc(SETTINGS)
        except Exception:
            settings = None  # DocType not migrated on this site yet
        frappe.local.fraxis_settings = settings
    return settings


def reset_request_cache() -> None:
    frappe.local.fraxis_settings = _MISSING


def get(key: str):
    settings = _settings()
    value = settings.get(key) if settings else None

    if key in ("odata_include_doctypes", "odata_exclude_doctypes"):
        return [row.ref_doctype for row in value or [] if row.ref_doctype]
    if key == "odata_functions":
        return [
            {"name": r.function_name, "method": r.method, "http_method": r.http_method or "GET", "return_type": r.return_type or "Edm.String"}
            for r in value or []
            if r.function_name and r.method
        ]
    if key == "routes":
        routes = {}
        for r in value or []:
            path, error = normalise_route_path(r.path)
            if not error and r.method:
                verb = r.http_method or "GET"
                routes.setdefault(path, {})[verb] = {
                    "path": path, "method": r.method, "http_method": verb, "description": r.description or "",
                }
        return routes  # path -> {HTTP verb -> route}
    if key == "odata_apps":
        apps = [line.strip() for line in (value or "").splitlines() if line.strip()]
        return apps or None
    if key in INT_KEYS:
        # A Single that was never saved loads Int fields as 0.
        return value if (value or 0) > 0 else DEFAULTS[key]
    if value in (None, ""):
        return DEFAULTS[key]
    return value


def normalise_base_path(raw) -> tuple[str, str | None]:
    """``(path, error)``: ``/segment[/segment]`` without trailing slash, or an error message."""
    path = "/" + str(raw or "").strip().strip("/")
    if path == "/":
        return path, _("Base Path cannot be empty")
    if any(ch in path for ch in " ?#%"):
        return path, _("Base Path cannot contain spaces, ?, # or %")
    if any(path == r or path.startswith(r + "/") for r in RESERVED_PREFIXES):
        return path, _("Base Path {0} collides with a Frappe route").format(path)
    return path, None


def normalise_route_path(raw) -> tuple[str, str | None]:
    """``(path, error)`` for a clean route: ``/segment[/segment…]``, not owned by the gateway."""
    path = "/" + str(raw or "").strip().strip("/")
    if path == "/":
        return path, _("Route Path cannot be empty")
    if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", path):
        return path, _("Route Path {0} may only contain letters, digits, '.', '_', '~', '-' and '/'").format(path)
    if any(path == p or path.startswith(p + "/") for p in GATEWAY_OWNED_PATHS):
        return path, _("Route Path {0} collides with a gateway route").format(path)
    return path, None


def enabled() -> bool:
    # "Disable" (not "Enable") so that an unsaved Single — Check fields load as 0 — is on.
    return not get("disable_gateway")


def base_path() -> str:
    """Configured URL prefix; an invalid stored value falls back to ``/fraxis``.

    ``before_request`` runs on every request of the site, so a bad value must never raise.
    """
    path, error = normalise_base_path(get("base_path"))
    return DEFAULT_BASE_PATH if error else path


def jwt_secret() -> str:
    """Signing secret stored (encrypted) in ``Fraxis Settings``; generated on first save."""
    settings = _settings()
    secret = settings.get_password("jwt_secret", raise_exception=False) if settings else None
    if not secret:
        frappe.throw(
            _("Fraxis Settings has no JWT secret yet: run bench migrate (or save Fraxis Settings once)."),
            frappe.AuthenticationError,
        )
    return secret


def issuer() -> str:
    return frappe.local.site


def ensure_settings() -> None:
    """``after_install`` / ``after_migrate``: persist defaults and generate the JWT secret."""
    if not frappe.db.exists("DocType", SETTINGS):
        return
    settings = frappe.get_single(SETTINGS)
    if not settings.get_password("jwt_secret", raise_exception=False):
        settings.base_path = settings.base_path or DEFAULT_BASE_PATH
        settings.save(ignore_permissions=True)  # validate() fills defaults and generates the secret
