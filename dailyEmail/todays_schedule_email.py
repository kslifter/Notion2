"""
Daily Notion task email

Designed for the HT Tasks database / RevtoNotion connection.

This version reads the changeable task view settings from:
    dailyEmail/task_email_config.json

That means routine edits to filters, sorting, grouping, and displayed columns can be
made in the JSON file without changing Python.

Credentials still come from environment variables / GitHub Secrets / local .env.
Do not hard-code Notion tokens, email passwords, or app passwords in this file.
"""

from __future__ import annotations

import html
import json
import os
import smtplib
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
# Credential / runtime configuration
# -----------------------------------------------------------------------------

NOTION_TOKEN = env("NOTION_TOKEN", required=True)
NOTION_TASKS_DATABASE_ID = env("NOTION_TASKS_DATABASE_ID", required=True).replace("-", "")
NOTION_VERSION = env("NOTION_VERSION", "2022-06-28")
TIME_ZONE = env("TIME_ZONE", "America/Chicago")
CONFIG_PATH = env("TASK_EMAIL_CONFIG", "dailyEmail/task_email_config.json")

EMAIL_FROM = env("EMAIL_FROM", "ian.hartsook@gmail.com")
EMAIL_TO = env("EMAIL_TO", "ihartsook@huttonbuilds.com")
SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587"))
SMTP_USERNAME = env("SMTP_USERNAME", EMAIL_FROM)
SMTP_PASSWORD = env("SMTP_PASSWORD", required=True)


# -----------------------------------------------------------------------------
# Data models
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    page: dict[str, Any]
    values: dict[str, list[str]]
    title: str
    url: str
    project_names: list[str]
    due_date: str


# -----------------------------------------------------------------------------
# Config loading / defaults
# -----------------------------------------------------------------------------


def default_config() -> dict[str, Any]:
    """Fallback config used only if the JSON is missing."""
    return {
        "email": {
            "title": "Notion Tasks by Project",
            "subject_prefix": "Notion Tasks by Project",
            "group_by": "project",
            "show_filter_summary": True,
        },
        "properties": {
            "title": "TASK",
            "project": "HT Project",
            "due_date": "DUE DATE",
            "db_entry_type": "DB ENTRY TYPE",
            "assignee": "ASSIGNEE",
            "project_status": "Project Status",
            "status": "STATUS",
            "priority": "PRIORITY",
            "discipline": "DISCIPLINE",
            "internal_project": "Internal Project",
        },
        "filter_options": {
            "tasks_only": {
                "enabled": True,
                "property_key": "db_entry_type",
                "operator": "any_equals",
                "values": ["Task"],
            },
            "assigned_to_ian": {
                "enabled": True,
                "property_key": "assignee",
                "operator": "contains_any",
                "values": ["Ian Hartsook", "ihartsook@huttonbuilds.com"],
            },
            "active_projects": {
                "enabled": True,
                "property_key": "project_status",
                "operator": "any_equals",
                "values": ["In progress"],
            },
            "task_status": {
                "enabled": True,
                "property_key": "status",
                "operator": "any_equals",
                "values": ["In progress"],
            },
            "internal_projects_only": {
                "enabled": False,
                "property_key": "internal_project",
                "operator": "checkbox_equals",
                "value": True,
            },
            "non_internal_projects_only": {
                "enabled": False,
                "property_key": "internal_project",
                "operator": "checkbox_equals",
                "value": False,
            },
            "due_today_or_overdue": {
                "enabled": False,
                "property_key": "due_date",
                "operator": "on_or_before_today",
            },
            "due_next_7_days": {
                "enabled": False,
                "property_key": "due_date",
                "operator": "within_next_days",
                "days": 7,
            },
        },
        "sort_options": {
            "project_name": {
                "enabled": True,
                "property_key": "project",
                "direction": "ascending",
                "local_only": True,
            },
            "due_date_asc": {
                "enabled": True,
                "property_key": "due_date",
                "direction": "ascending",
            },
            "task_name_asc": {
                "enabled": True,
                "property_key": "title",
                "direction": "ascending",
            },
            "priority_desc": {
                "enabled": False,
                "property_key": "priority",
                "direction": "descending",
            },
        },
        "display_columns": ["title", "due_date"],
    }


def load_config() -> dict[str, Any]:
    path = Path(CONFIG_PATH)
    if not path.is_absolute():
        repo_root_path = Path.cwd() / path
        script_relative_path = Path(__file__).resolve().parent.parent / path
        if repo_root_path.exists():
            path = repo_root_path
        elif script_relative_path.exists():
            path = script_relative_path
    if not path.exists():
        print(f"WARNING: Config file not found at {CONFIG_PATH}. Using built-in defaults.")
        return default_config()
    try:
        with path.open("r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse JSON config file {path}: {exc}") from exc
    return merge_defaults(default_config(), config)


def merge_defaults(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Shallow/deep merge so config can omit unchanged sections."""
    merged = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


CONFIG = load_config()


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
        title = extract_title_from_page(page, {}) or page_id[:8]
    except Exception:
        title = page_id[:8]

    PAGE_TITLE_CACHE[page_id] = title
    return title


# -----------------------------------------------------------------------------
# Schema/property helpers
# -----------------------------------------------------------------------------


def prop_type(schema: dict[str, Any], prop_name: str) -> str | None:
    prop = schema.get(prop_name)
    return prop.get("type") if prop else None


def normalize_prop_name(name: str) -> str:
    return "".join(ch.lower() for ch in name if ch.isalnum())


def resolve_property_name(schema: dict[str, Any], preferred: str, candidates: list[str] | None = None) -> str:
    """Resolve a configured property name against the real Notion schema."""
    candidates = candidates or []
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


def resolve_property_map(schema: dict[str, Any], config: dict[str, Any]) -> dict[str, str]:
    raw = config.get("properties", {}) or {}
    resolved: dict[str, str] = {}

    candidate_map = {
        "title": ["TASK", "MEETING / TASK NAME", "Task", "Task Name", "Name", "Title"],
        "project": ["HT Project", "Projects", "Project", "PROJECTS", "PROJECT"],
        "due_date": ["DUE DATE", "Due Date", "Due", "Due date"],
        "db_entry_type": ["DB ENTRY TYPE", "DB Entry Type", "Entry Type", "Type"],
        "assignee": ["ASSIGNEE", "Assignee", "Assigned To", "Assigned", "Owner"],
        "project_status": ["Project Status", "PROJECT STATUS", "Project status", "Project - Status"],
        "status": ["STATUS", "Status", "Task Status", "Task status"],
        "priority": ["PRIORITY", "Priority"],
        "discipline": ["DISCIPLINE", "Discipline"],
        "internal_project": ["Internal Project", "Internal", "Internal?"],
    }

    for key, preferred in raw.items():
        resolved[key] = resolve_property_name(schema, str(preferred or ""), candidate_map.get(key, []))

    return resolved


def require_property(schema: dict[str, Any], prop_name: str, logical_name: str) -> None:
    if not prop_name or prop_name not in schema:
        available = ", ".join(sorted(schema.keys()))
        raise RuntimeError(
            f"Could not find required Notion property for '{logical_name}': {prop_name}\n"
            "Update dailyEmail/task_email_config.json.\n"
            f"Available properties: {available}"
        )


def enabled_items(options: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []
    for name, item in (options or {}).items():
        if isinstance(item, dict) and item.get("enabled", False):
            items.append((name, item))
    return items


def property_for_key(prop_map: dict[str, str], key: str) -> str:
    return prop_map.get(key, key)


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
            person_id = person.get("id")
            if name:
                values.append(name)
            if email_addr:
                values.append(email_addr)
            if person_id:
                values.append(person_id)
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
        return [str(bool(data)).lower()]
    if ptype == "number":
        return [str(data)] if data is not None else []
    if ptype == "url":
        return [data or ""]
    if ptype == "email":
        return [data or ""]
    if ptype == "phone_number":
        return [data or ""]

    return []


def property_values(page: dict[str, Any], prop_name: str) -> list[str]:
    return [v for v in property_to_values(page.get("properties", {}).get(prop_name, {})) if str(v).strip()]


def property_text(page: dict[str, Any], prop_name: str) -> str:
    return ", ".join(property_values(page, prop_name))


def lower_values(values: Iterable[Any]) -> list[str]:
    return [str(v).strip().lower() for v in values if str(v).strip()]


def parse_iso_date(value: str) -> date | None:
    if not value:
        return None
    try:
        # Handles YYYY-MM-DD and full datetime strings.
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def extract_title_from_page(page: dict[str, Any], prop_map: dict[str, str]) -> str:
    props = page.get("properties", {})
    configured_title = prop_map.get("title", "")

    if configured_title and configured_title in props:
        title = property_text(page, configured_title)
        if title:
            return title

    for name, prop in props.items():
        if prop.get("type") == "title":
            title = property_text(page, name)
            if title:
                return title

    return "Untitled"


# -----------------------------------------------------------------------------
# Dynamic filter building and matching
# -----------------------------------------------------------------------------


def list_values(condition: dict[str, Any]) -> list[str]:
    if "values" in condition:
        raw = condition.get("values") or []
        return [str(v) for v in raw if str(v).strip()]
    if "value" in condition:
        value = condition.get("value")
        if isinstance(value, bool):
            return [str(value).lower()]
        return [str(value)] if str(value).strip() else []
    return []


def condition_label(name: str, condition: dict[str, Any], prop_map: dict[str, str]) -> str:
    prop = property_for_key(prop_map, condition.get("property_key", ""))
    values = list_values(condition)
    op = condition.get("operator", "any_equals")
    if op in {"on_or_before_today", "before_today", "on_or_after_today", "is_empty", "is_not_empty"}:
        value_text = op.replace("_", " ")
    elif op == "within_next_days":
        value_text = f"next {condition.get('days', 7)} days"
    else:
        value_text = ", ".join(values)
    return f"{prop}: {value_text}" if value_text else prop


def make_single_server_value_filter(ptype: str, prop_name: str, operator: str, value: str) -> dict[str, Any] | None:
    # Only include filters the Notion API can safely do server-side. Everything is
    # checked again locally after results come back.
    if ptype == "select":
        if operator in {"any_equals", "equals"}:
            return {"property": prop_name, "select": {"equals": value}}
        if operator == "does_not_equal":
            return {"property": prop_name, "select": {"does_not_equal": value}}
    if ptype == "status":
        if operator in {"any_equals", "equals"}:
            return {"property": prop_name, "status": {"equals": value}}
        if operator == "does_not_equal":
            return {"property": prop_name, "status": {"does_not_equal": value}}
    if ptype == "multi_select":
        if operator in {"any_equals", "contains", "contains_any"}:
            return {"property": prop_name, "multi_select": {"contains": value}}
        if operator == "does_not_contain":
            return {"property": prop_name, "multi_select": {"does_not_contain": value}}
    if ptype == "rich_text":
        if operator in {"contains", "contains_any"}:
            return {"property": prop_name, "rich_text": {"contains": value}}
        if operator in {"any_equals", "equals"}:
            return {"property": prop_name, "rich_text": {"equals": value}}
    if ptype == "title":
        if operator in {"contains", "contains_any"}:
            return {"property": prop_name, "title": {"contains": value}}
        if operator in {"any_equals", "equals"}:
            return {"property": prop_name, "title": {"equals": value}}
    if ptype == "people" and operator == "people_contains_id":
        return {"property": prop_name, "people": {"contains": value}}
    if ptype == "relation" and operator == "relation_contains_id":
        return {"property": prop_name, "relation": {"contains": value}}
    return None


def make_server_filter(schema: dict[str, Any], prop_map: dict[str, str], condition: dict[str, Any]) -> dict[str, Any] | None:
    if condition.get("server_side", True) is False:
        return None

    prop_key = condition.get("property_key", "")
    prop_name = property_for_key(prop_map, prop_key)
    ptype = prop_type(schema, prop_name)
    operator = condition.get("operator", "any_equals")
    if not prop_name or not ptype:
        return None

    if operator == "checkbox_equals" and ptype == "checkbox":
        return {"property": prop_name, "checkbox": {"equals": bool(condition.get("value"))}}

    if ptype == "date":
        today = datetime.now(ZoneInfo(TIME_ZONE)).date().isoformat()
        if operator == "on_or_before_today":
            return {"property": prop_name, "date": {"on_or_before": today}}
        if operator == "before_today":
            return {"property": prop_name, "date": {"before": today}}
        if operator == "on_or_after_today":
            return {"property": prop_name, "date": {"on_or_after": today}}
        if operator == "is_empty":
            return {"property": prop_name, "date": {"is_empty": True}}
        if operator == "is_not_empty":
            return {"property": prop_name, "date": {"is_not_empty": True}}
        # within_next_days is intentionally local so blanks/edge cases are clear.

    if operator == "is_empty":
        return {"property": prop_name, ptype: {"is_empty": True}} if ptype in {"title", "rich_text", "url", "email", "phone_number"} else None
    if operator == "is_not_empty":
        return {"property": prop_name, ptype: {"is_not_empty": True}} if ptype in {"title", "rich_text", "url", "email", "phone_number"} else None

    value_filters = [
        f for value in list_values(condition)
        if (f := make_single_server_value_filter(ptype, prop_name, operator, value))
    ]
    if not value_filters:
        return None
    if len(value_filters) == 1:
        return value_filters[0]
    return {"or": value_filters}


def condition_matches(page: dict[str, Any], prop_map: dict[str, str], condition: dict[str, Any]) -> bool:
    prop_key = condition.get("property_key", "")
    prop_name = property_for_key(prop_map, prop_key)
    operator = condition.get("operator", "any_equals")
    actual_raw = property_values(page, prop_name)
    actual = lower_values(actual_raw)
    expected = lower_values(list_values(condition))

    if operator == "is_empty":
        return not actual
    if operator == "is_not_empty":
        return bool(actual)

    if operator == "checkbox_equals":
        desired = bool(condition.get("value"))
        return str(desired).lower() in actual

    if operator == "any_equals":
        if not expected:
            return True
        return bool(set(actual) & set(expected))

    if operator == "equals":
        if not expected:
            return True
        return actual == expected or bool(set(actual) & set(expected))

    if operator == "contains_any":
        if not expected:
            return True
        return any(exp in act or act == exp for exp in expected for act in actual)

    if operator == "contains":
        if not expected:
            return True
        needle = expected[0]
        return any(needle in act for act in actual)

    if operator == "does_not_equal":
        return not bool(set(actual) & set(expected))

    if operator == "does_not_contain":
        return not any(exp in act for exp in expected for act in actual)

    if operator in {"people_contains_id", "relation_contains_id"}:
        return bool(set(actual) & set(expected))

    if operator in {"on_or_before_today", "before_today", "on_or_after_today", "within_next_days"}:
        first_date = parse_iso_date(actual_raw[0]) if actual_raw else None
        if not first_date:
            return False
        today = datetime.now(ZoneInfo(TIME_ZONE)).date()
        if operator == "on_or_before_today":
            return first_date <= today
        if operator == "before_today":
            return first_date < today
        if operator == "on_or_after_today":
            return first_date >= today
        if operator == "within_next_days":
            days = int(condition.get("days", 7))
            return first_date <= today + timedelta(days=days)

    # Unknown operators should not silently include everything.
    raise RuntimeError(f"Unsupported filter operator in config: {operator}")


def local_filters_match(page: dict[str, Any], prop_map: dict[str, str], config: dict[str, Any]) -> bool:
    for _name, condition in enabled_items(config.get("filter_options", {})):
        if not condition_matches(page, prop_map, condition):
            return False
    return True


def build_query(schema: dict[str, Any], prop_map: dict[str, str], config: dict[str, Any]) -> dict[str, Any]:
    filters: list[dict[str, Any]] = []
    for _name, condition in enabled_items(config.get("filter_options", {})):
        maybe_filter = make_server_filter(schema, prop_map, condition)
        if maybe_filter:
            filters.append(maybe_filter)

    query: dict[str, Any] = {}
    if filters:
        query["filter"] = {"and": filters} if len(filters) > 1 else filters[0]

    sorts: list[dict[str, Any]] = []
    for _name, sort in enabled_items(config.get("sort_options", {})):
        if sort.get("local_only"):
            continue
        prop_name = property_for_key(prop_map, sort.get("property_key", ""))
        direction = sort.get("direction", "ascending")
        if prop_name in schema and direction in {"ascending", "descending"}:
            sorts.append({"property": prop_name, "direction": direction})
    if sorts:
        query["sorts"] = sorts

    return query


# -----------------------------------------------------------------------------
# Task conversion / sorting
# -----------------------------------------------------------------------------


def values_for_key(page: dict[str, Any], prop_map: dict[str, str], key: str) -> list[str]:
    prop_name = property_for_key(prop_map, key)
    return property_values(page, prop_name)


def text_for_key(page: dict[str, Any], prop_map: dict[str, str], key: str) -> str:
    return ", ".join(values_for_key(page, prop_map, key))


def page_to_task(page: dict[str, Any], prop_map: dict[str, str], config: dict[str, Any]) -> Task:
    display_keys = list(dict.fromkeys(["title", "due_date", config.get("email", {}).get("group_by", "project")] + config.get("display_columns", [])))
    values = {key: values_for_key(page, prop_map, key) for key in display_keys if key}

    title = extract_title_from_page(page, prop_map)
    project_names = values_for_key(page, prop_map, "project")
    project_names = [p for p in project_names if p]
    if not project_names:
        project_names = ["No Project"]

    return Task(
        page=page,
        values=values,
        title=title,
        url=page.get("url", ""),
        project_names=project_names,
        due_date=text_for_key(page, prop_map, "due_date"),
    )


def sort_value(task: Task, key: str) -> str:
    if key == "title":
        return task.title.lower()
    values = task.values.get(key) or []
    if key == "due_date":
        return values[0] if values else "9999-12-31"
    if key == "project":
        return (task.project_names[0] if task.project_names else "No Project").lower()
    return ", ".join(values).lower()


def local_sort_key(task: Task, config: dict[str, Any]) -> tuple[Any, ...]:
    parts: list[Any] = []
    for _name, sort in enabled_items(config.get("sort_options", {})):
        key = sort.get("property_key", "")
        value = sort_value(task, key)
        if sort.get("direction") == "descending":
            # Simple string descending by inverting code points. Good enough for stable grouping/sorting.
            value = "".join(chr(0x10FFFF - ord(ch)) for ch in value)
        parts.append(value)
    if not parts:
        parts = [sort_value(task, "project"), sort_value(task, "due_date"), sort_value(task, "title")]
    return tuple(parts)


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


def display_column_label(key: str, prop_map: dict[str, str]) -> str:
    if key == "title":
        return "Task"
    if key == "due_date":
        return "Due"
    return prop_map.get(key, key).replace("_", " ")


def display_cell(task: Task, key: str) -> str:
    if key == "title":
        return task_link(task)
    if key == "due_date":
        due = fmt_date(task.due_date)
        return f'<span class="due {due_class(task.due_date)}">{html.escape(due)}</span>'
    values = task.values.get(key) or []
    return html.escape(", ".join(values) if values else "—")


def group_name_for_task(task: Task, group_by: str) -> list[str]:
    if group_by == "project":
        return task.project_names
    values = task.values.get(group_by) or []
    return values or ["No Group"]


def build_filter_summary(config: dict[str, Any], prop_map: dict[str, str]) -> str:
    labels = [condition_label(name, condition, prop_map) for name, condition in enabled_items(config.get("filter_options", {}))]
    return " · ".join(labels)


def build_html_email(tasks: list[Task], prop_map: dict[str, str], config: dict[str, Any]) -> str:
    now = datetime.now(ZoneInfo(TIME_ZONE))
    email_cfg = config.get("email", {})
    title = email_cfg.get("title", "Notion Tasks by Project")
    group_by = email_cfg.get("group_by", "project")
    display_columns = config.get("display_columns", ["title", "due_date"])

    grouped: dict[str, list[Task]] = defaultdict(list)
    for task in sorted(tasks, key=lambda t: local_sort_key(t, config)):
        for group_name in group_name_for_task(task, group_by):
            grouped[group_name].append(task)

    summary_items = []
    for group_name in sorted(grouped.keys(), key=str.lower):
        count = len(grouped[group_name])
        summary_items.append(
            f'<span class="project-chip"><strong>{html.escape(group_name)}</strong> <em>{count}</em></span>'
        )

    project_sections = []
    for group_name in sorted(grouped.keys(), key=str.lower):
        header_cells = "".join(f"<th>{html.escape(display_column_label(key, prop_map))}</th>" for key in display_columns)
        rows = []
        for task in grouped[group_name]:
            cells = "".join(f'<td class="col-{html.escape(key)}">{display_cell(task, key)}</td>' for key in display_columns)
            rows.append(f"<tr>{cells}</tr>")

        project_sections.append(
            f"""
            <section class="project-section">
                <div class="project-header">
                    <span class="caret">▾</span>
                    <span class="project-name">{html.escape(group_name)}</span>
                    <span class="count">{len(grouped[group_name])}</span>
                </div>
                <table>
                    <thead><tr>{header_cells}</tr></thead>
                    <tbody>{''.join(rows)}</tbody>
                </table>
            </section>
            """
        )

    if not project_sections:
        project_sections.append("<p class='empty'>No matching active tasks found for the current filters.</p>")

    filter_summary_html = ""
    if email_cfg.get("show_filter_summary", True):
        filter_summary = build_filter_summary(config, prop_map)
        filter_summary_html = f'<div class="filter-summary">{html.escape(filter_summary)}</div>' if filter_summary else ""

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: Arial, sans-serif; color: #222; line-height: 1.4; background: #ffffff; }}
.wrapper {{ max-width: 1080px; margin: 0 auto; padding: 16px; }}
h1 {{ font-size: 24px; margin: 0 0 4px 0; }}
.subtitle {{ color: #666; margin: 0 0 16px 0; }}
.filter-summary {{ font-size: 12px; color: #666; background: #f5f5f5; padding: 10px; border-radius: 8px; margin-bottom: 16px; }}
.summary {{ margin: 0 0 18px 0; }}
.project-chip {{ display: inline-block; margin: 0 6px 8px 0; padding: 6px 10px; border: 1px solid #dedede; border-radius: 999px; background: #fafafa; font-size: 13px; }}
.project-chip em {{ font-style: normal; color: #666; margin-left: 4px; }}
.project-section {{ margin: 18px 0 22px 0; border: 1px solid #e5e5e5; border-radius: 10px; overflow: hidden; }}
.project-header {{ background: #f7f7f5; padding: 10px 12px; font-size: 16px; font-weight: 700; }}
.caret {{ color: #555; margin-right: 6px; }}
.project-name {{ text-decoration: underline; }}
.count {{ display: inline-block; margin-left: 8px; color: #666; font-size: 12px; font-weight: 400; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; border-bottom: 1px solid #eeeeee; padding: 9px 12px; vertical-align: top; }}
tr:last-child td {{ border-bottom: none; }}
th {{ font-size: 12px; text-transform: uppercase; color: #666; background: #fcfcfc; }}
a {{ color: #0b57d0; text-decoration: none; }}
.col-title {{ width: 70%; }}
.col-due_date {{ width: 160px; white-space: nowrap; }}
.due {{ white-space: nowrap; }}
.overdue {{ font-weight: bold; color: #b00020; }}
.today {{ font-weight: bold; color: #9a6700; }}
.empty {{ padding: 16px; background: #f5f5f5; border-radius: 6px; }}
.footer {{ margin-top: 28px; font-size: 12px; color: #777; }}
</style>
</head>
<body>
<div class="wrapper">
    <h1>{html.escape(title)}</h1>
    <p class="subtitle">{now.strftime('%A, %B %d, %Y')} · {len(tasks)} task{'s' if len(tasks) != 1 else ''}</p>
    {filter_summary_html}
    <div class="summary">{''.join(summary_items)}</div>
    {''.join(project_sections)}
    <p class="footer">Sent automatically from GitHub Actions using the RevtoNotion-connected Notion integration.</p>
</div>
</body>
</html>"""


def build_plain_text(tasks: list[Task], config: dict[str, Any]) -> str:
    group_by = config.get("email", {}).get("group_by", "project")
    display_columns = config.get("display_columns", ["title", "due_date"])
    grouped: dict[str, list[Task]] = defaultdict(list)
    for task in sorted(tasks, key=lambda t: local_sort_key(t, config)):
        for group_name in group_name_for_task(task, group_by):
            grouped[group_name].append(task)

    lines = [f"{config.get('email', {}).get('title', 'Notion Tasks')} - {date.today().isoformat()}", ""]
    for group_name in sorted(grouped.keys(), key=str.lower):
        lines.append(f"▾ {group_name} ({len(grouped[group_name])})")
        lines.append("-" * (len(group_name) + 6))
        for task in grouped[group_name]:
            parts = []
            for key in display_columns:
                if key == "title":
                    parts.append(task.title)
                elif key == "due_date":
                    parts.append(f"Due: {fmt_date(task.due_date)}")
                else:
                    values = task.values.get(key) or []
                    parts.append(f"{key}: {', '.join(values) if values else '—'}")
            lines.append("- " + " | ".join(parts))
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
    print(f"Reading config from {CONFIG_PATH}...")
    print("Reading Notion database schema...")
    schema = retrieve_database_schema(NOTION_TASKS_DATABASE_ID)
    prop_map = resolve_property_map(schema, CONFIG)

    # Required logical properties for the default task email.
    for logical_key in ["title", "project", "due_date", "db_entry_type", "assignee", "project_status", "status"]:
        require_property(schema, property_for_key(prop_map, logical_key), logical_key)

    print("Resolved property names:")
    for logical_key, prop_name in prop_map.items():
        status = "OK" if prop_name in schema else "not found/unused"
        print(f"  {logical_key}: {prop_name} ({status})")

    print("Enabled filters:")
    for name, condition in enabled_items(CONFIG.get("filter_options", {})):
        print(f"  {name}: {condition_label(name, condition, prop_map)}")

    print("Enabled sorts:")
    for name, sort in enabled_items(CONFIG.get("sort_options", {})):
        prop_name = property_for_key(prop_map, sort.get("property_key", ""))
        print(f"  {name}: {prop_name} {sort.get('direction', 'ascending')}")

    print("Building query from JSON config...")
    query = build_query(schema, prop_map, CONFIG)
    print(f"Server-side query: {query}")

    print("Querying Notion tasks...")
    pages = query_database(NOTION_TASKS_DATABASE_ID, query)
    print(f"Pages returned before local filtering: {len(pages)}")

    filtered_pages = [page for page in pages if local_filters_match(page, prop_map, CONFIG)]
    print(f"Pages after local filtering: {len(filtered_pages)}")

    tasks = [page_to_task(page, prop_map, CONFIG) for page in filtered_pages]
    html_body = build_html_email(tasks, prop_map, CONFIG)
    text_body = build_plain_text(tasks, CONFIG)

    today = datetime.now(ZoneInfo(TIME_ZONE)).strftime("%B %d, %Y")
    subject_prefix = CONFIG.get("email", {}).get("subject_prefix", "Notion Tasks")
    subject = f"{subject_prefix} - {today}"

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
