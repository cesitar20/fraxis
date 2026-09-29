# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Gateway data per user lives in ``Fraxis User Profile`` (one row per User, named after it),
never in the core ``User`` DocType: no Custom Fields, no Property Setters, no
``override_doctype_class``.

``User`` is only *observed* through ``doc_events`` (hooks.py) so that credential changes
made in the desk still revoke gateway tokens:

* password changed or user disabled -> bump the profile's token version + revoke refresh tokens
* user deleted                      -> delete its profile and refresh tokens
"""

import frappe

PROFILE = "Fraxis User Profile"


def get_profile(user: str) -> frappe._dict | None:
    return frappe.db.get_value(PROFILE, user, ["name", "api_enabled", "token_version", "custom_data"], as_dict=True)


def invalidate_tokens_on_change(doc, method=None) -> None:
    """``User.validate``: a new password or disabling the user revokes every gateway token."""
    if doc.is_new() or not frappe.db.exists(PROFILE, doc.name):
        return

    password_changed = bool(getattr(doc, "_User__new_password", None))
    disabled = doc.has_value_changed("enabled") and not doc.enabled
    if password_changed or disabled:
        profile = frappe.get_doc(PROFILE, doc.name)
        profile.invalidate_tokens("User password changed" if password_changed else "User disabled")
        profile.save(ignore_permissions=True)


def delete_profile(doc, method=None) -> None:
    """``User.on_trash``: the profile and tokens go with the user (the Link would block it)."""
    from fraxis.gateway import tokens

    tokens.delete_user_tokens(doc.name)
    frappe.delete_doc(PROFILE, doc.name, ignore_permissions=True, ignore_missing=True, force=True)
