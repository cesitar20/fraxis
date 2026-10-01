// Copyright (c) 2026, Picurit and contributors
// For license information, please see license.txt

// Live preview of the Route column; the server recomputes it on save (config.route_label).
function fraxis_route_label(frm, row) {
	const base = "/" + (frm.doc.base_path || "/fraxis").trim().replace(/^\/+|\/+$/g, "");
	if (!row.sub_route) return "";
	const path = (row.path || "").trim().replace(/^\/+|\/+$/g, "");
	const item = ["PATCH", "DELETE"].includes(row.http_method) ? "/{name}" : "";
	return `${base}/${[row.sub_route, row.sub_category, path].filter(Boolean).join("/")}${item}`;
}

function fraxis_set_route(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	frappe.model.set_value(cdt, cdn, "route", fraxis_route_label(frm, row));
}

// API Names of a route row (server: config.OPERATION_NAMES / default_operation_names).
const FRAXIS_OPERATION_NAMES = {
	GET: [
		["list_name", "List"],
		["get_name", "Get one"],
	],
	POST: [["create_name", "Create"]],
	PATCH: [["update_name", "Update"]],
	DELETE: [["delete_name", "Delete"]],
};
const fraxis_last_public = {};

// Fill empty names, and names still carrying the previous default, from the Public Name.
function fraxis_set_names(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	const public_name = (row.public_name || "").trim() || row.ref_doctype;
	const previous = fraxis_last_public[cdn];
	fraxis_last_public[cdn] = public_name;
	if (!public_name) return;
	(FRAXIS_OPERATION_NAMES[row.http_method] || []).forEach(([fieldname, verb]) => {
		const current = row[fieldname];
		if (!current || (previous && current === `${verb} ${previous}`)) {
			frappe.model.set_value(cdt, cdn, fieldname, `${verb} ${public_name}`);
		}
	});
}

// Excluded Fields: one row per DocType; Select Fields picks from what it can return
// (server: model.publishable_fields) and stores them comma-separated.
const fraxis_fields_cache = {};

function fraxis_doctype_fields(doctype, include_name = false) {
	const key = `${doctype}|${include_name ? 1 : 0}`;
	if (!fraxis_fields_cache[key]) {
		fraxis_fields_cache[key] = frappe.xcall(
			"fraxis.fraxis_socket_io.doctype.fraxis_settings.fraxis_settings.get_doctype_fields",
			{ doctype, include_name: include_name ? 1 : 0 }
		);
	}
	return fraxis_fields_cache[key];
}

function fraxis_split_fields(value) {
	return [...new Set((value || "").split(/[,\n]/).map((f) => f.trim()).filter(Boolean))];
}

function fraxis_select_fields(frm, cdt, cdn) {
	const row = locals[cdt][cdn];
	if (!row.ref_doctype) {
		frappe.msgprint(__("Select the DocType first"));
		return;
	}
	fraxis_doctype_fields(row.ref_doctype).then((fields) => {
		const selected = fraxis_split_fields(row.fieldnames);
		const dialog = new frappe.ui.Dialog({
			title: __("Fields of {0} to exclude", [row.ref_doctype]),
			size: "large",
			fields: [
				{
					fieldtype: "MultiCheck",
					fieldname: "fields",
					columns: 2,
					options: fields.map((f) => ({
						label: `${__(f.label)} (${f.fieldname})`,
						value: f.fieldname,
						checked: selected.includes(f.fieldname),
					})),
				},
			],
			primary_action_label: __("Set"),
			primary_action(values) {
				frappe.model.set_value(cdt, cdn, "fieldnames", (values.fields || []).join(", "));
				dialog.hide();
			},
		});
		dialog.show();
	});
}

// Field Mappings > Field: a Select whose options are the row DocType's fields, ID included.
function fraxis_set_mapping_options(frm, cdn) {
	const row = locals["Fraxis Field Mapping"][cdn];
	const docfield = frappe.meta.get_docfield("Fraxis Field Mapping", "fieldname", cdn);
	if (!row || !docfield) return;
	if (!row.ref_doctype) {
		docfield.options = [];
		return;
	}
	fraxis_doctype_fields(row.ref_doctype, true).then((fields) => {
		docfield.options = [
			{ value: "", label: "" },
			...fields.map((f) => ({ value: f.fieldname, label: `${__(f.label)} (${f.fieldname})` })),
		];
		const grid_row = frm.fields_dict.field_mappings.grid.get_row(cdn);
		if (grid_row) grid_row.refresh_field("fieldname");
	});
}

frappe.ui.form.on("Fraxis Settings", {
	refresh(frm) {
		(frm.doc.field_mappings || []).forEach((row) => fraxis_set_mapping_options(frm, row.name));
		(frm.doc.routes || []).forEach((row) => {
			fraxis_last_public[row.name] = (row.public_name || "").trim() || row.ref_doctype;
		});
	},
	base_path(frm) {
		(frm.doc.routes || []).forEach((row) => fraxis_set_route(frm, row.doctype, row.name));
	},
});

frappe.ui.form.on("Fraxis Route", {
	http_method(frm, cdt, cdn) {
		fraxis_set_route(frm, cdt, cdn);
		fraxis_set_names(frm, cdt, cdn);
	},
	sub_route: fraxis_set_route,
	sub_category: fraxis_set_route,
	path: fraxis_set_route,
	ref_doctype: fraxis_set_names,
	public_name: fraxis_set_names,
});

frappe.ui.form.on("Fraxis Excluded Field", {
	ref_doctype(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "fieldnames", "");
		if (locals[cdt][cdn].ref_doctype) fraxis_select_fields(frm, cdt, cdn);
	},
	select_fields: fraxis_select_fields,
});

frappe.ui.form.on("Fraxis Field Mapping", {
	field_mappings_add(frm, cdt, cdn) {
		fraxis_set_mapping_options(frm, cdn);
	},
	ref_doctype(frm, cdt, cdn) {
		frappe.model.set_value(cdt, cdn, "fieldname", "");
		fraxis_set_mapping_options(frm, cdn);
	},
	form_render(frm, cdt, cdn) {
		fraxis_set_mapping_options(frm, cdn);
	},
});
