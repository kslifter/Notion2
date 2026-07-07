"""
Daily Notion task email

Designed for the HT Tasks database / RevtoNotion connection.

This script intentionally reads credentials from environment variables or a local .env file.
Do not hard-code Notion tokens, email passwords, or app passwords in a public GitHub repo.
"""

from __future__ import annotations

import html
import os
import smtplib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests


# -----------------------------------------------------------------------------
# Local .env support
# -----------------------------------------------------------------------------
# GitHub Actions will use repository secrets. For local PyCharm testing, create a
# file named .env in the repo root or dailyEmail folder. Keep .env in .gitignore.
# Example:
# NOTION_TOKEN=secret_xxx
# NOTION_TASKS_DATABASE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# SMTP_PASSWORD=your-google-app-password
# -----------------------------------------------------------------------------


def load_dotenv() -> None:
    candidates = [Path(".env"), Path(__file__).resolve().parent / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


# -----------------------------------------------------------------------------
# Configuration matching your screenshot
# -----------------------------------------------------------------------------

NOTION_TOKEN = env("NOTION_TOKEN", required=True)
NOTION_TASKS_DATABASE_ID = env("NOTION_TASKS_DATABASE_ID", required=True).replace("-", "")
NOTION_VERSION = env("NOTION_VERSION", "2022-06-28")
TIME_ZONE = env("TIME_ZONE", "America/Chicago")

EMAIL_FROM = env("EMAIL_FROM", "ihartsook@huttonbuilds.com")
EMAIL_TO = env("EMAIL_TO", "ihartsook@huttonbuilds.com")
SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USERNAME = env("SMTP_USERNAME", EMAIL_FROM)
SMTP_PASSWORD = env("SMTP_PASSWORD", required=True)

# Property names from the Notion view screenshot. Override in GitHub Secrets or .env
# if your database uses slightly different names.
TITLE_PROPERTY = env("TITLE_PROPERTY", "")  # blank = auto-detect title property
PROJECT_PROPERTY = env("PROJECT_PROPERTY", "Projects")
DUE_DATE_PROPERTY = env("DUE_DATE_PROPERTY", "DUE DATE")
DB_ENTRY_TYPE_PROPERTY = env("DB_ENTRY_TYPE_PROPERTY", "DB ENTRY TYPE")
ASSIGNEE_PROPERTY = env("ASSIGNEE_PROPERTY", "ASSIGNEE")
PROJECT_STATUS_PROPERTY = env("PROJECT_STATUS_PROPERTY", "Project Status")
STATUS_PROPERTY = env("STATUS_PROPERTY", "STATUS")
PRIORITY_PROPERTY = env("PRIORITY_PROPERTY", "PRIORITY")

# View filters from the screenshot.
DB_ENTRY_TYPE_VALUES = [v.strip() for v in env("DB_ENTRY_TYPE_VALUES", "Task").split(",") if v.strip()]
ASSIGNEE_MATCHES = [
    v.strip().lower()
    for v in env("ASSIGNEE_MATCHES", "Ian Hartsook,ihartsook@huttonbuilds.com").split(",")
    if v.strip()
]
PROJECT_STATUS_VALUES = [v.strip() for v in env("PROJECT_STATUS_VALUES", "In progress").split(",") if v.strip()]
STATUS_VALUES = [v.strip() for v in env("STATUS_VALUES", "In progress,To-do").split(",") if v.strip()]

# Optional: if you know your Notion user ID, this lets Notion do the assignee filter
# server-side. If blank, the script fetches matching tasks and filters assignee by
# displayed name/email locally.
NOTION_ASSIGNEE_USER_ID = env("NOTION_ASSIGNEE_USER_ID", "")

# all_open = match the screenshot filters and include all active assigned tasks.
# due_today_overdue = only include tasks due today or overdue.
# next_7_days = include overdue/today plus the next 7 days.
DUE_MODE = env("DUE_MODE", "all_open")


@dataclass(frozen=True)
class Task:
    title: str
    url: str
    project_names: list[str]
    due_date: str
    status: str
    priority: str
    assignees: list[str]


# -----------------------------------------------------------------------------
# Notion API helpers
# -----------------------------------------------------------------------------


def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    response = requests.request(method, url, headers=notion_headers(), json=payload, timeout=30)
    if not response.ok:
        raise RuntimeError(
            f"Notion API error {response.status_code} for {method} {path}:\n{response.text}"
        )
    return response.json()


def retrieve_database_schema(database_id: str) -> dict[str, Any]:
    database = notion_request("GET", f"databases/{database_id}")
    return database.get("properties", {})


def query_database(database_id: str, query: dict[str, Any]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    cursor: str | None = None

    while True:
        payload = dict(query)
        payload["page_size"] = 100
        if cursor:
            payload["start_cursor"] = cursor

        response = notion_request("POST", f"databases/{database_id}/query", payload)
        results.extend(response.get("results", []))
        if not response.get("has_more"):
            return results
        cursor = response.get("next_cursor")


PAGE_TITLE_CACHE: dict[str, str] = {}


def retrieve_page_title(page_id: str) -> str:
    if page_id in PAGE_TITLE_CACHE:
        return PAGE_TITLE_CACHE[page_id]

    try:
        page = notion_request("GET", f"pages/{page_id}")
        title = extract_title(page) or page_id[:8]
    except Exception:
        title = page_id[:8]

    PAGE_TITLE_CACHE[page_id] = title
    return title


# -----------------------------------------------------------------------------
# Filter building
# -----------------------------------------------------------------------------


def prop_type(schema: dict[str, Any], prop_name: str) -> str | None:
    prop = schema.get(prop_name)
    return prop.get("type") if prop else None


def normalize_prop_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def resolve_property_name(schema: dict[str, Any], preferred: str, candidates: list[str]) -> str:
    """Resolve a configured property name against the real Notion schema.

    Notion property names are exact, but the UI can make them feel fuzzy. This lets
    the script handle common variations like Project vs Projects or Status vs STATUS.
    """
    if preferred and preferred in schema:
        return preferred

    for candidate in candidates:
        if candidate in schema:
            return candidate

    by_normalized = {normalize_prop_name(name): name for name in schema.keys()}
    for candidate in [preferred] + candidates:
        normalized = normalize_prop_name(candidate)
        if normalized in by_normalized:
            return by_normalized[normalized]

    return preferred


def resolve_configured_properties(schema: dict[str, Any]) -> None:
    global PROJECT_PROPERTY, DUE_DATE_PROPERTY, DB_ENTRY_TYPE_PROPERTY, ASSIGNEE_PROPERTY
    global PROJECT_STATUS_PROPERTY, STATUS_PROPERTY, PRIORITY_PROPERTY, TITLE_PROPERTY

    TITLE_PROPERTY = resolve_property_name(
        schema,
        TITLE_PROPERTY,
        ["MEETING / TASK NAME", "Task", "Task Name", "Name", "Title"],
    )
    PROJECT_PROPERTY = resolve_property_name(
        schema,
        PROJECT_PROPERTY,
        ["Projects", "Project", "PROJECTS", "PROJECT"],
    )
    DUE_DATE_PROPERTY = resolve_property_name(
        schema,
        DUE_DATE_PROPERTY,
        ["DUE DATE", "Due Date", "Due", "Due date"],
    )
    DB_ENTRY_TYPE_PROPERTY = resolve_property_name(
        schema,
        DB_ENTRY_TYPE_PROPERTY,
        ["DB ENTRY TYPE", "DB Entry Type", "Entry Type", "Type"],
    )
    ASSIGNEE_PROPERTY = resolve_property_name(
        schema,
        ASSIGNEE_PROPERTY,
        ["ASSIGNEE", "Assignee", "Assigned To", "Assigned", "Owner"],
    )
    PROJECT_STATUS_PROPERTY = resolve_property_name(
        schema,
        PROJECT_STATUS_PROPERTY,
        ["Project Status", "PROJECT STATUS", "Project status", "Project - Status"],
    )
    STATUS_PROPERTY = resolve_property_name(
        schema,
        STATUS_PROPERTY,
        ["STATUS", "Status", "Task Status", "Task status"],
    )
    PRIORITY_PROPERTY = resolve_property_name(
        schema,
        PRIORITY_PROPERTY,
        ["PRIORITY", "Priority"],
    )


def validate_required_properties(schema: dict[str, Any]) -> None:
    required = [
        DB_ENTRY_TYPE_PROPERTY,
        ASSIGNEE_PROPERTY,
        PROJECT_STATUS_PROPERTY,
        STATUS_PROPERTY,
    ]
    missing = [name for name in required if name not in schema]
    if missing:
        available = ", ".join(sorted(schema.keys()))
        raise RuntimeError(
            "Could not find required Notion property/properties: "
            + ", ".join(missing)
            + "\nRun dailyEmail/check_notion_task_schema.py and update the property names in the workflow or .env.\n"
            + f"Available properties: {available}"
        )


def or_filters(filters: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return {"or": filters}


def make_value_filter(schema: dict[str, Any], prop_name: str, values: Iterable[str]) -> dict[str, Any] | None:
    """Build a Notion server-side filter for common property types."""
    ptype = prop_type(schema, prop_name)
    values = [v for v in values if v]
    if not ptype or not values:
        return None

    filters: list[dict[str, Any]] = []
    for value in values:
        if ptype == "select":
            filters.append({"property": prop_name, "select": {"equals": value}})
        elif ptype == "status":
            filters.append({"property": prop_name, "status": {"equals": value}})
        elif ptype == "multi_select":
            filters.append({"property": prop_name, "multi_select": {"contains": value}})
        elif ptype == "rich_text":
            filters.append({"property": prop_name, "rich_text": {"contains": value}})
        elif ptype == "title":
            filters.append({"property": prop_name, "title": {"contains": value}})
        # Rollups/formulas are filtered locally below because their inner types vary.

    return or_filters(filters)


def make_people_filter(schema: dict[str, Any], prop_name: str, user_id: str) -> dict[str, Any] | None:
    if prop_type(schema, prop_name) == "people" and user_id:
        return {"property": prop_name, "people": {"contains": user_id}}
    return None


def make_due_filter(schema: dict[str, Any], prop_name: str, today_iso: str) -> dict[str, Any] | None:
    if prop_type(schema, prop_name) != "date":
        return None

    if DUE_MODE == "due_today_overdue":
        return {"property": prop_name, "date": {"on_or_before": today_iso}}

    if DUE_MODE == "next_7_days":
        # Date math is easier locally; keep server-side unfiltered.
        return None

    return None


def build_query(schema: dict[str, Any]) -> dict[str, Any]:
    tz = ZoneInfo(TIME_ZONE)
    today_iso = datetime.now(tz).date().isoformat()

    filters: list[dict[str, Any]] = []

    for maybe_filter in [
        make_value_filter(schema, DB_ENTRY_TYPE_PROPERTY, DB_ENTRY_TYPE_VALUES),
        make_value_filter(schema, STATUS_PROPERTY, STATUS_VALUES),
        make_people_filter(schema, ASSIGNEE_PROPERTY, NOTION_ASSIGNEE_USER_ID),
        make_due_filter(schema, DUE_DATE_PROPERTY, today_iso),
        # Project Status is often a rollup from the project relation; filtered locally.
    ]:
        if maybe_filter:
            filters.append(maybe_filter)

    query: dict[str, Any] = {}
    if filters:
        query["filter"] = {"and": filters} if len(filters) > 1 else filters[0]

    sorts: list[dict[str, Any]] = []
    if prop_type(schema, DUE_DATE_PROPERTY) == "date":
        sorts.append({"property": DUE_DATE_PROPERTY, "direction": "ascending"})
    if TITLE_PROPERTY and prop_type(schema, TITLE_PROPERTY) == "title":
        sorts.append({"property": TITLE_PROPERTY, "direction": "ascending"})
    query["sorts"] = sorts

    return query


# -----------------------------------------------------------------------------
# Property decoding
# -----------------------------------------------------------------------------


def text_from_rich_text(items: list[dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in items or [])


def property_to_values(prop: dict[str, Any]) -> list[str]:
    if not prop:
        return []

    ptype = prop.get("type")
    data = prop.get(ptype, None)

    if ptype == "title":
        return [text_from_rich_text(data)] if data else []
    if ptype == "rich_text":
        return [text_from_rich_text(data)] if data else []
    if ptype == "select":
        return [data.get("name", "")] if data else []
    if ptype == "status":
        return [data.get("name", "")] if data else []
    if ptype == "multi_select":
        return [item.get("name", "") for item in data or []]
    if ptype == "date":
        return [data.get("start", "")] if data else []
    if ptype == "people":
        values: list[str] = []
        for person in data or []:
            name = person.get("name")
            email_addr = (person.get("person") or {}).get("email")
            if name:
                values.append(name)
            if email_addr:
                values.append(email_addr)
        return values
    if ptype == "relation":
        return [retrieve_page_title(item.get("id", "")) for item in data or [] if item.get("id")]
    if ptype == "formula":
        ftype = data.get("type") if data else None
        if ftype == "string":
            return [data.get("string") or ""]
        if ftype == "number":
            return [str(data.get("number"))]
        if ftype == "boolean":
            return [str(data.get("boolean"))]
        if ftype == "date":
            date_value = data.get("date") or {}
            return [date_value.get("start", "")]
        return []
    if ptype == "rollup":
        rtype = data.get("type") if data else None
        if rtype == "array":
            values: list[str] = []
            for item in data.get("array", []):
                values.extend(property_to_values(item))
            return values
        if rtype == "number":
            return [str(data.get("number"))]
        if rtype == "date":
            date_value = data.get("date") or {}
            return [date_value.get("start", "")]
        return []
    if ptype == "checkbox":
        return [str(bool(data))]
    if ptype == "number":
        return [str(data)] if data is not None else []
    if ptype == "url":
        return [data or ""]
    if ptype == "email":
        return [data or ""]
    if ptype == "phone_number":
        return [data or ""]

    return []


def property_text(page: dict[str, Any], prop_name: str) -> str:
    return ", ".join(v for v in property_to_values(page.get("properties", {}).get(prop_name, {})) if v)


def property_values_lower(page: dict[str, Any], prop_name: str) -> set[str]:
    return {v.strip().lower() for v in property_to_values(page.get("properties", {}).get(prop_name, {})) if v.strip()}


def exact_value_match(page: dict[str, Any], prop_name: str, allowed_values: list[str]) -> bool:
    if not allowed_values:
        return True
    actual = property_values_lower(page, prop_name)
    allowed = {v.strip().lower() for v in allowed_values}
    return bool(actual & allowed)


def assignee_match(page: dict[str, Any]) -> bool:
    if NOTION_ASSIGNEE_USER_ID:
        # Already handled server-side. Still return True to avoid double filtering.
        return True
    if not ASSIGNEE_MATCHES:
        return True
    actual = property_values_lower(page, ASSIGNEE_PROPERTY)
    return bool(actual & set(ASSIGNEE_MATCHES))


def parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        # Handles YYYY-MM-DD and full datetime strings.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def due_mode_match(page: dict[str, Any]) -> bool:
    if DUE_MODE == "all_open":
        return True

    due_value = property_text(page, DUE_DATE_PROPERTY)
    due = parse_iso_date(due_value)
    if not due:
        return False

    today = datetime.now(ZoneInfo(TIME_ZONE)).date()
    if DUE_MODE == "due_today_overdue":
        return due <= today
    if DUE_MODE == "next_7_days":
        return due <= today.fromordinal(today.toordinal() + 7)
    return True


def extract_title(page: dict[str, Any]) -> str:
    props = page.get("properties", {})

    if TITLE_PROPERTY and TITLE_PROPERTY in props:
        title = property_text(page, TITLE_PROPERTY)
        if title:
            return title

    for name, prop in props.items():
        if prop.get("type") == "title":
            title = property_text(page, name)
            if title:
                return title

    return "Untitled"


def local_filters_match(page: dict[str, Any]) -> bool:
    return all(
        [
            exact_value_match(page, DB_ENTRY_TYPE_PROPERTY, DB_ENTRY_TYPE_VALUES),
            exact_value_match(page, STATUS_PROPERTY, STATUS_VALUES),
            exact_value_match(page, PROJECT_STATUS_PROPERTY, PROJECT_STATUS_VALUES),
            assignee_match(page),
            due_mode_match(page),
        ]
    )


def page_to_task(page: dict[str, Any]) -> Task:
    project_names = property_to_values(page.get("properties", {}).get(PROJECT_PROPERTY, {}))
    project_names = [p for p in project_names if p]
    if not project_names:
        project_names = ["No Project"]

    return Task(
        title=extract_title(page),
        url=page.get("url", ""),
        project_names=project_names,
        due_date=property_text(page, DUE_DATE_PROPERTY),
        status=property_text(page, STATUS_PROPERTY),
        priority=property_text(page, PRIORITY_PROPERTY),
        assignees=property_to_values(page.get("properties", {}).get(ASSIGNEE_PROPERTY, {})),
    )


def sort_key(task: Task) -> tuple[str, str, str]:
    # Blank due dates go last.
    due = task.due_date or "9999-12-31"
    project = task.project_names[0] if task.project_names else "No Project"
    return (project.lower(), due, task.title.lower())


# -----------------------------------------------------------------------------
# Email rendering
# -----------------------------------------------------------------------------


def fmt_date(value: str) -> str:
    parsed = parse_iso_date(value)
    if not parsed:
        return "—"
    return parsed.strftime("%b %-d, %Y") if os.name != "nt" else parsed.strftime("%b %#d, %Y")


def due_class(value: str) -> str:
    parsed = parse_iso_date(value)
    if not parsed:
        return ""
    today = datetime.now(ZoneInfo(TIME_ZONE)).date()
    if parsed < today:
        return "overdue"
    if parsed == today:
        return "today"
    return ""


def task_link(task: Task) -> str:
    title = html.escape(task.title)
    if task.url:
        return f'<a href="{html.escape(task.url)}">{title}</a>'
    return title


def build_html_email(tasks: list[Task]) -> str:
    now = datetime.now(ZoneInfo(TIME_ZONE))
    grouped: dict[str, list[Task]] = defaultdict(list)

    for task in sorted(tasks, key=sort_key):
        for project in task.project_names:
            grouped[project].append(task)

    project_sections = []
    for project in sorted(grouped.keys(), key=str.lower):
        rows = []
        for task in grouped[project]:
            rows.append(
                f"""
                <tr>
                    <td class="task-title">{task_link(task)}</td>
                    <td class="{due_class(task.due_date)}">{html.escape(fmt_date(task.due_date))}</td>
                    <td>{html.escape(task.status or '—')}</td>
                    <td>{html.escape(task.priority or '—')}</td>
                </tr>
                """
            )

        project_sections.append(
            f"""
            <h2>{html.escape(project)} <span>{len(grouped[project])}</span></h2>
            <table>
                <thead>
                    <tr>
                        <th>Task</th>
                        <th>Due</th>
                        <th>Status</th>
                        <th>Priority</th>
                    </tr>
                </thead>
                <tbody>{''.join(rows)}</tbody>
            </table>
            """
        )

    if not project_sections:
        project_sections.append("<p class='empty'>No matching active tasks found for the current filters.</p>")

    filter_summary = (
        f"DB ENTRY TYPE: {', '.join(DB_ENTRY_TYPE_VALUES)} · "
        f"ASSIGNEE: {', '.join(ASSIGNEE_MATCHES)} · "
        f"Project Status: {', '.join(PROJECT_STATUS_VALUES)} · "
        f"STATUS: {', '.join(STATUS_VALUES)} · "
        f"Due mode: {DUE_MODE}"
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: Arial, sans-serif; color: #222; line-height: 1.4; }}
.wrapper {{ max-width: 900px; margin: 0 auto; padding: 16px; }}
h1 {{ font-size: 24px; margin: 0 0 4px 0; }}
.subtitle {{ color: #666; margin: 0 0 16px 0; }}
.filter-summary {{ font-size: 12px; color: #666; background: #f5f5f5; padding: 10px; border-radius: 6px; margin-bottom: 18px; }}
h2 {{ font-size: 18px; margin: 24px 0 8px 0; border-bottom: 1px solid #ddd; padding-bottom: 4px; }}
h2 span {{ font-size: 12px; color: #666; font-weight: normal; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 12px; }}
th, td {{ text-align: left; border-bottom: 1px solid #e6e6e6; padding: 8px 6px; vertical-align: top; }}
th {{ font-size: 12px; text-transform: uppercase; color: #666; }}
a {{ color: #0b57d0; text-decoration: none; }}
.task-title {{ width: 55%; }}
.overdue {{ font-weight: bold; color: #b00020; }}
.today {{ font-weight: bold; color: #9a6700; }}
.empty {{ padding: 16px; background: #f5f5f5; border-radius: 6px; }}
.footer {{ margin-top: 28px; font-size: 12px; color: #777; }}
</style>
</head>
<body>
<div class="wrapper">
    <h1>Notion Tasks</h1>
    <p class="subtitle">{now.strftime('%A, %B %d, %Y')} · {len(tasks)} task{'s' if len(tasks) != 1 else ''}</p>
    <div class="filter-summary">{html.escape(filter_summary)}</div>
    {''.join(project_sections)}
    <p class="footer">Sent automatically from GitHub Actions using the RevtoNotion-connected Notion integration.</p>
</div>
</body>
</html>"""


def build_plain_text(tasks: list[Task]) -> str:
    grouped: dict[str, list[Task]] = defaultdict(list)
    for task in sorted(tasks, key=sort_key):
        for project in task.project_names:
            grouped[project].append(task)

    lines = [f"Notion Tasks - {date.today().isoformat()}", ""]
    for project in sorted(grouped.keys(), key=str.lower):
        lines.append(project)
        lines.append("-" * len(project))
        for task in grouped[project]:
            lines.append(f"- {task.title} | Due: {fmt_date(task.due_date)} | Status: {task.status or '—'} | Priority: {task.priority or '—'}")
            if task.url:
                lines.append(f"  {task.url}")
        lines.append("")
    if not tasks:
        lines.append("No matching active tasks found.")
    return "\n".join(lines)


def send_email(subject: str, html_body: str, text_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(EMAIL_FROM, [addr.strip() for addr in EMAIL_TO.split(",")], msg.as_string())


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------


def main() -> int:
    print("Reading Notion database schema...")
    schema = retrieve_database_schema(NOTION_TASKS_DATABASE_ID)
    resolve_configured_properties(schema)
    validate_required_properties(schema)

    print("Resolved property names:")
    for label, value in {
        "Title": TITLE_PROPERTY or "auto-detect title property",
        "Project": PROJECT_PROPERTY,
        "Due Date": DUE_DATE_PROPERTY,
        "DB Entry Type": DB_ENTRY_TYPE_PROPERTY,
        "Assignee": ASSIGNEE_PROPERTY,
        "Project Status": PROJECT_STATUS_PROPERTY,
        "Status": STATUS_PROPERTY,
        "Priority": PRIORITY_PROPERTY,
    }.items():
        print(f"  {label}: {value}")

    print("Building query from configured properties...")
    query = build_query(schema)
    print(f"Server-side query: {query}")

    print("Querying Notion tasks...")
    pages = query_database(NOTION_TASKS_DATABASE_ID, query)
    print(f"Pages returned before local filtering: {len(pages)}")

    filtered_pages = [page for page in pages if local_filters_match(page)]
    print(f"Pages after local filtering: {len(filtered_pages)}")

    tasks = [page_to_task(page) for page in filtered_pages]
    html_body = build_html_email(tasks)
    text_body = build_plain_text(tasks)

    today = datetime.now(ZoneInfo(TIME_ZONE)).strftime("%B %d, %Y")
    subject = f"Notion Tasks - {today}"

    print(f"Sending email to {EMAIL_TO}...")
    send_email(subject, html_body, text_body)
    print("Done.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
