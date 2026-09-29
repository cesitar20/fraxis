# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Gateway-owned endpoints (token lifecycle + documentation). Data access has no endpoint
here: ``/fraxis/api`` and ``/fraxis/odata`` are rewritten onto Frappe's own REST v2.

Reached through ``paths.route_request`` (``/fraxis/auth/token`` ->
``/api/v2/method/fraxis.gateway.api.token``); also callable at that raw path.
"""

import json

import frappe
from frappe import _
from frappe.auth import validate_ip_address
from frappe.rate_limiter import rate_limit
from frappe.twofactor import should_run_2fa
from werkzeug.wrappers import Response

from fraxis.gateway import config, tokens
from fraxis.gateway.odata import csdl, model
from fraxis.gateway.odata.request import service_root
from fraxis.gateway.paths import gateway_path, gateway_url

NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _json(payload, status: int = 200, headers: dict | None = None, mimetype="application/json") -> Response:
    return Response(json.dumps(payload, default=str), status=status, mimetype=mimetype, headers=headers)


def _token_response(pair: dict) -> Response:
    """RFC 6749 §5.1 body plus where to refresh / revoke it (absolute, base-path aware)."""
    pair["refresh_url"] = gateway_url("/auth/refresh")
    pair["revoke_url"] = gateway_url("/auth/revoke")
    return _json(pair, headers=NO_STORE)


def _oauth_error(code: str, description: str, status: int = 400) -> Response:
    return _json({"error": code, "error_description": description}, status, NO_STORE)


# --- token lifecycle -----------------------------------------------------------------------

def _password_grant(username: str | None, password: str | None) -> str:
    login_manager = frappe.local.login_manager
    try:
        login_manager.authenticate(user=username, pwd=password)
    except frappe.AuthenticationError:
        raise tokens.TokenError(_("Invalid username or password"))
    user = login_manager.user
    if should_run_2fa(user):
        raise tokens.TokenError(
            _("Two-factor authentication is enabled for this user; use grant_type=api_key"),
            code="unauthorized_client",
        )
    validate_ip_address(user)
    return user


def _header_key_pair() -> str | None:
    """``Authorization: Bearer <api_key>:<api_secret>``, set aside by ``paths.route_request``."""
    from fraxis.gateway.auth import is_api_key_pair

    token = frappe.request.environ.get("FRAXIS_AUTH_BEARER")
    return token if token and is_api_key_pair(token) else None


def _api_key_grant(api_key: str | None, api_secret: str | None) -> str:
    from fraxis.security.auth import AuthError, resolve_token_to_user

    pair = f"{api_key}:{api_secret}" if api_key or api_secret else _header_key_pair()
    try:
        return resolve_token_to_user(pair)
    except AuthError:
        raise tokens.TokenError(_("Invalid api_key or api_secret"))


@frappe.whitelist(allow_guest=True, xss_safe=True, methods=["POST"])
@rate_limit(limit=30, seconds=60)
def token(
    grant_type: str | None = None,
    username: str | None = None,
    password: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    refresh_token: str | None = None,
    usr: str | None = None,
    pwd: str | None = None,
):
    """OAuth2 token endpoint (RFC 6749 §4.3 / §6) plus an ``api_key`` grant for services.

    The ``api_key`` grant takes ``api_key``/``api_secret`` from the body or, as everywhere
    else in the gateway, from ``Authorization: Bearer <api_key>:<api_secret>`` — in which
    case ``grant_type`` may be omitted.
    """
    if not grant_type and not (username or usr) and _header_key_pair():
        grant_type = "api_key"
    try:
        if grant_type == "password":
            user = _password_grant(username or usr, password or pwd)
            return _token_response(tokens.issue_token_pair(user, "password"))
        if grant_type == "api_key":
            user = _api_key_grant(api_key, api_secret)
            return _token_response(tokens.issue_token_pair(user, "api_key"))
        if grant_type == "refresh_token":
            return _token_response(tokens.rotate_refresh_token(refresh_token))
    except tokens.TokenError as e:
        return _oauth_error(e.code, str(e), 401 if e.code == "unauthorized_client" else 400)
    except frappe.AuthenticationError as e:  # e.g. validate_ip_address
        return _oauth_error("unauthorized_client", str(e) or _("Not allowed"), 401)
    return _oauth_error("unsupported_grant_type", _("grant_type must be password, api_key or refresh_token"))


@frappe.whitelist(allow_guest=True, xss_safe=True, methods=["POST"])
@rate_limit(limit=60, seconds=60)
def refresh(refresh_token: str | None = None):
    try:
        return _token_response(tokens.rotate_refresh_token(refresh_token))
    except tokens.TokenError as e:
        return _oauth_error(e.code, str(e))


@frappe.whitelist(allow_guest=True, xss_safe=True, methods=["POST"])
@rate_limit(limit=60, seconds=60)
def revoke(token: str | None = None, refresh_token: str | None = None):
    """RFC 7009: always 200, even for unknown tokens."""
    tokens.revoke_refresh_token(token or refresh_token)
    return _json({"revoked": True}, headers=NO_STORE)


@frappe.whitelist(methods=["GET"])
def me():
    """The authenticated user and its ``Fraxis User Profile`` custom data."""
    state = tokens.get_user_state(frappe.session.user)
    return {
        "user": frappe.session.user,
        "full_name": frappe.utils.get_fullname(frappe.session.user),
        "user_type": state.user_type if state else None,
        "roles": frappe.get_roles(),
        "custom_data": frappe.parse_json(state.custom_data) if state and state.custom_data else {},
    }


# --- OData service documents ------------------------------------------------------------

def _readable_sets(app: str | None = None) -> dict[str, str]:
    if frappe.session.user == "Guest":
        return model.exposed_sets() if config.get("public_docs") else {}
    return model.readable_sets(app)


@frappe.whitelist(allow_guest=True, methods=["GET"])
def odata_service_document():
    root = service_root()
    sets = _readable_sets()
    return _json(
        {
            "@odata.context": f"{root}/$metadata",
            "value": [{"name": s, "kind": "EntitySet", "url": s} for s in sets],
        },
        headers={"OData-Version": "4.0"},
        mimetype="application/json;odata.metadata=minimal",
    )


@frappe.whitelist(allow_guest=True, methods=["GET"])
def odata_metadata(app: str | None = None):
    return Response(
        csdl.build(_readable_sets(app)),
        mimetype="application/xml",
        headers={"OData-Version": "4.0"},
    )


@frappe.whitelist(allow_guest=True, methods=["GET"])
def odata_service_document_v2():
    """OData V2 service document (JSON verbose)."""
    return _json({"d": {"EntitySets": list(_readable_sets())}}, headers={"DataServiceVersion": "2.0"})


@frappe.whitelist(allow_guest=True, methods=["GET"])
def odata_metadata_v2(app: str | None = None):
    """OData V2 ``$metadata`` (edmx 1.0) with the FunctionImports from Fraxis Settings."""
    return Response(
        csdl.build_v2(_readable_sets(app)),
        mimetype="application/xml",
        headers={"DataServiceVersion": "2.0"},
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def odata_error():
    ctx = frappe.local.fraxis_route.odata
    error = ctx["error"]
    if ctx.get("dialect") == "v2":
        from fraxis.gateway.odata.response_v2 import error_payload

        return _json(error_payload(error["code"], error["message"]), error["status"], {"DataServiceVersion": "2.0"})
    return _json(
        {"error": {"code": error["code"], "message": error["message"]}},
        error["status"],
        {"OData-Version": "4.0"},
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def method_not_allowed():
    """A clean route (Fraxis Settings > Routes) called with a verb it is not mapped to."""
    allowed = getattr(frappe.local, "fraxis_allowed_methods", [])
    return _json(
        {"status": 405, "error": _("Method {0} is not allowed on this route").format(frappe.request.method)},
        405,
        {"Allow": ", ".join(allowed)},
    )


@frappe.whitelist(allow_guest=True, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found():
    raise frappe.DoesNotExistError(_("Unknown Fraxis gateway path"))


# --- OpenAPI + UIs ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True, methods=["GET"])
def openapi_spec(app: str | None = None):
    from fraxis.gateway import openapi

    cache_key = f"fraxis_gateway:openapi:{gateway_url()}:{frappe.session.user}:{app or '*'}"
    spec = frappe.cache.get_value(cache_key)
    if spec is None:
        spec = openapi.build(_readable_sets(app), f" — {app}" if app else "")
        frappe.cache.set_value(cache_key, spec, expires_in_sec=300)
    return _json(spec)


def _doc_sources() -> list[dict]:
    """"All" plus one spec per app that has at least one entity set the viewer can read."""
    spec = gateway_path("/openapi.json")
    sources = [{"title": "All", "url": spec}]
    if frappe.session.user == "Guest":
        return sources
    sources += [
        {"title": app, "url": f"{spec}?app={app}"}
        for app in frappe.get_installed_apps()
        if model.readable_sets(app)
    ]
    return sources


def _login_redirect() -> Response | None:
    """The spec needs a desk session (or a JWT the browser cannot send on page load)."""
    if frappe.session.user != "Guest" or config.get("public_docs"):
        return None
    from urllib.parse import quote

    from werkzeug.utils import redirect

    from fraxis.gateway.paths import original_path

    target = original_path()
    if query := frappe.request.query_string.decode():
        target += f"?{query}"
    return redirect(f"/login?redirect-to={quote(target)}")


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fraxis API</title>
<script>
// "Try it" calls must authenticate with the JWT only. Sending the desk ``sid`` cookie would
// make Frappe enforce CSRF on POST/PATCH/DELETE before the gateway runs, so every
// gateway request except the spec itself is sent without credentials.
(function () {{
  const base = {base_json}, spec = base + "/openapi.json";
  const nativeFetch = window.fetch;
  window.fetch = function (input, init) {{
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin === location.origin && url.pathname.startsWith(base + "/") && url.pathname !== spec) {{
      if (input instanceof Request) input = new Request(input, {{ credentials: "omit" }});
      else init = Object.assign({{}}, init, {{ credentials: "omit" }});
    }}
    return nativeFetch.call(this, input, init);
  }};
}})();
</script>
</head>
<body style="margin:0">
{body}
</body>
</html>"""


@frappe.whitelist(allow_guest=True, methods=["GET"])
def docs_scalar():
    if redirect := _login_redirect():
        return redirect
    config_js = json.dumps(
        {
            "sources": _doc_sources(),
            "withDefaultFonts": False,
            "persistAuth": True,
            "authentication": {"preferredSecurityScheme": "bearerAuth"},
        }
    )
    body = (
        '<div id="app"></div>\n'
        '<script src="https://cdn.jsdelivr.net/npm/@scalar/api-reference@1"></script>\n'
        f"<script>Scalar.createApiReference('#app', {config_js})</script>"
    )
    return Response(_PAGE.format(body=body, base_json=json.dumps(gateway_path())), mimetype="text/html")
