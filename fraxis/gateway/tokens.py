# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Refresh tokens (opaque, stored hashed, rotated) and the revocation list of access tokens.

A login (``/auth/token``) starts a *family*: every refresh returns a new refresh token of the
same family and marks the old one ``Rotated``. Presenting a non-active refresh token again is
treated as theft and revokes its whole family (OAuth 2.1 refresh token rotation).

Access tokens are stateless JWTs carrying their ``jti`` and family (``fam``). Revoking a
family or a single access token puts it on a Redis deny list for as long as an access token
can live, which :func:`is_denied` checks on every request.
"""

import hashlib
import secrets

import frappe
from frappe.utils import add_to_date, now_datetime

from fraxis.gateway import config

DOCTYPE = "Fraxis Refresh Token"
PREFIX = "rt1."  # shown to clients: never the platform's name
# Outside config.CACHE_PREFIX: saving Fraxis Settings clears that prefix and must not forget revocations.
DENY_PREFIX = "fraxis_revoked:"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(user: str, family: str, key_fingerprint: str, grant_type: str) -> tuple[str, str, int]:
    """New active refresh token -> ``(token, row name, ttl seconds)``; only its hash is stored."""
    ttl = config.get_int("refresh_token_ttl")
    token = PREFIX + secrets.token_urlsafe(48)
    request = getattr(frappe.local, "request", None)
    row = frappe.get_doc(
        {
            "doctype": DOCTYPE,
            "user": user,
            "status": "Active",
            "family": family,
            "grant_type": grant_type,
            "expires_at": add_to_date(now_datetime(), seconds=ttl),
            "ip_address": getattr(frappe.local, "request_ip", None),
            "user_agent": (request.headers.get("User-Agent") or "")[:500] if request else None,
            "token_hash": _hash(token),
            "key_fingerprint": key_fingerprint,
        }
    ).insert(ignore_permissions=True)
    return token, row.name, ttl


def find(token) -> frappe._dict | None:
    if not isinstance(token, str) or not token.startswith(PREFIX):
        return None
    return frappe.db.get_value(
        DOCTYPE,
        {"token_hash": _hash(token)},
        ["name", "user", "family", "status", "expires_at", "key_fingerprint"],
        as_dict=True,
        for_update=True,
    )


def mark(name: str, status: str, **values) -> None:
    frappe.db.set_value(DOCTYPE, name, {"status": status, "last_used_at": now_datetime(), **values})


def revoke_family(family: str, reason: str) -> None:
    """Revoke every active refresh token of the family and the access tokens issued from it."""
    frappe.db.set_value(
        DOCTYPE, {"family": family, "status": "Active"}, {"status": "Revoked", "revoked_reason": reason}
    )
    deny("fam", family, config.get_int("access_token_ttl"))


def deny(kind: str, value: str, seconds: int) -> None:
    if value and seconds > 0:
        frappe.cache.set_value(f"{DENY_PREFIX}{kind}:{value}", 1, expires_in_sec=seconds + 60)


def is_denied(jti: str | None, family: str | None) -> bool:
    return any(
        value and frappe.cache.get_value(f"{DENY_PREFIX}{kind}:{value}")
        for kind, value in (("jti", jti), ("fam", family))
    )


def delete_user_tokens(user: str) -> None:
    frappe.db.delete(DOCTYPE, {"user": user})


def purge_expired() -> None:
    """Daily: drop refresh tokens that expired more than a week ago."""
    frappe.db.delete(DOCTYPE, {"expires_at": ["<", add_to_date(now_datetime(), days=-7)]})
