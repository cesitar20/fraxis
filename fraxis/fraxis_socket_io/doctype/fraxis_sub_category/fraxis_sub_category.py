# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document


class FraxisSubCategory(Document):
    """URL segment used by the routes of ``Fraxis Settings`` (``/stats/<sub_category>``)."""

    def validate(self):
        self.sub_category = (self.sub_category or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.sub_category):
            frappe.throw(
                _("Sub Category may only contain lowercase letters, digits, '-' and '_'"),
                title=_("Invalid Sub Category"),
            )

    def on_update(self):
        # Routes and the OpenAPI document are cached from Fraxis Settings.
        frappe.cache.delete_keys("fraxis_gateway:")
