# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OpenAPI 3.0 document rendered by Scalar at ``<base_path>/docs``.

* ``Authentication`` — token, refresh and revoke with request / response examples.
* Every route answers with a whole example record (lists: a page of one), built from the same
  property examples as the schemas, so Scalar shows what comes back next to each request.
* One folder per Sub Route (Stats, Assistants, Numbers, Campaigns) with a sub-folder per Sub Category
  (``x-tagGroups``: group = Sub Route, tag = Sub Category) holding the routes of exposed
  DocTypes. Schemas and filter parameters come from the same entity model the routes serve,
  under the public names of Field Mappings, so excluded fields never appear.
* Every text goes through ``_()`` in the language of the request (``openapi.json?lang=``):
  static texts are translated in ``fraxis/translations``, and names typed in Fraxis Settings
  (public names, operation names, descriptions) through Frappe's Translation DocType.
* No parameter carries an ``example``: Scalar copies examples into the URL of its generated
  requests, which then filter or select what the caller did not ask for.
"""

import json
import re
from urllib.parse import urlencode

import frappe
from frappe import _

from fraxis import __version__
from fraxis.gateway import config, router
from fraxis.gateway.odata import model, query, serialize

# Public title of the docs: the product's name only, nothing of the platform behind the API.
DOCS_TITLE = "Go4Clients AI API Documentation"
INT_TYPES = ("Int", "Long Int", "Duration")
FLOAT_TYPES = ("Float", "Currency", "Percent", "Rating")
ERROR_EXAMPLES = {
    "400": ("BadRequest", "Unknown property in $filter: 'foo'"),
    "401": ("Unauthorized", "Access token expired"),
    "403": ("Forbidden", "Not permitted"),
    "404": ("NotFound", "Record not found"),
    "405": ("MethodNotAllowed", "DELETE is not allowed on this route"),
    "409": ("Conflict", "Record already exists"),
    "412": ("PreconditionFailed", "The document was modified by someone else (ETag mismatch)"),
}


def _error_text(code: str) -> str:
    return {
        "400": _("Invalid query option, property or value"),
        "401": _("Missing, invalid, expired or revoked access token"),
        "403": _("Your access does not allow it"),
        "404": _("No such record, or outside your access"),
        "405": _("HTTP method not available on this route"),
        "409": _("Duplicate record"),
        "412": _("If-Match does not match the current ETag"),
    }[code]


def _ref(name: str, kind: str = "schemas") -> dict:
    return {"$ref": f"#/components/{kind}/{name}"}


def _errors(*codes: str) -> dict:
    return {code: _ref(f"Error{code}", "responses") for code in codes}


# --- schemas ------------------------------------------------------------------------------

def _type_schema(p: model.Prop) -> dict:
    if p.fieldtype in INT_TYPES:
        return {"type": "integer"}
    if p.fieldtype in FLOAT_TYPES:
        return {"type": "number"}
    if p.fieldtype == "Check":
        return {"type": "boolean"}
    if p.fieldtype == "Date":
        return {"type": "string", "format": "date"}
    if p.fieldtype == "Datetime":
        return {"type": "string", "format": "date-time"}
    if p.fieldtype == "JSON":
        return {}
    if p.fieldtype == "Select" and p.options and (choices := [c for c in p.options.split("\n") if c]):
        return {"type": "string", "enum": choices}
    return {"type": "string"}


# Shown when Field Mappings sets no Example for the field.
TYPE_EXAMPLES = {
    "Check": True, "Int": 10, "Long Int": 1000, "Duration": 90, "Float": 12.5, "Currency": 12.5,
    "Percent": 50, "Rating": 0.8, "Date": "2026-09-01", "Datetime": "2026-09-01T14:30:00-05:00", "Time": "14:30:00",
}
DATA_EXAMPLES = {"Email": "user@example.com", "Phone": "+573001234567", "URL": "https://example.com"}


def _example(p: model.Prop):
    """The field's Example (Field Mappings), typed like the field, else one based on its type."""
    if p.example:
        try:
            if p.fieldtype == "JSON":
                return json.loads(p.example)
            if p.fieldtype == "Check":
                return p.example.strip().lower() in ("1", "true", "yes")
            if p.fieldtype in INT_TYPES:
                return int(p.example)
            if p.fieldtype in FLOAT_TYPES:
                return float(p.example)
        except ValueError:
            pass
        return p.example
    if p.fieldtype == "Select" and p.options:
        return next((c for c in p.options.split("\n") if c), None)
    if p.fieldtype == "Data" and p.options in DATA_EXAMPLES:
        return DATA_EXAMPLES[p.options]
    return TYPE_EXAMPLES.get(p.fieldtype)


def _param_example(param: query.FieldParam):
    """Example of a filter value: a date alone for ranges, true/false as text."""
    if param.prop.example:
        return param.prop.example
    if param.prop.fieldtype in ("Date", "Datetime") and param.operator != "=":
        return {"day": "2026-09-23", ">=": "2026-09-01"}.get(param.operator, "2026-09-30")
    value = _example(param.prop)
    return str(value).lower() if isinstance(value, bool) else value


def _label(p: model.Prop) -> str:
    """Title of a property: a lookup field is named after its public name, never its own label."""
    if p.lookup:
        return _(p.public.replace("_", " ").capitalize())
    return _(p.label or p.public)


def _prop_schema(p: model.Prop) -> dict:
    schema = _type_schema(p)
    if (example := _example(p)) is not None:
        schema["example"] = example
    if p.lookup or (p.label and p.label != p.name):
        schema["title"] = _label(p)
    if p.description:
        schema["description"] = _(p.description)
    if p.read_only:
        schema["readOnly"] = True
    if not p.required and "type" in schema:
        schema["nullable"] = True
    return schema


def _sample(p: model.Prop):
    """Value of a property in the response and body examples: its example, else its title for text."""
    if p.name == "idx":
        return 1
    if (example := _example(p)) not in (None, ""):
        return example
    return {} if p.fieldtype == "JSON" else _label(p)


def _shown(p: model.Prop, write: bool = False, list_item: bool = False) -> bool:
    """Whether a property belongs to a request body (``write``), a list row or a whole record."""
    if write:
        return not p.read_only
    return not p.write_only and (p.in_lists or not list_item)


def _record_example(entity: model.Entity, write: bool = False, list_item: bool = False) -> dict:
    """A record as the routes answer it (``list_item``: a row of a list, without child tables;
    ``write``: as a create body expects it)."""
    record = {p.public: _sample(p) for p in entity.props.values() if _shown(p, write, list_item)}
    for fieldname in entity.collections if not list_item else ():
        child_props = entity.child_props(fieldname).values()
        record[entity.public(fieldname)] = [{p.public: _sample(p) for p in child_props if _shown(p, write)}]
    return record


def _object(props: dict[str, model.Prop], collections: dict[str, str] | None = None, write: bool = False,
            list_item: bool = False) -> dict:
    """Properties under their public names. ``collections``: public name -> schema name of its rows."""
    properties = {p.public: _prop_schema(p) for p in props.values() if _shown(p, write, list_item)}
    for public, child_schema in (collections or {}).items():
        properties[public] = {"type": "array", "items": _ref(child_schema)}
    required = [p.public for p in props.values() if p.required and p.public in properties]
    return {"type": "object", "properties": properties, **({"required": required} if required else {})}


def _list_schema(entity: model.Entity, name: str) -> str:
    """Schema of a list row: the record's own unless some property is left out of lists."""
    return f"{name}ListItem" if any(not p.in_lists and not p.write_only for p in entity.props.values()) else name


def _entity_schemas(entity: model.Entity, public_name: str, schemas: dict) -> str:
    """Schemas named after the route's Public Name, never after the DocType (child rows too)."""
    name = model.schema_name(public_name)
    children = {f: name + model.schema_name(entity.public(f)) for f in entity.collections}
    by_public = {entity.public(f): schema for f, schema in children.items()}
    schemas[name] = {**_object(entity.props, by_public), "description": _(public_name)}
    schemas[f"{name}Input"] = _object(entity.props, by_public, write=True)
    if _list_schema(entity, name) != name:
        # Some property is not returned by lists (Field Mappings > Returned In): rows get their own schema.
        schemas[_list_schema(entity, name)] = {**_object(entity.props, list_item=True), "description": _(public_name)}
    for fieldname, child_schema in children.items():
        schemas.setdefault(child_schema, _object(entity.child_props(fieldname)))
    return name


# --- operations ---------------------------------------------------------------------------

def _filter_parameters(entity: model.Entity) -> list[dict]:
    """One query parameter per field filter (odata.query.field_parameters); none has an example or
    an enum, so Scalar's generated requests carry only what the caller fills in."""
    out = []
    for name, param in query.field_parameters(entity).items():
        label = _label(param.prop)
        schema = _type_schema(param.prop)
        # One paragraph each (field description, how to filter, values, example): Scalar shows the
        # parameter's description next to its request example, and it also travels in the document.
        paragraphs = [_(param.prop.description)] if param.prop.description else []
        choices = None
        if param.operator == "day":
            text = _("{0}: this whole day, as YYYY-MM-DD or DD-MM-YYYY").format(label)
            schema = {"type": "string", "format": "date"}
        elif param.operator == ">=":
            text = _("{0}: from this value (included)").format(label)
        elif param.operator == "<=":
            text = _("{0}: up to this value (included); a date alone includes the whole day").format(label)
        elif param.prop.fieldtype == "Check":
            text = _("{0}: true or false").format(label)
        else:
            text = _("{0}: equals; separate several values with commas to match any of them").format(label)
            choices = schema.pop("enum", None)
            schema["type"] = "string"
        paragraphs.append(text)
        if choices:
            # Named in the text, not as an enum: Scalar would put the first choice in every request.
            paragraphs.append(_("Values: {0}").format(", ".join(f"`{c}`" for c in choices)))
        if (example := _param_example(param)) is not None:
            # In the text, not as ``example``: every standard form (example, examples, schema example)
            # makes Scalar copy it into the generated curl.
            paragraphs.append(_("Example: {0}").format(f"`{example}`"))
        out.append({"name": name, "in": "query", "description": "\n\n".join(paragraphs), "schema": schema})
    return out


def _odata_parameters(entity: model.Entity) -> list[dict]:
    return [
        {"name": "$select", "in": "query", "schema": {"type": "string"},
         "description": _("Properties to return, separated by commas, e.g. `{0}`").format(
             ",".join(p.public for p in list(entity.props.values())[:3]))},
        {"name": "$filter", "in": "query", "schema": {"type": "string"},
         "description": _("Advanced OData filter: `eq ne gt ge lt le`, `in (...)`, `contains/startswith/endswith(field,'text')`, "
                          "`and`, `or`, `not`, parentheses; shape `a and b and (c or d)`. Strings in single quotes, datetimes "
                          "as ISO 8601, e.g. `modified ge 2026-09-01T00:00:00Z`")},
        {"name": "$orderby", "in": "query", "schema": {"type": "string"},
         "description": _("Sort, e.g. `creation asc`; default `creation desc` (newest first)")},
        {"name": "$top", "in": "query", "description": _("Page size (default {0})").format(config.get_int("page_size")),
         "schema": {"type": "integer", "minimum": 1, "maximum": config.get_int("max_page_size")}},
        {"name": "$skip", "in": "query", "description": _("Records to skip"), "schema": {"type": "integer", "minimum": 0}},
        {"name": "$count", "in": "query", "description": _("Add `@odata.count`: total matching records"),
         "schema": {"type": "boolean"}},
    ]


def _tag(spec: config.RouteSpec) -> str:
    """Tag of a route, unique per Sub Route + Sub Category. Scalar shows its x-displayName inside
    the Sub Route folder; importers (Postman, Bruno) name their folder after the tag itself, so it
    reads as "Stats - Records" rather than an identifier. Without a Sub Category it is the Sub
    Route itself ("Campaigns")."""
    section = _(spec.sub_route.capitalize())
    return f"{section} - {_(spec.sub_category.capitalize())}" if spec.sub_category else section


def _auth_tag() -> str:
    return _("Access tokens")


def _created(spec: config.RouteSpec, entity: model.Entity, record: dict) -> dict:
    """201 of a POST: the whole record, or only its ID when the route's Create Response is ID Only."""
    description = _("Created; `Location` has its URL")
    if not spec.verbs["POST"].get("id_only"):
        return {**record, "description": description}
    key = entity.public("name")
    example = _sample(entity.props["name"])
    return {
        "description": description,
        "headers": {"Location": {"schema": {"type": "string", "format": "uri"},
                                 "example": router.gateway_url(f"{spec.path}/{example}")}},
        "content": {"application/json": {
            "schema": {"type": "object", "required": [key], "properties": {
                key: {"type": "string", "description": _("ID of the created record")}}},
            "example": {key: example},
        }},
    }


def _operations(spec: config.RouteSpec, entity: model.Entity, schema: str) -> tuple[dict, dict]:
    tag = [_tag(spec)]
    # Declared on every operation, not only at the root: importers then give each request its own
    # Bearer auth ({{token}} in Bruno) instead of "inherited from collection", ready to fill in.
    security = [{SECURITY_SCHEME: []}]
    public_name = _(spec.public_name)
    op_id = re.sub(r"[^A-Za-z0-9_]", "_", "_".join(spec.segments))
    # Explicit examples: Scalar shows them in the response and request panels next to the curl.
    etag = serialize.etag("2026-09-01 14:30:00.000000")
    record = {"@odata.etag": etag, **_record_example(entity)}
    page_size = config.get_int("page_size")
    page = {"@odata.count": 1, "value": [_record_example(entity, list_item=True)],
            "@odata.nextLink": f"{router.gateway_url(spec.path)}?{urlencode({'$skip': page_size, '$top': page_size})}"}
    new_record = _record_example(entity, write=True)
    editable = [p.public for p in entity.props.values() if not p.read_only and p.name != "naming_series"]
    changes = {k: new_record[k] for k in editable[:2]}
    one = {"description": _("Record"), "headers": {"ETag": {"schema": {"type": "string", "example": etag}}},
           "content": {"application/json": {"schema": _ref(schema), "example": record}}}
    body = {"required": True, "content": {"application/json": {"schema": _ref(f"{schema}Input"), "example": new_record}}}
    if_match = {"name": "If-Match", "in": "header", "schema": {"type": "string"},
                "description": _("ETag of a previous read (`@odata.etag`); 412 if the record changed since")}
    key_param = {"name": entity.public("name"), "in": "path", "required": True,
                 "description": _("{0} ID").format(public_name), "schema": {"type": "string"}}

    def summary(verb: str, fieldname: str) -> dict:
        """Operation name from the route row (Fraxis Route > API Names); its description below it."""
        text = spec.verbs[verb]["description"]
        return {"summary": _(spec.verbs[verb]["names"][fieldname]), **({"description": _(text)} if text else {})}

    collection, item = {}, {}
    if "GET" in spec.verbs:
        collection["get"] = {
            "tags": tag, "operationId": f"{op_id}_list", "security": security, **summary("GET", "list_name"),
            "parameters": _filter_parameters(entity) + _odata_parameters(entity),
            "responses": {
                "200": {"description": _("Page of records"), "content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "@odata.count": {"type": "integer", "description": _("Only with $count=true")},
                        "value": {"type": "array", "items": _ref(_list_schema(entity, schema))},
                        "@odata.nextLink": {"type": "string", "format": "uri",
                                            "description": _("Next page; absent on the last one")},
                    },
                }, "example": page}}},
                **_errors("400", "401", "403"),
            },
        }
        item["get"] = {
            "tags": tag, "operationId": f"{op_id}_get", "security": security, **summary("GET", "get_name"),
            "parameters": [{"name": "$select", "in": "query", "schema": {"type": "string"},
                            "description": _("Properties to return, separated by commas (child tables allowed)")}],
            "responses": {"200": one, **_errors("400", "401", "403", "404")},
        }
    if "POST" in spec.verbs:
        collection["post"] = {
            "tags": tag, "operationId": f"{op_id}_create", "security": security, **summary("POST", "create_name"),
            "requestBody": body,
            "responses": {"201": _created(spec, entity, one), **_errors("400", "401", "403", "409")},
        }
    if "PATCH" in spec.verbs:
        item["patch"] = {
            "tags": tag, "operationId": f"{op_id}_update", "security": security, **summary("PATCH", "update_name"),
            "parameters": [if_match],
            "requestBody": {**body, "content": {"application/json": {"schema": {
                **_ref(f"{schema}Input"), "description": _("Only the properties to change")}, "example": changes}}},
            "responses": {"200": one, **_errors("400", "401", "403", "404", "412")},
        }
    if "DELETE" in spec.verbs:
        item["delete"] = {
            "tags": tag, "operationId": f"{op_id}_delete", "security": security, **summary("DELETE", "delete_name"),
            "parameters": [if_match],
            "responses": {"204": {"description": _("Deleted")}, **_errors("401", "403", "404", "412")},
        }
    if item:
        item = {"parameters": [key_param], **item}
    return collection, item


# --- authentication -------------------------------------------------------------------------

SECURITY_SCHEME = "Bearer"  # shown by Scalar as the auth type
KEY_EXAMPLE = {"api_key": "a1b2c3d4e5f6g7h", "api_secret": "9z8y7x6w5v4u3t2"}
REFRESH_EXAMPLE = "rt1.Q2hhbmdlIG1lIC0gZXhhbXBsZSByZWZyZXNoIHRva2Vu..."


def _token_example() -> dict:
    return {"access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...", "token_type": "Bearer",
            "expires_in": config.get_int("access_token_ttl"), "refresh_token": REFRESH_EXAMPLE,
            "refresh_expires_in": config.get_int("refresh_token_ttl")}


def _oauth_error(code: str, text: str) -> dict:
    return {"description": _(text), "content": {"application/json": {
        "schema": _ref("TokenError"), "example": {"error": code, "error_description": text}}}}


def _tokens_ok(description: str) -> dict:
    return {"description": description, "content": {"application/json": {
        "schema": _ref("TokenResponse"), "example": _token_example()}}}


def _auth_paths() -> dict:
    token = {"post": {
        "tags": [_auth_tag()],
        "operationId": "auth_token",
        "summary": _("Get an access token"),
        # "<access_token>" goes in through {0}: Frappe's _() strips anything that looks like an HTML tag
        # before looking the text up, so a literal one would never match its translation.
        "description": _("Send the API Key and API Secret provided to you. Every other route only accepts the returned "
                         "`access_token` as `Authorization: Bearer {0}`. Keep the `refresh_token` to renew it "
                         "with **Refresh the access token**. Also accepted as OAuth2 client credentials "
                         "(`grant_type=client_credentials` with `client_id` / `client_secret`, in the form body or as HTTP "
                         "Basic) and `grant_type=refresh_token`.").format("<access_token>"),
        "security": [],
        "requestBody": {"required": True, "content": {
            "application/json": {"schema": _ref("TokenRequest"), "example": KEY_EXAMPLE},
            "application/x-www-form-urlencoded": {
                "schema": _ref("ClientCredentials"),
                "example": {"grant_type": "client_credentials", "client_id": KEY_EXAMPLE["api_key"],
                            "client_secret": KEY_EXAMPLE["api_secret"]},
            },
        }},
        "responses": {
            "200": _tokens_ok(_("Access and refresh token")),
            "400": _oauth_error("invalid_request", "api_key and api_secret are required"),
            "401": _oauth_error("invalid_client", "Invalid api_key or api_secret"),
            "403": _oauth_error("unauthorized_client", "API access is not enabled for this account"),
            "429": {"description": _("Too many token requests (30 per minute)")},
        },
    }}
    refresh = {"post": {
        "tags": [_auth_tag()],
        "operationId": "auth_refresh",
        "summary": _("Refresh the access token"),
        "description": _("Exchange the `refresh_token` for a new access token **and a new refresh token**; the one sent "
                         "stops working (rotation). Sending an already used refresh token again ends the whole session. "
                         "When the refresh token expires, request new tokens with the API Key and API Secret."),
        "security": [],
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": _ref("RefreshRequest"), "example": {"refresh_token": REFRESH_EXAMPLE}}}},
        "responses": {
            "200": _tokens_ok(_("New access and refresh token")),
            "400": _oauth_error("invalid_grant", "Refresh token is no longer valid"),
            "429": {"description": _("Too many requests (60 per minute)")},
        },
    }}
    revoke = {"post": {
        "tags": [_auth_tag()],
        "operationId": "auth_revoke",
        "summary": _("Revoke (log out)"),
        "description": _("Ends the session of a refresh token or an access token: the refresh token and every access "
                         "token issued from the same login stop working at once. Answers 200 even for unknown tokens."),
        "security": [],
        "requestBody": {"required": True, "content": {"application/json": {
            "schema": _ref("RevokeRequest"), "example": {"token": REFRESH_EXAMPLE}}}},
        "responses": {
            "200": {"description": _("Session ended"), "content": {"application/json": {
                "schema": {"type": "object", "properties": {"revoked": {"type": "boolean"}}},
                "example": {"revoked": True}}}},
        },
    }}
    return {"/auth/token": token, "/auth/refresh": refresh, "/auth/revoke": revoke}


def _code(*lines: str, lang: str = "bash") -> str:
    return "\n".join([f"```{lang}", *lines, "```"])


def _description(example_path: str | None) -> str:
    """The guide at the top of the docs, one translatable paragraph at a time."""
    token_url = router.gateway_url("/auth/token")
    example_url = router.gateway_url(example_path or "/stats/records")
    ttl = config.get_int("access_token_ttl")
    refresh_ttl = config.get_int("refresh_token_ttl")
    json_header = '-H "Content-Type: application/json" \\'
    blocks = [
        "## " + _("How to authenticate"),
        _("Your **API Key** and **API Secret** are provided to you together with your access to this API."),
        _("**1. Get the tokens** with your API Key and API Secret:"),
        _code(f'curl -X POST "{token_url}" \\', f"  {json_header}",
              """  -d '{"api_key": "<api_key>", "api_secret": "<api_secret>"}'"""),
        _code(f'{{"access_token": "eyJhbGciOiJIUzI1NiIs...", "token_type": "Bearer", "expires_in": {ttl},',
              f' "refresh_token": "rt1.Q2hhbmdl...", "refresh_expires_in": {refresh_ttl}}}', lang="json"),
        _("**2. Send it on every request**; no other credential is accepted by the routes:"),
        _code(f'curl "{example_url}" -H "Authorization: Bearer <access_token>"'),
        _("**3. Renew it** when a route answers `401 Access token expired` (after {0} seconds). The answer is a new "
          "access token **and a new refresh token**; the old refresh token stops working:").format(ttl),
        _code(f'curl -X POST "{router.gateway_url("/auth/refresh")}" \\', f"  {json_header}",
              """  -d '{"refresh_token": "<refresh_token>"}'"""),
        _("When the refresh token itself expires (after {0} seconds), repeat step 1.").format(refresh_ttl),
        _("**4. Log out**: the refresh token and every access token of that login stop working:"),
        _code(f'curl -X POST "{router.gateway_url("/auth/revoke")}" \\', f"  {json_header}",
              """  -d '{"token": "<refresh_token>"}'"""),
        _("If your API Key and API Secret are replaced or your access is disabled, every token stops working at once."),
        _("To try the routes from this page, run **Get an access token** with *Test Request*, copy the `access_token` "
          "and paste it in **Authentication**, *Bearer Token*: the request examples and the downloaded document then use "
          "it as Bearer authentication."),
        # "{{token}}" goes in through {0}: _() would read its braces as a format field.
        _("**Postman or Bruno**: import the document from *Download OpenAPI Document*. Every request comes with Bearer "
          "authentication set to {0}: define a `token` variable in the environment with your access token and every "
          "request uses it.").format("`{{token}}`"),
        "## " + _("Filters"),
        _("Lists are filtered with one query parameter per field: `?status=Active`, several values separated by commas "
          "(`?status=Active,Paused`), and `_from` / `_to` on dates (`?creation_from=2026-09-01&creation_to=2026-09-30`). "
          "Each list shows the parameters it accepts."),
        _("For anything else use the OData options `$filter`, `$select`, `$orderby`, `$top`, `$skip` and `$count`. "
          "Pages carry `@odata.nextLink` until the last one."),
        _("Errors are answered as {0}.").format('`{"error": {"code": "...", "message": "..."}}`'),
    ]
    return "\n\n".join(blocks)


def _components(schemas: dict) -> dict:
    responses = {
        f"Error{code}": {"description": _error_text(code), "content": {"application/json": {
            "schema": _ref("Error"), "example": {"error": {"code": err, "message": msg}}}}}
        for code, (err, msg) in ERROR_EXAMPLES.items()
    }
    schemas.update({
        "Error": {"type": "object", "properties": {"error": {"type": "object", "properties": {
            "code": {"type": "string"}, "message": {"type": "string"}}}}},
        "TokenRequest": {"type": "object", "required": ["api_key", "api_secret"], "properties": {
            "api_key": {"type": "string", "description": _("API Key provided to you")},
            "api_secret": {"type": "string", "format": "password", "description": _("API Secret provided to you")}}},
        "ClientCredentials": {"type": "object", "required": ["grant_type", "client_id", "client_secret"], "properties": {
            "grant_type": {"type": "string", "enum": ["client_credentials"]},
            "client_id": {"type": "string", "description": _("API Key")},
            "client_secret": {"type": "string", "format": "password", "description": _("API Secret")}}},
        "TokenResponse": {"type": "object", "properties": {
            "access_token": {"type": "string"},
            "token_type": {"type": "string", "enum": ["Bearer"]},
            "expires_in": {"type": "integer", "description": _("Seconds until the access token expires")},
            "refresh_token": {"type": "string", "description": _("Send to /auth/refresh; valid once")},
            "refresh_expires_in": {"type": "integer", "description": _("Seconds until the refresh token expires")}}},
        "RefreshRequest": {"type": "object", "required": ["refresh_token"], "properties": {
            "refresh_token": {"type": "string"}}},
        "RevokeRequest": {"type": "object", "required": ["token"], "properties": {
            "token": {"type": "string", "description": _("Refresh token (or access token) of the session to end")}}},
        "TokenError": {"type": "object", "properties": {
            "error": {"type": "string"}, "error_description": {"type": "string"}}},
    })
    return {
        # One scheme only: clients importing the document (Postman, Bruno, Insomnia) set it as the
        # collection's Bearer auth instead of choosing between flows or copying it into headers.
        "securitySchemes": {
            SECURITY_SCHEME: {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": _("`access_token` returned by POST /auth/token."),
            },
        },
        "responses": responses,
        "schemas": schemas,
    }


def build() -> dict:
    """The document in ``frappe.local.lang`` (set by the caller)."""
    exposed = config.exposed_doctypes()
    specs = sorted(
        (s for s in config.routes().values() if s.doctype in exposed),
        key=lambda s: config.SUB_ROUTES.index(s.sub_route) if s.sub_route in config.SUB_ROUTES else 99,
    )
    schemas: dict = {}
    paths: dict = _auth_paths()
    tags = [{"name": _auth_tag(), "x-displayName": _("Access tokens"),
             "description": _("Get, refresh and revoke the tokens every route needs.")}]
    # Folders in Scalar: one group per Sub Route, one tag (sub-folder) per Sub Category.
    groups: dict[str, list[str]] = {_("Authentication"): [_auth_tag()]}

    for spec in specs:
        entity = model.entity(spec.doctype)
        schema = _entity_schemas(entity, spec.public_name, schemas)
        collection, item = _operations(spec, entity, schema)
        if collection:
            paths[spec.path] = collection
        if item:
            paths[f"{spec.path}/{{{entity.public('name')}}}"] = item
        # Routes with a Path share the sub-folder of their Sub Category.
        group = groups.setdefault(_(spec.sub_route.capitalize()), [])
        if _tag(spec) not in group:
            group.append(_tag(spec))
            # Without a Sub Category the folder is named after the route's Public Name: Scalar hides
            # tags outside x-tagGroups, and the Sub Route would only repeat the section title.
            display = _(spec.sub_category.capitalize()) if spec.sub_category else _(spec.public_name)
            tags.append({"name": _tag(spec), "x-displayName": display})

    first_list = next((s.path for s in specs if "GET" in s.verbs), None)
    return {
        "openapi": "3.0.3",
        "info": {"title": _(DOCS_TITLE), "version": __version__, "description": _description(first_list)},
        "servers": [{"url": router.gateway_url(), "description": frappe.local.site}],
        "security": [{SECURITY_SCHEME: []}],
        "tags": tags,
        "x-tagGroups": [{"name": name, "tags": group} for name, group in groups.items()],
        "paths": paths,
        "components": _components(schemas),
    }
