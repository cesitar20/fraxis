# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``before_request`` hook: resolves ``<base_path>/...`` and rewrites the request onto the
gateway endpoint that answers it (``/api/v2/method/fraxis.gateway.api.<endpoint>``), so
Frappe's stock router, rate limiting and commit/rollback handling still apply.

``before_request`` runs after ``make_form_dict`` and before ``validate_auth``. The
``Authorization`` header of gateway requests is moved aside here: Frappe would otherwise try
it as an OAuth token / API key and answer a Bearer JWT with its own 401 before the gateway
can check it (``auth.authenticate`` reads it from ``environ[AUTHORIZATION_ENV]``).
"""

from dataclasses import dataclass

import frappe
from frappe.utils import get_url

from fraxis.gateway import config

ENDPOINT = "/api/v2/method/fraxis.gateway.api."
AUTHORIZATION_ENV = "FRAXIS_AUTHORIZATION"
COLLECTION_VERBS = ("GET", "POST")
ITEM_VERBS = ("GET", "PATCH", "DELETE")
# Route kind -> fraxis.gateway.api endpoint; data routes and errors go to ``dispatch``.
ENDPOINTS = {"token": "token", "refresh": "refresh", "revoke": "revoke", "docs": "docs", "spec": "openapi_spec"}
STATIC = {"/auth/token": "token", "/auth/refresh": "refresh", "/auth/revoke": "revoke", "/docs": "docs", "/openapi.json": "spec"}


@dataclass
class Route:
    kind: str  # token | refresh | revoke | docs | spec | data | error
    spec: config.RouteSpec | None = None
    name: str | None = None  # document name after the route's path
    status: int = 200
    allowed: tuple[str, ...] = ()

    @property
    def endpoint(self) -> str:
        return ENDPOINTS.get(self.kind, "dispatch")


def current() -> Route | None:
    return getattr(frappe.local, "fraxis_route", None)


def gateway_path(sub: str = "") -> str:
    return config.base_path() + sub


def gateway_url(sub: str = "") -> str:
    return get_url(gateway_path(sub))


def original_path() -> str:
    request = frappe.local.request
    return request.environ.get("ORIGINAL_PATH_INFO") or request.path


def resolve(sub: str, verb: str) -> Route:
    if kind := STATIC.get(sub):
        return Route(kind)

    # The longest configured route that prefixes the path wins; what is left is the document name.
    parts = tuple(sub.strip("/").split("/"))
    exposed = config.exposed_doctypes()
    spec = max(
        (s for s in config.routes().values() if parts[: len(s.segments)] == s.segments and s.doctype in exposed),
        key=lambda s: len(s.segments),
        default=None,
    )
    if not spec:
        return Route("error", status=404)

    name = "/".join(parts[len(spec.segments) :]) or None
    allowed = tuple(v for v in (ITEM_VERBS if name else COLLECTION_VERBS) if v in spec.verbs)
    if not allowed:
        return Route("error", status=404)
    if verb not in allowed:
        return Route("error", status=405, allowed=allowed)
    return Route("data", spec=spec, name=name)


def route_request() -> None:
    # Per-request state must never leak into the next request, even when frappe.local was not
    # released in between.
    frappe.local.fraxis_route = None
    frappe.local.fraxis_user = None
    frappe.local.fraxis_auth_error = None
    config.reset_request_cache()

    request = getattr(frappe.local, "request", None)
    if not request:
        return
    prefix = config.base_path()
    if request.path != prefix and not request.path.startswith(prefix + "/"):
        return
    if not config.enabled():
        return  # "Disable Gateway": the path falls through to Frappe (404)

    route = resolve(request.path[len(prefix) :].rstrip("/") or "/", request.method)
    frappe.local.fraxis_route = route

    if authorization := request.environ.pop("HTTP_AUTHORIZATION", None):
        request.environ[AUTHORIZATION_ENV] = authorization
    target = ENDPOINT + route.endpoint
    request.environ["ORIGINAL_PATH_INFO"] = request.environ["PATH_INFO"]
    request.environ["PATH_INFO"] = target
    request.path = target
