# Updated Notion daily task email

This replaces the old one-file daily email script with a safer workflow:

- No hard-coded Notion tokens or email passwords
- Filters match the Notion view shown in the screenshot
- Tasks are grouped by project in the email
- Relation project IDs are resolved to project names
- GitHub Action uses current `checkout` and `setup-python` actions
- Workflow can run on a Central Time schedule and can still be run manually

## Files to replace/add

Copy these into the repo:

```text
.github/workflows/daily-email-actions.yml
requirements.txt
dailyEmail/todays_schedule_email.py
dailyEmail/check_notion_task_schema.py
.env.example
.gitignore
```

You can leave the older files in place temporarily, but the updated workflow runs:

```bash
python dailyEmail/todays_schedule_email.py
```

## Required GitHub repository secrets

In GitHub, go to:

`Settings > Secrets and variables > Actions > New repository secret`

Create these secrets:

| Secret | Purpose |
|---|---|
| `NOTION_TOKEN` | Secret token for the Notion connection/integration that has access to the HT Tasks database. In your screenshot this is the RevtoNotion connection. |
| `NOTION_TASKS_DATABASE_ID` | Database ID for the HT Tasks database/source. |
| `SMTP_PASSWORD` | Email app password or SMTP password for `ihartsook@huttonbuilds.com`. |

## Notion setup

The Notion source/database must be shared with the RevtoNotion connection. If it is not shared, the Notion API usually returns a 404 even when the ID is correct.

The default filter settings match your screenshot:

```text
DB ENTRY TYPE: Task
ASSIGNEE: Ian Hartsook
Project Status: In progress
STATUS: In progress, To-do
Grouped by: Projects
Sorted by: DUE DATE ascending
```

## Local PyCharm test

1. Copy `.env.example` to `.env`.
2. Paste your actual Notion token, database ID, and SMTP password in `.env`.
3. Run:

```bash
python dailyEmail/check_notion_task_schema.py
```

4. Confirm the property names match. Then run:

```bash
python dailyEmail/todays_schedule_email.py
```

## Common tweaks

If your project relation is named `Project` instead of `Projects`, set:

```text
PROJECT_PROPERTY=Project
```

If your title property is not auto-detected correctly, set:

```text
TITLE_PROPERTY=MEETING / TASK NAME
```

If you only want today and overdue tasks, set:

```text
DUE_MODE=due_today_overdue
```

If you want all active tasks from the screenshot-style filtered view, leave:

```text
DUE_MODE=all_open
```
