# match_schools_backend

Frappe v16 app providing the backend for **Match Education** — a
school management web app.

It sits on top of the standard `education` app and adds the roles, doctypes and
JSON APIs the frontend needs. **Nothing in `education` or `match_edu_v2` is
modified**; every customisation lives here.

- Frontend: <https://github.com/tareqkhanfar/match-connect-learn>

## Requirements

- Frappe **v16**
- `erpnext`
- `education`

## Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/tareqkhanfar/match_schools_backend.git --branch main
bench --site $SITE install-app match_schools
```

Installing (and every `bench migrate`) creates the four Match Schools roles and applies a
required compatibility patch — see *Education v16 workaround* below.

## Roles

The frontend has four personas. Each maps to a Frappe role created on install:

| Persona   | Frappe role         | Scoped to                 |
| --------- | ------------------- | ------------------------- |
| `admin`   | `MS School Admin`  | everything                |
| `teacher` | `MS Teacher`       | their own student groups  |
| `student` | `MS Student`       | themselves                |
| `parent`  | `MS Parent`        | their children            |

The persona is derived from the signed-in user's roles — it is **never** sent by
the client. `Administrator` and `System Manager` resolve to `admin` so the system
is usable before any role is handed out.

A user is linked to their education record through `Student.user`,
`Guardian.user`, or `Instructor → Employee → user_id`.

## API

All endpoints live under `match_schools.api.*` and return one flat envelope:

```json
{ "success": true, "data": {}, "message_en": "", "message_ar": "" }
```

Authentication uses Frappe's own session cookie, so the browser sends `sid` on
every later request.

| Module          | Purpose                                                              |
| --------------- | -------------------------------------------------------------------- |
| `auth`          | `login`, `logout`, `me`                                              |
| `dashboard`     | `summary` — a different payload per persona                          |
| `students`      | directory (server-side search/filter/paging), profile, create/update |
| `academics`     | classes, subjects, teachers, timetable, exams, grades, report card   |
| `attendance`    | marking grid, save marks, reporting, chronic absentees               |
| `assignments`   | list/save, submissions, student submit, teacher grading              |
| `fees`          | invoices, summary, collection report                                 |
| `communication` | targeted announcements, message threads, contacts                    |
| `reports`       | academic / attendance / financial analysis                           |
| `settings`      | school details, active academic year and term, role counts           |

Access is enforced per endpoint by the `ms_endpoint` decorator, and every read
is scoped by `resolve_scope()` — a teacher only sees their own groups, a parent
only their children.

## Doctypes

Four doctypes cover the gaps `education` does not:

- **MS Assignment** / **MS Assignment Submission**
- **MS Announcement**
- **MS Message**

Everything else reuses `education`: Student, Instructor, Program, Student Group,
Course, Student Attendance, Course Schedule, Fees, Assessment Plan/Result and
Guardian.

## Demo data

Optional, for a working local environment:

```bash
bench --site $SITE console
```

```python
from match_schools.setup.demo_data import create_demo_data
from match_schools.setup.demo_users import create_demo_users
from match_schools.setup.demo_finance import create_demo_finance

frappe.flags.in_import = True   # bypasses user-creation throttling
create_demo_data()      # grades, subjects, teachers, classes, students, attendance
create_demo_users()     # one login per persona
create_demo_finance()   # fee structures, invoices, assessments and results
frappe.db.commit()
```

Demo logins (password `Match@12345`): `admin@`, `teacher@`, `student@` and
`parent@match-edu.ps`.

## Education v16 workaround

`Fees.income_account` ships with `fetch_from = "fee_structure.income_account"`,
but `Fee Structure` has no `income_account` field and `fetch_if_empty` is `0`,
so the fetch always runs. Because `fee_structure` is mandatory on `Fees`, every
insert fails with:

```
OperationalError: (1054, "Unknown column 'income_account' in 'SELECT'")
```

`match_schools.setup.install.patch_fees_income_account_fetch` clears the broken
`fetch_from` with a system-generated Property Setter on install and migrate. It
is a no-op if upstream adds the field.

## Contributing

This app uses `pre-commit` for formatting and linting:

```bash
cd apps/match_schools
pre-commit install
```

Configured tools: ruff, eslint, prettier, pyupgrade.

## License

MIT
