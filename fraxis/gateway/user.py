# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``User`` is never customised: its gateway data lives in ``Fraxis User Profile``. The User is
only observed here so that deleting it is not blocked by the profile's Link. Disabling it or
regenerating its API keys needs no hook — ``auth`` checks both on every request and refresh.

Integration Data (a JSON object on the profile) is for other apps: Fraxis never reads its keys,
it only hands them over (``profile_data``) and stores what an app caches there
(``update_profile_data``), e.g. a token of an external service the app needs for this user.
"""

import json

import frappe

PROFILE = "Fraxis User Profile"


def parse_data(value) -> dict:
    """Integration Data as a dict; raises ValueError when it is not a JSON object."""
    if value in (None, ""):
        return {}
    data = json.loads(value) if isinstance(value, str) else value
    if not isinstance(data, dict):
        raise ValueError(value)
    return data


def current() -> str | None:
    """The User the gateway authenticated for this request, None outside gateway requests."""
    return getattr(frappe.local, "fraxis_user", None)


def profile_data(key: str | None = None, user: str | None = None):
    """Integration Data of ``user`` (default: the gateway caller), or one key of it."""
    user = user or current()
    raw = frappe.db.get_value(PROFILE, user, "integration_data") if user else None
    data = parse_data(raw)
    return data if key is None else data.get(key)


def update_profile_data(values: dict, user: str | None = None) -> dict:
    """Merge ``values`` into the Integration Data of ``user`` (default: the gateway caller)
    without touching ``modified``; returns the stored object. Part of the current transaction,
    so a request that fails afterwards drops it too."""
    user = user or current()
    data = {**profile_data(user=user), **values}
    frappe.db.set_value(PROFILE, user, "integration_data", json.dumps(data, indent=1), update_modified=False)
    return data


def delete_profile(doc, method=None) -> None:
    """``User.on_trash``: its profile and refresh tokens go with it."""
    from fraxis.gateway import tokens

    tokens.delete_user_tokens(doc.name)
    frappe.delete_doc("Fraxis User Profile", doc.name, ignore_permissions=True, ignore_missing=True, force=True)
