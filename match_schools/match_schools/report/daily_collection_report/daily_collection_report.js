// Copyright (c) 2026, Match Systems and contributors
// For license information, please see license.txt

frappe.query_reports["Daily Collection Report"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -30),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "students_only",
			label: __("School Collections Only"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "payment_type",
			label: __("Payment Type"),
			fieldtype: "Select",
			options: "\nReceive\nPay\nInternal Transfer",
		},
		{
			fieldname: "mode_of_payment",
			label: __("Mode of Payment"),
			fieldtype: "Link",
			options: "Mode of Payment",
		},
		{
			fieldname: "party_type",
			label: __("Party Type"),
			fieldtype: "Link",
			options: "Party Type",
		},
		{
			fieldname: "party",
			label: __("Party"),
			fieldtype: "Dynamic Link",
			options: "party_type",
		},
		{
			fieldname: "cost_center",
			label: __("Cost Center"),
			fieldtype: "Link",
			options: "Cost Center",
		},
		{
			fieldname: "project",
			label: __("Project"),
			fieldtype: "Link",
			options: "Project",
		},
		{
			fieldname: "status",
			label: __("Status"),
			fieldtype: "Select",
			options: "\nDraft\nSubmitted\nCancelled",
		},
		// Site-specific dimensions. The server ignores these unless the column
		// actually exists on Payment Entry, so they are harmless on a site
		// that never added them.
		{
			fieldname: "custom_department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
		},
		{
			fieldname: "custom_branch",
			label: __("Branch"),
			fieldtype: "Link",
			options: "Branch",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname === "paid_amount" && data && data.paid_amount > 0) {
			const color = data.payment_type === "Pay" ? "#b45309" : "#15803d";
			value = `<span style="color:${color}; font-weight:600;">${value}</span>`;
		}

		if (column.fieldname === "status" && data) {
			if (data.status === "Submitted") {
				value = `<span class="indicator-pill green">${__(data.status)}</span>`;
			} else if (data.status === "Cancelled") {
				value = `<span class="indicator-pill red">${__(data.status)}</span>`;
			} else if (data.status === "Draft") {
				value = `<span class="indicator-pill orange">${__(data.status)}</span>`;
			}
		}

		return value;
	},
};
