// Copyright (c) 2026, Match Systems and contributors
// For license information, please see license.txt

frappe.query_reports["Unenrolled Students Next Academic Year"] = {
	filters: [
		{
			fieldname: "from_academic_year",
			label: __("Previous Academic Year"),
			fieldtype: "Link",
			options: "Academic Year",
			reqd: 1,
		},
		{
			fieldname: "to_academic_year",
			label: __("New Academic Year"),
			fieldtype: "Link",
			options: "Academic Year",
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
			fieldname: "payment_status",
			label: __("Payment Status"),
			fieldtype: "Select",
			options: "\nAll\nFully Paid\nPartially Paid\nUnpaid\nNo Fees",
			default: "All",
		},
		{
			fieldname: "only_with_outstanding",
			label: __("Only Students With Outstanding"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "exclude_left_students",
			label: __("Exclude Students Marked as Left"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (data && data.is_total) {
			return `<span style="font-weight:700; background-color:#e5e7eb;">${value}</span>`;
		}

		if (
			(column.fieldname === "total_outstanding" ||
				column.fieldname === "prev_outstanding_amount") &&
			data
		) {
			const color = (data[column.fieldname] || 0) > 0 ? "#dc3545" : "#28a745";
			value = `<span style="color:${color}; white-space:nowrap;">${value}</span>`;
		}
		if (column.fieldname === "prev_paid_amount" && data && data.prev_paid_amount > 0) {
			value = `<span style="color:#28a745; white-space:nowrap;">${value}</span>`;
		}

		// Mobile numbers are why this report exists — make them dialable.
		if (
			(column.fieldname === "guardian_mobile" ||
				column.fieldname === "student_mobile_number") &&
			value
		) {
			value = `<a href="tel:${value}">${value}</a>`;
		}

		if (column.fieldname === "payment_status" && data && data.payment_status) {
			let color = "#6c757d",
				bg = "#e9ecef";
			if (data.payment_status === __("Fully Paid")) {
				color = "#0f5132";
				bg = "#d1e7dd";
			} else if (data.payment_status === __("Partially Paid")) {
				color = "#664d03";
				bg = "#fff3cd";
			} else if (data.payment_status === __("Unpaid")) {
				color = "#842029";
				bg = "#f8d7da";
			}
			value = `<span style="color:${color}; background-color:${bg}; padding:2px 8px; border-radius:10px; white-space:nowrap;">${value}</span>`;
		}

		if (column.fieldname === "student_status" && data && data.student_status) {
			let color = "#0f5132",
				bg = "#d1e7dd";
			if (data.student_status === __("Left")) {
				color = "#842029";
				bg = "#f8d7da";
			} else if (data.student_status === __("Disabled")) {
				color = "#6c757d";
				bg = "#e9ecef";
			}
			value = `<span style="color:${color}; background-color:${bg}; padding:2px 8px; border-radius:10px; white-space:nowrap;">${value}</span>`;
		}

		return value;
	},
};
