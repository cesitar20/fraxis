# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from fraxis.gateway import scope, user


class FraxisUserProfile(Document):
    """Gateway access of one User, kept beside ``User`` instead of customising the core DocType."""

    def validate(self):
        self.validate_integration_data()
        for row in self.extra_params:
            if not frappe.db.exists("DocType", row.ref_doctype):
                continue  # the Link validation reports it
            meta = frappe.get_meta(row.ref_doctype)
            if meta.istable or meta.issingle:
                frappe.throw(_("Extra Params row {0}: {1} is a child table or a Single").format(row.idx, row.ref_doctype))
            row.fieldname = (row.fieldname or "").strip()
            if row.fieldname not in scope.STANDARD_FIELDS and not meta.has_field(row.fieldname):
                frappe.throw(
                    _("Extra Params row {0}: {1} has no field {2}").format(row.idx, row.ref_doctype, row.fieldname)
                )
            try:
                scope.cast(row.value, row.fieldtype)
            except ValueError:
                frappe.throw(_("Extra Params row {0}: {1} is not a valid {2}").format(row.idx, row.value, row.fieldtype))

    def validate_integration_data(self):
        """Integration Data must be a JSON object: apps read it by key (``user.profile_data``)."""
        try:
            user.parse_data(self.integration_data)
        except ValueError:
            frappe.throw(_("Integration Data must be a JSON object, e.g. {0}").format('{"token": "..."}'))
