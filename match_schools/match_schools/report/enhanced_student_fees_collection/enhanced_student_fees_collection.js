// Copyright (c) 2026, Match Systems and contributors
// For license information, please see license.txt

frappe.query_reports["Enhanced Student Fees Collection"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "academic_year",
			label: __("Academic Year"),
			fieldtype: "Link",
			options: "Academic Year",
			default: frappe.defaults.get_user_default("academic_year"),
		},
		{
			fieldname: "academic_term",
			label: __("Academic Term"),
			fieldtype: "Link",
			options: "Academic Term",
		},
		{
			fieldname: "student",
			label: __("Student"),
			fieldtype: "Link",
			options: "Student",
		},
		{
			fieldname: "program",
			label: __("Program"),
			fieldtype: "Link",
			options: "Program",
		},
		{
			fieldname: "student_batch_name",
			label: __("Student Batch"),
			fieldtype: "Link",
			options: "Student Batch Name",
		},
		{
			fieldname: "student_category",
			label: __("Student Category"),
			fieldtype: "Link",
			options: "Student Category",
		},
		{
			fieldname: "student_group",
			label: __("Student Group"),
			fieldtype: "Link",
			options: "Student Group",
		},
		{
			fieldname: "fee_schedule",
			label: __("Fee Schedule"),
			fieldtype: "Link",
			options: "Fee Schedule",
		},
		{
			fieldname: "outstanding_amount",
			label: __("Payment"),
			fieldtype: "Select",
			options: "\nHas Outstanding\nFully Paid",
		},
		// Posting and due dates are separate ranges and labelled as such: the
		// office asks "what did we bill in March" and "what falls due in March"
		// on different days, and a single ambiguous "From Date" answered only
		// one of them.
		{
			fieldname: "from_date",
			label: __("From Posting Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "to_date",
			label: __("To Posting Date"),
			fieldtype: "Date",
		},
		{
			fieldname: "due_from",
			label: __("Due From"),
			fieldtype: "Date",
		},
		{
			fieldname: "due_to",
			label: __("Due To"),
			fieldtype: "Date",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname === "outstanding_amount" && data) {
			const color = data.outstanding_amount > 0 ? "#dc3545" : "#28a745";
			value = `<span style="color:${color}; font-weight:600; white-space:nowrap;">${value}</span>`;
		}
		if (column.fieldname === "paid_amount" && data && data.paid_amount > 0) {
			value = `<span style="color:#28a745; font-weight:600; white-space:nowrap;">${value}</span>`;
		}

		// Mobile numbers are the point of this report — make them dialable.
		if (
			(column.fieldname === "student_mobile" || column.fieldname === "guardian_mobile") &&
			value
		) {
			value = `<a href="tel:${value}">${value}</a>`;
		}

		if (column.fieldname === "status" && data && data.status) {
			let color = "#6c757d",
				bg = "#e9ecef";
			if (data.status === "Fully Paid") {
				color = "#0f5132";
				bg = "#d1e7dd";
			} else if (data.status === "Partially Paid") {
				color = "#664d03";
				bg = "#fff3cd";
			} else if (data.status === "Unpaid") {
				color = "#842029";
				bg = "#f8d7da";
			}
			value = `<span style="color:${color}; background-color:${bg}; padding:2px 8px; border-radius:10px; white-space:nowrap;">${__(
				data.status
			)}</span>`;
		}

		return value;
	},
};
