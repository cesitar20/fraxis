# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

from frappe.model.document import Document


class FraxisProfileParam(Document):
    """Extra parameter forced on every gateway request of the user against ``ref_doctype``."""
