// Copyright (c) 2026, Match Systems and contributors
// For license information, please see license.txt

frappe.query_reports["Student Statement of Account"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			reqd: 1,
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "student",
			label: __("Student"),
			fieldtype: "Link",
			options: "Student",
			reqd: 1,
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.year_start(),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "show_all_voucher_accounts",
			label: __("Show All Voucher Accounts (incl. Cash/Bank/Discount legs)"),
			fieldtype: "Check",
			default: 1,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && (data.row_type === "opening" || data.row_type === "closing")) {
			return `<span style="font-weight:700; background-color:#f3f4f6;">${value}</span>`;
		}

		// Legs of the same voucher that are not the student's own receivable
		// row (cash, bank, discount) — context only, they do not move the
		// running balance.
		if (data && data.row_type === "other_leg") {
			if (column.fieldname === "account") {
				return `<span style="color:#6c757d;">↳ ${value}</span>`;
			}
			return `<span style="color:#6c757d;">${value}</span>`;
		}

		if (
			column.fieldname === "balance" &&
			data &&
			data.row_type === "student" &&
			data.balance != null
		) {
			const color = data.balance > 0 ? "#dc3545" : "#28a745";
			value = `<span style="color:${color}; font-weight:600;">${value}</span>`;
		}
		if (column.fieldname === "debit" && data && data.debit > 0) {
			value = `<span style="color:#b45309;">${value}</span>`;
		}
		if (column.fieldname === "credit" && data && data.credit > 0) {
			value = `<span style="color:#15803d;">${value}</span>`;
		}

		return value;
	},
};
