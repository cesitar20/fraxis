# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
OData V2 FunctionImports: whitelisted methods listed in ``Fraxis Settings > OData V2
Functions``. Parameters and their Edm types come from the Python signature of the method;
V2 clients send them in the query string as URI literals (``'text'``, ``10L``, ``true``,
``datetime'…'``), which :func:`parse_literal` turns back into Python values.
"""

import inspect
import re
from datetime import datetime

import frappe

from fraxis.gateway import config

_ANNOTATION_TYPES = {int: "Edm.Int32", float: "Edm.Double", bool: "Edm.Boolean", datetime: "Edm.DateTime"}


def _params(method: str) -> list[tuple[str, str]]:
    try:
        fn = frappe.get_attr(method)
    except Exception:
        return []
    fn = inspect.unwrap(fn)
    out = []
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        hint = param.annotation
        if hint is param.empty and param.default not in (param.empty, None):
            hint = type(param.default)  # unannotated: infer from the default (debug=False -> bool)
        out.append((name, _ANNOTATION_TYPES.get(hint, "Edm.String")))
    return out


def v2_functions() -> dict[str, dict]:
    """FunctionImport name -> {name, method, http_method, return_type, params}."""
    return {fn["name"]: {**fn, "params": _params(fn["method"])} for fn in config.get("odata_functions")}


def parse_literal(raw: str):
    """OData V2 URI literal -> Python value (strings unquoted, typed prefixes stripped)."""
    if raw is None:
        return None
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if m := re.fullmatch(r"(datetime|datetimeoffset|guid|time)'(.*)'", value):
        return m.group(2)
    if value in ("true", "false"):
        return value == "true"
    if value == "null":
        return None
    if re.fullmatch(r"-?\d+[Ll]?", value):
        return int(value.rstrip("Ll"))
    if re.fullmatch(r"-?\d+(\.\d+)?([eE][+-]?\d+)?[MmDdFf]?", value):
        return float(value.rstrip("MmDdFf"))
    return value  # unquoted text: accept as is (lenient for hand-written URLs)
