# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``before_request`` path resolver: maps ``<base_path>/...`` (``/fraxis`` by default, see
``config.base_path``) onto Frappe's ``/api/v2/...``.

Same mechanism as ``dendriva.utils.path_resolver.rewrite_chat_completions_path``: the
original path is kept in ``environ["ORIGINAL_PATH_INFO"]`` and ``PATH_INFO`` /
``request.path`` are replaced before ``frappe.app.application`` dispatches, so the
request is served by the stock ``frappe.api.handle`` router.

``before_request`` runs after ``make_form_dict`` and before ``validate_auth``, which is
what lets the OData translation rewrite ``frappe.form_dict`` here and lets the JWT
``auth_hooks`` entry see which gateway route was matched.
"""

from dataclasses import dataclass, field

import frappe
from frappe.utils import get_url

from fraxis.gateway import config

API_V2 = "/api/v2"
GATEWAY_METHOD = f"{API_V2}/method/fraxis.gateway.api"
NOT_FOUND = f"{API_V2}/method/fraxis.gateway.api.not_found"


@dataclass
class Route:
    kind: str  # auth | docs | spec | api | odata
    target: str
    # "public": no auth. "session": desk session cookie or a Bearer credential.
    # "jwt": a Bearer credential only (api_key:api_secret or a gateway JWT).
    auth: str = "jwt"
    odata: dict = field(default_factory=dict)


def gateway_path(sub: str = "") -> str:
    """``<base_path>`` + ``sub`` (base path from Fraxis Settings, ``/fraxis`` by default)."""
    return config.base_path() + sub


def gateway_url(sub: str = "") -> str:
    """Absolute URL of a gateway route for the current host."""
    return get_url(gateway_path(sub))


def current_route() -> Route | None:
    return getattr(frappe.local, "fraxis_route", None)


def original_path() -> str:
    request = frappe.local.request
    return request.environ.get("ORIGINAL_PATH_INFO") or request.path


def _static_routes() -> dict[str, Route]:
    return {
        "/auth/token": Route("auth", f"{GATEWAY_METHOD}.token", auth="public"),
        "/auth/refresh": Route("auth", f"{GATEWAY_METHOD}.refresh", auth="public"),
        "/auth/revoke": Route("auth", f"{GATEWAY_METHOD}.revoke", auth="public"),
        "/auth/me": Route("api", f"{GATEWAY_METHOD}.me"),
        "/docs": Route("docs", f"{GATEWAY_METHOD}.docs_scalar", auth="public"),
        "/docs/scalar": Route("docs", f"{GATEWAY_METHOD}.docs_scalar", auth="public"),
        "/openapi.json": Route("spec", f"{GATEWAY_METHOD}.openapi_spec", auth="session"),
        "/odata": Route("odata", f"{GATEWAY_METHOD}.odata_service_document", auth="session"),
        "/odata/$metadata": Route("spec", f"{GATEWAY_METHOD}.odata_metadata", auth="session"),
        "/odata/v2": Route("spec", f"{GATEWAY_METHOD}.odata_service_document_v2", auth="session"),
        "/odata/v2/$metadata": Route("spec", f"{GATEWAY_METHOD}.odata_metadata_v2", auth="session"),
    }


def resolve(path: str) -> Route:
    sub = path[len(config.base_path()) :].rstrip("/") or "/"

    if route := _static_routes().get(sub):
        return route

    if verbs := config.get("routes").get(sub):
        # Clean route from Fraxis Settings > Routes, answered by a whitelisted method.
        verb = frappe.local.request.method
        if entry := verbs.get(verb):
            return Route("api", f"{API_V2}/method/{entry['method']}")
        frappe.local.fraxis_allowed_methods = sorted(verbs)
        return Route("api", f"{GATEWAY_METHOD}.method_not_allowed")

    if sub.startswith("/api/"):
        return Route("api", API_V2 + sub[len("/api"):])

    if sub.startswith("/odata/"):
        from fraxis.gateway.odata.request import translate

        if sub.startswith("/odata/v2/"):
            return translate(sub[len("/odata/v2/") :], dialect="v2")
        return translate(sub[len("/odata/"):])

    return Route("api", NOT_FOUND)  # authenticated: a valid caller gets 404, others 401


def route_request() -> None:
    # Per-request state must never leak into the next request, even when frappe.local was not
    # released in between (auth.validate_bearer / config read it).
    frappe.local.fraxis_route = None
    config.reset_request_cache()

    request = getattr(frappe.local, "request", None)
    if not request:
        return

    path = request.path
    prefix = config.base_path()
    if path != prefix and not path.startswith(prefix + "/"):
        return
    if not config.enabled():
        return  # "Disable Gateway" in Fraxis Settings: the path falls through to Frappe (404)

    route = resolve(path)
    frappe.local.fraxis_route = route
    authorization = request.environ.get("HTTP_AUTHORIZATION", "")
    if route.kind == "auth" and authorization:
        # Token endpoints authenticate from the body or a Bearer key pair, never through
        # Frappe's validate_auth, which would reject both before the endpoint runs:
        # * Basic = OAuth2 client_id:client_secret (Scalar "password" flow), read by
        #   Frappe as api_key:api_secret; the gateway has no OAuth clients, so it is dropped.
        # * Bearer <api_key>:<api_secret> = a Guest request with a 2-part header -> 401. It is
        #   moved aside for fraxis.gateway.api.token (grant_type=api_key).
        request.environ.pop("HTTP_AUTHORIZATION")
        if authorization.lower().startswith("bearer "):
            request.environ["FRAXIS_AUTH_BEARER"] = authorization[len("bearer ") :].strip()
    request.environ["ORIGINAL_PATH_INFO"] = request.environ["PATH_INFO"]
    # WSGI carries PATH_INFO as latin-1-decoded bytes; werkzeug re-decodes it as UTF-8 when
    # routing, so non-ASCII DocType names / document keys must be stored in that form.
    request.environ["PATH_INFO"] = route.target.encode("utf-8").decode("latin-1")
    request.path = route.target
