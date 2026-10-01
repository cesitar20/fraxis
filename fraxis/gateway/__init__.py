# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Fraxis REST gateway: masked routes over Frappe DocTypes, documented with Scalar.

Every public path is declared in ``Fraxis Settings > Routes``::

    <base_path>/<sub_route>[/<sub_category>][/<name>]    e.g. /fraxis/stats/records, /fraxis/campaigns

and answered by Frappe's own document API (``frappe.get_list``, ``Document.insert`` /
``save``, ``frappe.delete_doc``) on the row's DocType, so permissions, controllers and hooks
all run as usual. No method is written per route: OData query options ($filter, $select,
$orderby, $top, $skip, $count) are translated into ``get_list`` arguments by the small
helper library in :mod:`fraxis.gateway.odata`, which also shapes the responses.

Authentication: ``POST <base_path>/auth/token`` with the user's API Key + API Secret
returns a short-lived access token; every route only accepts ``Authorization: Bearer
<access_token>``. The user needs a ``Fraxis User Profile`` with *API Enabled*, whose
*Extra Params* are forced on every request against their DocType.

Pipeline per request (see ``frappe.app.application``)::

    init_request -> before_request  (router.route_request: resolve + rewrite the path)
                 -> validate_auth   (auth.authenticate as an ``auth_hooks`` entry)
                 -> frappe.api.handle(/api/v2/method/fraxis.gateway.api.<endpoint>)
"""
