"""
Small helper to verify the HT Tasks database is accessible to the RevtoNotion connection
and to print the exact Notion property names/types the email script will see.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests


def load_dotenv() -> None:
    for path in [Path(".env"), Path(__file__).resolve().parent / ".env"]:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env(name: str, required: bool = False, default: str = "") -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


load_dotenv()

NOTION_TOKEN = env("NOTION_TOKEN", required=True)
NOTION_TASKS_DATABASE_ID = env("NOTION_TASKS_DATABASE_ID", required=True).replace("-", "")
NOTION_VERSION = env("NOTION_VERSION", default="2022-06-28")


def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def notion_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method,
        f"https://api.notion.com/v1/{path.lstrip('/')}",
        headers=notion_headers(),
        json=payload,
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Notion API error {response.status_code}:\n{response.text}")
    return response.json()


def main() -> None:
    database = notion_request("GET", f"databases/{NOTION_TASKS_DATABASE_ID}")
    title_parts = database.get("title", [])
    title = "".join(part.get("plain_text", "") for part in title_parts) or "Untitled database"

    print(f"Database: {title}")
    print(f"ID: {NOTION_TASKS_DATABASE_ID}")
    print("\nProperties:")
    print("-" * 70)

    for name, prop in sorted(database.get("properties", {}).items(), key=lambda item: item[0].lower()):
        ptype = prop.get("type", "unknown")
        extra = ""
        if ptype in {"select", "status", "multi_select"}:
            options = prop.get(ptype, {}).get("options", [])
            option_names = ", ".join(opt.get("name", "") for opt in options)
            extra = f" | options: {option_names}"
        elif ptype == "relation":
            relation = prop.get("relation", {})
            extra = f" | relation database: {relation.get('database_id', 'unknown')}"
        elif ptype == "rollup":
            rollup = prop.get("rollup", {})
            extra = f" | rollup function: {rollup.get('function', 'unknown')}"
        print(f"{name}  ->  {ptype}{extra}")

    print("\nTip: if one of your property names is different than the defaults, add a GitHub Secret")
    print("or .env entry such as PROJECT_PROPERTY=Project or TITLE_PROPERTY=Task.")


if __name__ == "__main__":
    main()
