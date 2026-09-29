# Copyright (c) 2026, Picurit and contributors
# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at http://mozilla.org/MPL/2.0/.
# For license information, please see license.txt

"""``$metadata`` — CSDL XML for the entity sets the current user can read.

* :func:`build`    — OData V4 (``<base_path>/odata/$metadata``)
* :func:`build_v2` — OData V2 (``<base_path>/odata/v2/$metadata``) for V2-only clients such
  as SAP pyodata: edmx 1.0 namespaces, V2 primitive types, no complex-type collections
  (child tables are not part of the V2 model) and the FunctionImports configured in
  Fraxis Settings.
"""

from xml.sax.saxutils import quoteattr

from fraxis.gateway.odata import functions, model


def _type_name(doctype: str) -> str:
    return model.set_name_for(doctype)


def _props_xml(props: dict[str, model.Prop], indent: str) -> list[str]:
    out = []
    for p in props.values():
        nullable = "" if p.nullable else ' Nullable="false"'
        out.append(f"{indent}<Property Name={quoteattr(p.name)} Type=\"{p.edm}\"{nullable}/>")
    return out


def build(sets: dict[str, str]) -> str:
    ns = model.NAMESPACE
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx" Version="4.0">',
        "  <edmx:DataServices>",
        f'    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm" Namespace="{ns}">',
    ]
    complex_types: dict[str, dict] = {}
    for set_name, doctype in sets.items():
        entity = model.entity_type(doctype)
        lines.append(f"      <EntityType Name={quoteattr(set_name)}>")
        lines.append('        <Key><PropertyRef Name="name"/></Key>')
        lines += _props_xml(entity.props, "        ")
        for fieldname, child in entity.collections.items():
            complex_types.setdefault(child, model.complex_type(child))
            lines.append(
                f'        <Property Name={quoteattr(fieldname)} Type="Collection({ns}.{_type_name(child)})"/>'
            )
        lines.append("      </EntityType>")
    for child, props in complex_types.items():
        lines.append(f"      <ComplexType Name={quoteattr(_type_name(child))}>")
        lines += _props_xml(props, "        ")
        lines.append("      </ComplexType>")
    lines.append('      <EntityContainer Name="Container">')
    for set_name in sets:
        lines.append(f"        <EntitySet Name={quoteattr(set_name)} EntityType=\"{ns}.{set_name}\"/>")
    lines += ["      </EntityContainer>", "    </Schema>", "  </edmx:DataServices>", "</edmx:Edmx>"]
    return "\n".join(lines)


EDMX_V1 = "http://schemas.microsoft.com/ado/2007/06/edmx"
EDM_V2 = "http://schemas.microsoft.com/ado/2008/09/edm"
METADATA_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"


def build_v2(sets: dict[str, str]) -> str:
    ns = model.NAMESPACE
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<edmx:Edmx xmlns:edmx="{EDMX_V1}" Version="1.0">',
        f'  <edmx:DataServices xmlns:m="{METADATA_NS}" m:DataServiceVersion="2.0">',
        f'    <Schema xmlns="{EDM_V2}" xmlns:m="{METADATA_NS}" Namespace="{ns}">',
    ]
    for set_name, doctype in sets.items():
        entity = model.entity_type(doctype)
        lines.append(f"      <EntityType Name={quoteattr(set_name)}>")
        lines.append('        <Key><PropertyRef Name="name"/></Key>')
        for p in entity.props.values():
            nullable = "" if p.nullable else ' Nullable="false"'
            etag = ' ConcurrencyMode="Fixed"' if p.name == "modified" else ""
            lines.append(f'        <Property Name={quoteattr(p.name)} Type="{model.edm_v2(p)}"{nullable}{etag}/>')
        lines.append("      </EntityType>")
    lines.append('      <EntityContainer Name="Container" m:IsDefaultEntityContainer="true">')
    for set_name in sets:
        lines.append(f'        <EntitySet Name={quoteattr(set_name)} EntityType="{ns}.{set_name}"/>')
    for fn in functions.v2_functions().values():
        lines.append(
            f'        <FunctionImport Name={quoteattr(fn["name"])} ReturnType="{fn["return_type"]}" '
            f'm:HttpMethod="{fn["http_method"]}">'
        )
        for param, edm in fn["params"]:
            lines.append(f'          <Parameter Name={quoteattr(param)} Type="{edm}" Mode="In" Nullable="true"/>')
        lines.append("        </FunctionImport>")
    lines += ["      </EntityContainer>", "    </Schema>", "  </edmx:DataServices>", "</edmx:Edmx>"]
    return "\n".join(lines)
