# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Gateway authentication — API Key + API Secret in exchange for tokens, and only the access
token everywhere else.

* ``POST <base_path>/auth/token`` takes the User's Frappe key pair (User > API Access >
  Generate Keys) as ``api_key``/``api_secret``, as OAuth2 client credentials
  (``client_id``/``client_secret`` in the body or ``Authorization: Basic``), and answers
  ``{"access_token", "token_type": "Bearer", "expires_in", "refresh_token", "refresh_expires_in"}``.
* ``POST <base_path>/auth/refresh`` exchanges the refresh token for a new pair (rotation, see
  :mod:`fraxis.gateway.tokens`); ``POST <base_path>/auth/revoke`` ends the session.
* Every route requires ``Authorization: Bearer <access_token>``; a key pair is refused there.

The access token is an HS256 JWT signed with the secret kept in ``Fraxis Settings``. Its
``skh`` claim is a fingerprint of the key pair it was issued for and is compared on every
request together with the user's state, so regenerating the keys, disabling the user or
unticking *API Enabled* in its ``Fraxis User Profile`` kills its tokens at once.
"""

import base64
import binascii
import hashlib
import hmac
import time
import uuid

import frappe
import jwt
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

from fraxis.gateway import config, router, tokens

ALGORITHM = "HS256"
AUDIENCE = "api"  # readable by anyone who decodes the token: never the platform's name
PROFILE = "Fraxis User Profile"


class AuthError(Exception):
    """Authentication failure; ``code`` is the RFC 6749 ``error`` value of the token endpoint."""

    def __init__(self, message: str, code: str = "invalid_client", status: int = 401):
        super().__init__(message)
        self.code = code
        self.status = status


def authorization_header() -> str:
    """The request's ``Authorization`` header, set aside by ``router.route_request``."""
    return frappe.request.environ.get(router.AUTHORIZATION_ENV) or ""


def basic_credentials() -> tuple[str, str] | None:
    scheme, _sep, value = authorization_header().partition(" ")
    if scheme.lower() != "basic" or not value.strip():
        return None
    try:
        client_id, _sep, secret = base64.b64decode(value.strip()).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return client_id, secret


def _fingerprint(api_key: str, api_secret: str) -> str:
    return hashlib.sha256(f"{api_key}:{api_secret}".encode()).hexdigest()[:32]


def _key_pair(user: str) -> tuple[str | None, str | None]:
    api_key = frappe.db.get_value("User", user, "api_key")
    return api_key, get_decrypted_password("User", user, "api_secret", raise_exception=False)


def assert_allowed(user: str) -> None:
    """The per-user gate, checked when a token is issued and on every request."""
    if not frappe.db.get_value("User", user, "enabled"):
        raise AuthError(_("User is disabled"))
    if not frappe.db.get_value(PROFILE, user, "api_enabled"):
        raise AuthError(
            _("API access is not enabled for this account"),
            code="unauthorized_client",
            status=403,
        )


def user_for_key_pair(api_key: str | None, api_secret: str | None) -> str:
    if not api_key or not api_secret:
        raise AuthError(_("api_key and api_secret are required"), code="invalid_request", status=400)
    user = frappe.db.get_value("User", {"api_key": api_key}, "name")
    stored = get_decrypted_password("User", user, "api_secret", raise_exception=False) if user else None
    if not stored or not hmac.compare_digest(stored, api_secret):
        raise AuthError(_("Invalid api_key or api_secret"))
    assert_allowed(user)
    return user


def _issue_pair(user: str, key_fingerprint: str, grant_type: str, family: str | None = None) -> tuple[dict, str]:
    """Access + refresh token response (RFC 6749 §5.1) and the name of the new refresh row."""
    secret = config.jwt_secret()
    if not secret:
        frappe.log_error(title="Fraxis Settings has no token signing secret yet: run bench migrate")
        raise AuthError(_("Service temporarily unavailable"), "server_error", 503)
    family = family or uuid.uuid4().hex
    ttl = config.get_int("access_token_ttl")
    now = int(time.time())
    claims = {
        "iss": frappe.local.site,
        "aud": AUDIENCE,
        "sub": user,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "fam": family,
        "skh": key_fingerprint,
    }
    refresh, row, refresh_ttl = tokens.create(user, family, key_fingerprint, grant_type)
    pair = {
        "access_token": jwt.encode(claims, secret, algorithm=ALGORITHM),
        "token_type": "Bearer",
        "expires_in": ttl,
        "refresh_token": refresh,
        "refresh_expires_in": refresh_ttl,
    }
    return pair, row


def issue_token(api_key: str | None, api_secret: str | None) -> dict:
    user = user_for_key_pair(api_key, api_secret)
    return _issue_pair(user, _fingerprint(api_key, api_secret), "api_key")[0]


def _current_fingerprint(user: str) -> str | None:
    api_key, api_secret = _key_pair(user)
    return _fingerprint(api_key, api_secret) if api_key and api_secret else None


def refresh_token(token: str | None) -> dict:
    """Rotate a refresh token: new pair, same family; a reused (non-active) token revokes the family."""
    if not token:
        raise AuthError(_("refresh_token is required"), "invalid_request", 400)
    row = tokens.find(token)
    if not row:
        raise AuthError(_("Invalid refresh token"), "invalid_grant", 400)
    if row.status != "Active":
        # A rotated or revoked token came back: someone else holds a copy. End the session.
        tokens.revoke_family(row.family, "Reuse detected")
        raise AuthError(_("Refresh token is no longer valid"), "invalid_grant", 400)
    if row.expires_at <= now_datetime():
        tokens.mark(row.name, "Expired")
        raise AuthError(_("Refresh token expired; request a new one with the API keys"), "invalid_grant", 400)
    try:
        assert_allowed(row.user)
    except AuthError as e:
        raise AuthError(str(e), "invalid_grant", 400)
    fingerprint = _current_fingerprint(row.user)
    if not fingerprint or not hmac.compare_digest(fingerprint, row.key_fingerprint or ""):
        tokens.revoke_family(row.family, "API keys changed")
        raise AuthError(_("Refresh token has been revoked (the API keys changed)"), "invalid_grant", 400)

    pair, new_row = _issue_pair(row.user, fingerprint, "refresh_token", family=row.family)
    tokens.mark(row.name, "Rotated", replaced_by=new_row)
    return pair


def _decode(token: str, verify_exp: bool = True) -> dict:
    secret = config.jwt_secret()
    if not secret:
        raise jwt.InvalidTokenError("no signing secret")
    return jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        audience=AUDIENCE,
        issuer=frappe.local.site,
        leeway=30,
        options={"require": ["exp", "iat", "sub", "skh"], "verify_exp": verify_exp},
    )


def revoke(token: str | None) -> None:
    """RFC 7009: end the session of a refresh or access token; unknown tokens are not an error."""
    if not token:
        return
    if row := tokens.find(token):
        tokens.revoke_family(row.family, "Revoked by client")
        return
    try:
        claims = _decode(token, verify_exp=False)
    except jwt.PyJWTError:
        return
    tokens.deny("jti", claims.get("jti"), max(int(claims["exp"]) - int(time.time()), 0))
    if claims.get("fam"):
        tokens.revoke_family(claims["fam"], "Revoked by client")


def user_for_access_token(token: str) -> str:
    try:
        claims = _decode(token)
    except jwt.ExpiredSignatureError:
        raise AuthError(_("Access token expired"))
    except jwt.PyJWTError:
        raise AuthError(_("Invalid access token"))
    if tokens.is_denied(claims.get("jti"), claims.get("fam")):
        raise AuthError(_("Access token has been revoked"))

    user = claims["sub"]
    assert_allowed(user)
    api_key, api_secret = _key_pair(user)
    if not api_key or not api_secret or not hmac.compare_digest(claims["skh"], _fingerprint(api_key, api_secret)):
        raise AuthError(_("Access token has been revoked (the API keys changed)"))
    return user


def _bearer_user() -> str:
    scheme, _sep, token = authorization_header().partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise AuthError(_("Authorization: Bearer <access_token> required; get one at POST {0}").format(
            router.gateway_path("/auth/token")
        ))
    if ":" in token:
        raise AuthError(_("api_key:api_secret is only accepted by POST {0}; send the access token it returns").format(
            router.gateway_path("/auth/token")
        ))
    return user_for_access_token(token)


def authenticate() -> None:
    """``auth_hooks`` entry. Never raises: a failure is left for ``api.dispatch`` to answer
    in the gateway's error format, and only a request marked here is ever served."""
    route = router.current()
    if not route or route.kind != "data":
        return
    try:
        user = _bearer_user()
    except AuthError as e:
        frappe.local.fraxis_auth_error = e
        return

    # frappe.set_user() resets frappe.local.form_dict; keep it, as validate_api_key_secret does.
    form_dict = frappe.local.form_dict
    frappe.set_user(user)
    frappe.local.form_dict = form_dict
    frappe.local.fraxis_user = user
