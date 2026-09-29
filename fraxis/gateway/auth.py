# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``auth_hooks`` entry: authenticates gateway routes from the ``Authorization: Bearer`` header.

Two bearer credentials are accepted:

* ``Bearer <api_key>:<api_secret>`` — the Frappe API key pair of the User, sent as is
  (same convention as Dendriva's OpenAI-compatible endpoint). The signed ``fxsig1.…``
  token minted by ``fraxis.security.auth.make_signed_token`` is accepted too.
* ``Bearer <jwt>`` — an access token issued by ``/fraxis/auth/token``.

Frappe's ``validate_auth`` first tries OAuth / API keys — neither matches a ``Bearer``
key pair or a JWT, both are no-ops for them — then runs ``auth_hooks``. Here the
credential is verified and the request user is set with ``frappe.set_user``, exactly what
Frappe does for its own API keys, so everything downstream (``is_whitelisted``, DocPerm,
User Permissions, field-level permissions) runs as that user. Non-gateway requests are
untouched.
"""

import frappe
import jwt
from frappe import _

from fraxis.gateway import config, paths, tokens
from fraxis.security.auth import SIGNED_TOKEN_PREFIX, AuthError, resolve_token_to_user


def bearer_token() -> str | None:
    header = frappe.get_request_header("Authorization") or ""
    scheme, _sep, token = header.partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def is_api_key_pair(token: str) -> bool:
    """``key:secret`` or a signed ``fxsig1.`` token; a JWT never contains ``:``."""
    return ":" in token or token.startswith(SIGNED_TOKEN_PREFIX)


def _fail(message: str) -> None:
    frappe.local.fraxis_auth_error = message
    raise frappe.AuthenticationError(message)


def _user_from_api_key(token: str) -> str:
    try:
        user = resolve_token_to_user(token)
    except AuthError:
        _fail(_("Invalid api_key:api_secret"))
    try:
        tokens.assert_user_allowed(user)
    except tokens.TokenError as e:
        _fail(str(e))
    return user


def _user_from_jwt(token: str) -> str:
    try:
        claims = tokens.decode_access_token(token)
    except jwt.ExpiredSignatureError:
        _fail(_("Access token expired"))
    except jwt.PyJWTError:
        _fail(_("Invalid access token"))

    user = claims["sub"]
    try:
        state = tokens.assert_user_allowed(user)
    except tokens.TokenError as e:
        _fail(str(e))

    if int(claims.get("tv") or 0) != int(state.token_version or 0):
        _fail(_("Access token has been revoked"))

    frappe.local.fraxis_claims = claims
    return user


def validate_bearer() -> None:
    route = paths.current_route()
    if not route or route.auth == "public":
        return

    token = bearer_token()
    if not token:
        if route.auth == "session" and (
            frappe.session.user != "Guest" or (route.kind == "spec" and config.get("public_docs"))
        ):
            return
        _fail(_("Authorization: Bearer <api_key>:<api_secret> (or a Fraxis access token) required"))

    user = _user_from_api_key(token) if is_api_key_pair(token) else _user_from_jwt(token)

    # frappe.set_user() resets frappe.local.form_dict; keep the request arguments (and the
    # OData translation already written into it), as frappe.auth.validate_api_key_secret does.
    form_dict = frappe.local.form_dict
    frappe.set_user(user)
    frappe.local.form_dict = form_dict
