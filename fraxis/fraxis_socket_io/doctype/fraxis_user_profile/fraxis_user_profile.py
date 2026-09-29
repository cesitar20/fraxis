# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document


class FraxisUserProfile(Document):
    """Gateway data of one User (API access, token version, free-form custom data).

    Lives beside ``User`` instead of adding Custom Fields to the core DocType.
    """

    def validate(self):
        self.custom_data = normalise_custom_data(self.custom_data)
        if not self.is_new() and self.has_value_changed("api_enabled") and not self.api_enabled:
            self.invalidate_tokens(_("Fraxis API access removed"))

    def invalidate_tokens(self, reason: str) -> None:
        """Bump the version (kills every access token) and revoke every refresh token."""
        from fraxis.gateway import tokens

        self.token_version = (self.token_version or 0) + 1
        tokens.revoke_user(self.user, reason)


def normalise_custom_data(value) -> str | None:
    """Custom Data must be a JSON object; stored pretty-printed so the form stays readable."""
    if value in (None, "", {}):
        return None
    try:
        data = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        frappe.throw(_("Custom Data must be valid JSON"), title=_("Invalid Custom Data"))
    if not isinstance(data, dict):
        frappe.throw(_("Custom Data must be a JSON object ({...})"), title=_("Invalid Custom Data"))
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
