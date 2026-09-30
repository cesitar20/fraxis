# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""
``$filter`` parser (OData V4 URL conventions §5.1.1, subset) -> Frappe filters.

Pure module (no Frappe access). Supported:

* comparison: ``eq ne gt ge lt le`` against string / number / boolean / null / date /
  datetime literals, plus ``field in ('a','b')``
* functions: ``contains``, ``startswith``, ``endswith`` (and ``not`` of them)
* logic: ``and``, ``or``, ``not``, parentheses

Frappe's ``get_list`` evaluates ``filters`` (AND-ed) AND ``or_filters`` (OR-ed), so the
expression must reduce to ``c1 and c2 ... and (o1 or o2 ...)`` where every ``c``/``o`` is a
simple condition. Anything else is rejected with a clear 501 instead of being guessed.
"""

import re
from dataclasses import dataclass

from fraxis.gateway.odata import ODataError

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<string>'(?:[^']|'')*')
  | (?P<datetime>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)
  | (?P<date>\d{4}-\d{2}-\d{2})
  | (?P<number>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?![A-Za-z0-9_])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<lparen>\()
  | (?P<rparen>\))
  | (?P<comma>,)
    """,
    re.VERBOSE,
)

COMPARISON = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
NEGATED = {"=": "!=", "!=": "=", ">": "<=", ">=": "<", "<": ">=", "<=": ">", "in": "not in",
           "not in": "in", "like": "not like", "not like": "like"}
FUNCTIONS = {"contains", "startswith", "endswith"}


@dataclass
class Tok:
    kind: str
    value: object


@dataclass
class Literal:
    kind: str  # string | number | bool | null | date | datetime
    value: object


def tokenize(text: str) -> list[Tok]:
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ODataError(f"Invalid $filter near: {text[pos:pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        raw = m.group(kind)
        if kind == "ws":
            continue
        if kind == "string":
            out.append(Tok("literal", Literal("string", raw[1:-1].replace("''", "'"))))
        elif kind == "number":
            out.append(Tok("literal", Literal("number", float(raw) if any(c in raw for c in ".eE") else int(raw))))
        elif kind in ("date", "datetime"):
            out.append(Tok("literal", Literal(kind, raw)))
        elif kind == "ident":
            low = raw.lower()
            if low in ("true", "false"):
                out.append(Tok("literal", Literal("bool", low == "true")))
            elif low == "null":
                out.append(Tok("literal", Literal("null", None)))
            elif low in ("and", "or", "not", "in", *COMPARISON):
                out.append(Tok("op", low))
            else:
                out.append(Tok("ident", raw))
        else:
            out.append(Tok(kind, raw))
    return out


# AST nodes are tuples: ("and", a, b) | ("or", a, b) | ("not", a) | ("cond", field, op, value)

class _Parser:
    def __init__(self, toks: list[Tok]):
        self.toks, self.i = toks, 0

    def peek(self, kind=None, value=None) -> Tok | None:
        tok = self.toks[self.i] if self.i < len(self.toks) else None
        if tok and (kind is None or tok.kind == kind) and (value is None or tok.value == value):
            return tok
        return None

    def take(self, kind=None, value=None) -> Tok:
        tok = self.peek(kind, value)
        if not tok:
            got = self.toks[self.i].value if self.i < len(self.toks) else "end of expression"
            raise ODataError(f"Invalid $filter: expected {value or kind}, got {got!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.or_expr()
        if self.i != len(self.toks):
            raise ODataError(f"Invalid $filter: unexpected {self.toks[self.i].value!r}")
        return node

    def or_expr(self):
        node = self.and_expr()
        while self.peek("op", "or"):
            self.i += 1
            node = ("or", node, self.and_expr())
        return node

    def and_expr(self):
        node = self.unary()
        while self.peek("op", "and"):
            self.i += 1
            node = ("and", node, self.unary())
        return node

    def unary(self):
        if self.peek("op", "not"):
            self.i += 1
            return ("not", self.unary())
        if self.peek("lparen"):
            self.i += 1
            node = self.or_expr()
            self.take("rparen")
            return node
        return self.condition()

    def condition(self):
        ident = self.take("ident").value
        if ident.lower() in FUNCTIONS and self.peek("lparen"):
            return self.function(ident.lower())

        op_tok = self.take("op")
        if op_tok.value == "in":
            self.take("lparen")
            values = [self.take("literal").value]
            while self.peek("comma"):
                self.i += 1
                values.append(self.take("literal").value)
            self.take("rparen")
            return ("cond", ident, "in", values)
        if op_tok.value not in COMPARISON:
            raise ODataError(f"Invalid $filter: unexpected operator {op_tok.value!r}")
        lit = self.take("literal").value
        op = COMPARISON[op_tok.value]
        if lit.kind == "null":
            if op not in ("=", "!="):
                raise ODataError("null can only be compared with eq / ne")
            return ("cond", ident, "is", Literal("string", "not set" if op == "=" else "set"))
        return ("cond", ident, op, lit)

    def function(self, name: str):
        self.take("lparen")
        field = self.take("ident").value
        self.take("comma")
        lit = self.take("literal").value
        self.take("rparen")
        if lit.kind != "string":
            raise ODataError(f"{name}() expects a string literal")
        value = _escape_like(str(lit.value))
        pattern = {"startswith": f"{value}%", "endswith": f"%{value}"}.get(name, f"%{value}%")
        return ("cond", field, "like", Literal("string", pattern))


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def parse(text: str):
    return _Parser(tokenize(text)).parse()


def _negate(node):
    kind = node[0]
    if kind == "not":
        return node[1]
    if kind == "cond":
        _, field, op, value = node
        if op == "is":
            return ("cond", field, "is", Literal("string", "set" if value.value == "not set" else "not set"))
        return ("cond", field, NEGATED[op], value)
    return ("or" if kind == "and" else "and", _negate(node[1]), _negate(node[2]))  # De Morgan


def _flatten(node, kind) -> list:
    if node[0] == kind:
        return _flatten(node[1], kind) + _flatten(node[2], kind)
    return [node]


def _push_not(node):
    if node[0] == "not":
        return _push_not(_negate(node[1]))
    if node[0] in ("and", "or"):
        return (node[0], _push_not(node[1]), _push_not(node[2]))
    return node


def to_frappe(text: str) -> tuple[list, list]:
    """Return ``(filters, or_filters)`` as lists of ``(field, op, Literal | list[Literal])``."""
    node = _push_not(parse(text))
    and_filters, or_filters = [], []
    for term in _flatten(node, "and"):
        if term[0] == "cond":
            and_filters.append(term[1:])
        elif term[0] == "or" and not or_filters:
            parts = _flatten(term, "or")
            if any(p[0] != "cond" for p in parts):
                raise ODataError("Unsupported $filter: nested and/or inside an or-group", 501, "NotImplemented")
            or_filters = [p[1:] for p in parts]
        else:
            raise ODataError(
                "Unsupported $filter: only 'a and b and (c or d)' shapes are supported (one or-group)",
                501,
                "NotImplemented",
            )
    return and_filters, or_filters
