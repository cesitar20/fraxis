# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Access tokens (JWT, stateless) and refresh tokens (opaque, stored hashed, rotated).

Access token: HS256 JWT, short-lived. Claims::

    iss=<site>  aud="fraxis"  sub=<user>  iat/nbf/exp  jti  typ="access"
    tv=<Fraxis User Profile.token_version>  fam=<refresh family id>

``tv`` is compared against the user on every request, so bumping the version (or
disabling the user / its Fraxis access) kills every outstanding access token at once.

Refresh token: 48 random bytes, only its SHA-256 is persisted in ``Fraxis Refresh Token``.
Every use rotates it (the old row becomes ``Rotated``); presenting a non-active token again
is treated as theft and revokes its whole family (RFC 6819 §5.2.2.3 / OAuth 2.1 rotation).
"""

import hashlib
import secrets
import time
import uuid

import frappe
import jwt
from frappe.utils import add_to_date, get_datetime, now_datetime

from fraxis.gateway import config

REFRESH_DOCTYPE = "Fraxis Refresh Token"
REFRESH_PREFIX = "fxr1."


class TokenError(Exception):
    """OAuth2-style token failure. ``code`` is the RFC 6749 ``error`` value."""

    def __init__(self, message: str, code: str = "invalid_grant"):
        super().__init__(message)
        self.code = code


# --- user gate -------------------------------------------------------------------------

def get_user_state(user: str) -> frappe._dict | None:
    """User status + its ``Fraxis User Profile`` (API access, token version, custom data)."""
    from fraxis.gateway.user import get_profile

    state = frappe.db.get_value("User", user, ["name", "enabled", "user_type"], as_dict=True)
    if not state:
        return None
    profile = get_profile(user) or frappe._dict()
    state.api_enabled = profile.api_enabled or 0
    state.token_version = profile.token_version or 0
    state.custom_data = profile.custom_data
    return state


def assert_user_allowed(user: str) -> frappe._dict:
    """The single per-user gate used at issue, refresh and every request."""
    state = get_user_state(user)
    if not state or not state.enabled:
        raise TokenError("User is disabled or does not exist")
    if not state.api_enabled:
        raise TokenError(
            "Fraxis API access is not enabled for this user (Fraxis User Profile > API Enabled)",
            code="unauthorized_client",
        )
    return state


# --- access token ------------------------------------------------------------------------

def issue_access_token(user: str, token_version: int, family: str) -> tuple[str, int]:
    ttl = int(config.get("access_token_ttl"))
    now = int(time.time())
    claims = {
        "iss": config.issuer(),
        "aud": config.JWT_AUDIENCE,
        "sub": user,
        "iat": now,
        "nbf": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
        "typ": "access",
        "tv": int(token_version or 0),
        "fam": family,
    }
    return jwt.encode(claims, config.jwt_secret(), algorithm=config.JWT_ALGORITHM), ttl


def decode_access_token(token: str) -> dict:
    """Verify signature, audience, issuer and time claims. Raises ``jwt.PyJWTError``."""
    claims = jwt.decode(
        token,
        config.jwt_secret(),
        algorithms=[config.JWT_ALGORITHM],
        audience=config.JWT_AUDIENCE,
        issuer=config.issuer(),
        leeway=30,
        options={"require": ["exp", "iat", "sub", "jti"]},
    )
    if claims.get("typ") != "access":
        raise jwt.InvalidTokenError("Not an access token")
    return claims


# --- refresh token -----------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_refresh_row(user: str, family: str, grant_type: str) -> tuple[str, str, int]:
    ttl = int(config.get("refresh_token_ttl"))
    token = REFRESH_PREFIX + secrets.token_urlsafe(48)
    request = getattr(frappe.local, "request", None)
    row = frappe.get_doc(
        {
            "doctype": REFRESH_DOCTYPE,
            "user": user,
            "token_hash": _hash(token),
            "family": family,
            "status": "Active",
            "grant_type": grant_type,
            "expires_at": add_to_date(now_datetime(), seconds=ttl),
            "ip_address": getattr(frappe.local, "request_ip", None),
            "user_agent": (request.headers.get("User-Agent") or "")[:500] if request else None,
        }
    ).insert(ignore_permissions=True)
    return token, row.name, ttl


def issue_token_pair(user: str, grant_type: str, family: str | None = None) -> dict:
    """RFC 6749 §5.1 token response for an already-authenticated ``user``."""
    state = assert_user_allowed(user)
    family = family or uuid.uuid4().hex
    access, access_ttl = issue_access_token(user, state.token_version, family)
    refresh, _, refresh_ttl = _new_refresh_row(user, family, grant_type)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": access_ttl,
        "refresh_token": refresh,
        "refresh_expires_in": refresh_ttl,
    }


def _find_refresh(token: str):
    if not token or not isinstance(token, str) or not token.startswith(REFRESH_PREFIX):
        return None
    return frappe.db.get_value(
        REFRESH_DOCTYPE,
        {"token_hash": _hash(token)},
        ["name", "user", "family", "status", "expires_at"],
        as_dict=True,
        for_update=True,
    )


def revoke_family(family: str, reason: str) -> None:
    frappe.db.set_value(
        REFRESH_DOCTYPE,
        {"family": family, "status": "Active"},
        {"status": "Revoked", "revoked_reason": reason},
        update_modified=True,
    )


def revoke_user(user: str, reason: str) -> None:
    frappe.db.set_value(
        REFRESH_DOCTYPE,
        {"user": user, "status": "Active"},
        {"status": "Revoked", "revoked_reason": reason},
        update_modified=True,
    )


def rotate_refresh_token(token: str) -> dict:
    """Exchange a refresh token for a new pair; detect reuse of rotated tokens."""
    row = _find_refresh(token)
    if not row:
        raise TokenError("Invalid refresh token")

    if row.status != "Active":
        # A rotated/revoked token came back: someone holds a copy. Kill the family.
        revoke_family(row.family, "Reuse detected")
        raise TokenError("Refresh token is no longer valid")

    if get_datetime(row.expires_at) <= now_datetime():
        frappe.db.set_value(REFRESH_DOCTYPE, row.name, "status", "Expired")
        raise TokenError("Refresh token expired")

    pair = issue_token_pair(row.user, "refresh_token", family=row.family)
    new_name = frappe.db.get_value(REFRESH_DOCTYPE, {"token_hash": _hash(pair["refresh_token"])}, "name")
    frappe.db.set_value(
        REFRESH_DOCTYPE,
        row.name,
        {"status": "Rotated", "replaced_by": new_name, "last_used_at": now_datetime()},
    )
    return pair


def delete_user_tokens(user: str) -> None:
    frappe.db.delete(REFRESH_DOCTYPE, {"user": user})


def revoke_refresh_token(token: str) -> None:
    """RFC 7009: revoking an unknown token is not an error. Revokes the whole family."""
    row = _find_refresh(token)
    if row:
        revoke_family(row.family, "Revoked by client")


def purge_expired() -> None:
    """Daily: drop refresh-token rows that expired more than a week ago."""
    frappe.db.delete(REFRESH_DOCTYPE, {"expires_at": ["<", add_to_date(now_datetime(), days=-7)]})
