# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``User`` is never customised: its gateway data lives in ``Fraxis User Profile``. The User is
only observed here so that deleting it is not blocked by the profile's Link. Disabling it or
regenerating its API keys needs no hook — ``auth`` checks both on every request and refresh.
"""

import frappe


def delete_profile(doc, method=None) -> None:
    """``User.on_trash``: its profile and refresh tokens go with it."""
    from fraxis.gateway import tokens

    tokens.delete_user_tokens(doc.name)
    frappe.delete_doc("Fraxis User Profile", doc.name, ignore_permissions=True, ignore_missing=True, force=True)
