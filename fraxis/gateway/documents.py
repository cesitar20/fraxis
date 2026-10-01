# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
Data operations of a route, run as the authenticated user through Frappe's own document API
— the same calls ``/api/v2/document`` makes — so DocPerms, User Permissions, controllers and
hooks apply unchanged:

    GET    /<sub_route>/<sub_category>          frappe.get_list          -> collection
    POST   /<sub_route>/<sub_category>          Document.insert          -> 201 document
    GET    /<sub_route>/<sub_category>/<name>   frappe.get_doc (read)    -> document
    PATCH  /<sub_route>/<sub_category>/<name>   Document.save            -> document
    DELETE /<sub_route>/<sub_category>/<name>   frappe.delete_doc        -> 204

The caller's Extra Params (:mod:`fraxis.gateway.scope`) are applied before each call and
only properties of the entity model (:mod:`fraxis.gateway.odata.model`) go in or out.

Apps can adjust what a DocType's routes return with the ``fraxis_response_rows`` hook
(``{"<DocType>": "dotted.path"}`` in their hooks.py): the function gets the doctype and the rows
about to be answered — Frappe rows under fieldnames, child tables included for a single
document — and changes them in place, e.g. to hide an internal part of a value.

A document created here carries ``doc.flags.gateway_fields``: the fieldnames the request set
(after Extra Params). Controllers can tell a gateway insert apart from any other, and a field
the client left out (filled by its DocType default) from one it sent.
"""

from urllib.parse import urlencode

import frappe

from fraxis.gateway import config, lookup, router, scope
from fraxis.gateway.odata import ODataError, model, query, serialize


def run(route: router.Route):
    entity = model.entity(route.spec.doctype)
    verb = frappe.request.method
    if route.name is None:
        return list_documents(route, entity) if verb == "GET" else create_document(route, entity)
    if verb == "GET":
        return read_document(route, entity)
    if verb == "PATCH":
        return update_document(route, entity)
    return delete_document(route, entity)


def _body():
    return frappe.request.get_json(silent=True) if frappe.request.data else {}


def _get_in_scope(entity: model.Entity, name: str, for_update: bool = False):
    doc = frappe.get_doc(entity.doctype, name, for_update=for_update)
    # Before permissions: a document outside the caller's scope is simply not there.
    scope.assert_in_scope(doc)
    if not lookup.allowed(entity, doc):
        raise frappe.DoesNotExistError(f"{entity.doctype} {name} not found")
    return doc


def _parse_body(entity: model.Entity) -> dict:
    """Request body -> Frappe values, lookup fields turned back into their internal values."""
    data = serialize.parse_body(entity, _body())
    for fieldname, prop in entity.props.items():
        if prop.lookup and fieldname in data:
            data[fieldname] = lookup.to_internal_one(prop, data[fieldname])
    return data


def _check_if_match(doc) -> None:
    header = frappe.request.headers.get("If-Match")
    if header and header.strip() != "*" and serialize.etag(doc.modified) not in [t.strip() for t in header.split(",")]:
        raise ODataError("The document was modified by someone else (ETag mismatch)", 412, "PreconditionFailed")


def list_documents(route: router.Route, entity: model.Entity):
    args = frappe.request.args
    q = query.parse_list(entity, args, config.get_int("page_size"), config.get_int("max_page_size"))
    filters = q.filters + scope.list_filters(entity.doctype) + lookup.restrictions(entity)

    rows = frappe.get_list(
        entity.doctype,
        fields=q.fields,
        filters=filters,
        or_filters=q.or_filters,
        order_by=q.order_by,
        limit_start=q.skip,
        limit_page_length=q.top,
    )
    payload = {}
    if q.count:
        total = frappe.get_list(
            entity.doctype,
            fields=[f"count(`tab{entity.doctype}`.`name`) as total"],
            filters=filters,
            or_filters=q.or_filters,
            order_by=None,
        )
        payload["@odata.count"] = total[0].total if total else 0
    _adjust_rows(entity.doctype, rows)
    payload["value"] = lookup.publish(entity, [serialize.row(entity.props, r) for r in rows])
    if len(rows) == q.top:
        next_args = {**args.to_dict(), "$skip": q.skip + q.top, "$top": q.top}
        payload["@odata.nextLink"] = f"{router.gateway_url(route.spec.path)}?{urlencode(next_args)}"
    return serialize.json_response(payload)


def read_document(route: router.Route, entity: model.Entity):
    args = frappe.request.args
    query.check_options(args, query.ITEM_OPTIONS)
    selected = query.select(entity, args.get("$select"), allow_collections=True)
    doc = _get_in_scope(entity, route.name)
    doc.check_permission("read")
    return _document_response(entity, doc, select=selected)


def create_document(route: router.Route, entity: model.Entity):
    data = _parse_body(entity)
    scope.apply_to_new(entity.doctype, data)
    doc = frappe.get_doc({**data, "doctype": entity.doctype})
    doc.flags.gateway_fields = frozenset(data)
    doc.insert()
    location = router.gateway_url(f"{route.spec.path}/{doc.name}")
    if route.spec.verbs["POST"].get("id_only"):
        # Create Response = ID Only: the new ID under its public name (e.g. campaign_id).
        return serialize.json_response({entity.public("name"): doc.name}, 201, {"Location": location})
    return _document_response(entity, doc, 201, {"Location": location})


def update_document(route: router.Route, entity: model.Entity):
    doc = _get_in_scope(entity, route.name, for_update=True)
    _check_if_match(doc)
    data = _parse_body(entity)
    scope.check_update(entity.doctype, data)
    doc.update(data)
    doc.save()
    return _document_response(entity, doc)


def delete_document(route: router.Route, entity: model.Entity):
    doc = _get_in_scope(entity, route.name)
    _check_if_match(doc)
    frappe.delete_doc(entity.doctype, doc.name)
    return serialize.empty_response()


def _adjust_rows(doctype: str, rows: list) -> None:
    """Run the apps' ``fraxis_response_rows`` hooks for ``doctype`` on the rows to answer."""
    for method in frappe.get_hooks("fraxis_response_rows", {}).get(doctype, []):
        frappe.get_attr(method)(doctype, rows)


def _document_response(entity, doc, status: int = 200, headers: dict | None = None, select=None):
    doc.apply_fieldlevel_read_permissions()
    data = doc.as_dict()
    _adjust_rows(entity.doctype, [data])
    payload = lookup.publish(entity, [serialize.document(entity, doc, select, data)])[0]
    etag = payload.get("@odata.etag")
    return serialize.json_response(payload, status, {**({"ETag": etag} if etag else {}), **(headers or {})})
