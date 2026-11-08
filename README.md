# PunchCard

**Track work against Jira tickets without spending your day logging time in Tempo.**

PunchCard is a lightweight Windows desktop time tracker for people who work with Jira and Tempo. It keeps a running record of your activity, can recognise Microsoft Teams calls, and lets you review or correct sessions before exporting them as CSV or synchronising eligible worklogs directly to Tempo.

The goal is simple: **spend less time recording what you worked on and more time doing it.**

## Features

- **Jira ticket tracking** — browse your assigned, unfinished issues, switch tickets, and track active work from a small desktop widget.
- **Teams call detection** — automatically record detected calls as separate sessions and resume an interrupted ticket when appropriate.
- **Daily timesheet** — review recorded sessions, correct times or ticket assignments, and remove entries you don't want to keep.
- **Date-range CSV export** — export sessions between two chosen dates without connecting to Tempo.
- **Optional Tempo synchronisation** — send eligible completed sessions as worklogs, with updates and deletions handled for linked entries. Automatic synchronisation is off by default.
- **Windows desktop integration** — a compact draggable window and system-tray controls; tracking can continue while the widget is hidden.

## Getting started

PunchCard runs on **Windows** and requires access to **Jira Cloud**. A Tempo account and API token are only needed for direct synchronisation.

### Run from source

The project uses [uv](https://docs.astral.sh/uv/) for Python and dependency management.

From the root of a clone of this repository:

```powershell
uv python install 3.14
uv sync
uv run punchcard
```

For a packaged Windows build, if one is published, extract it and run `PunchCard.exe` instead. You don't need Python or uv to use a packaged executable.

### Connect Jira

1. Open **Settings → Jira connection**.
2. Enter your Jira Cloud URL, account email and API token, along with the token expiry date.
3. Select **Save**, then use **Choose ticket** in the widget to start tracking.

**Jira permissions:** `read:jira-work` (API token) and **Browse Projects** (Jira account). You must also be able to access the relevant issues.

### Optional: connect Tempo

1. In **Settings → Tempo synchronisation**, enter your Tempo API token and its expiry date, then select **Save**.
2. Open **Timesheet** and enable **Tempo sync**.

**Tempo permissions:** **Worklogs → Manage** (API token). Your Jira account also needs **Browse Projects**, **Work on Issues**, **Edit Own Worklogs** and **Delete Own Worklogs** for the relevant projects.

PunchCard records sessions locally whether or not Tempo sync is enabled. Sessions without a Jira ticket, or too short to form a whole-minute Tempo worklog, remain local until eligible.

For everyday use, settings and troubleshooting, see the **[User Guide](USER_GUIDE.md)**, which is also accessible from inside PunchCard.

## How it works

1. Choose a Jira ticket to start a tracking session.
2. Switch tickets, pause or stop tracking as you work. Detected Teams calls can temporarily interrupt ticket tracking.
3. Review and edit completed sessions in the daily timesheet.
4. Export a CSV for any date range, or let Tempo synchronise eligible completed sessions automatically.

**Notes on synchronisation:** PunchCard retains original session timestamps locally and converts eligible worklogs to Tempo's minute-based times. Changes made directly in Tempo are not imported back into PunchCard. An uncertain API response is not automatically retried, to reduce the risk of duplicate worklogs.

## Privacy and limitations

- Sessions, preferences and synchronisation records are stored locally in SQLite. API tokens are encrypted with Windows user-account protection (DPAPI).
- PunchCard does **not** record Teams audio, video or transcripts. Call detection relies on accessibility information exposed by the desktop app and may occasionally miss or misidentify calls.
- CSV exports contain recorded activity data. They are general-purpose exports, not a guaranteed match for a particular Tempo import format.
- The current application is Windows-focused and is not an official Atlassian, Tempo or Microsoft product.

## Development

The codebase separates stateful application objects (`Jira`, `Tempo`, `Tracker`, `Timesheet`, etc.) from UI objects. `Application` constructs them and connects their notifications.

Run the included tests with:

```powershell
uv run python -m unittest discover -s tests -v
```

For implementation details, see [ARCHITECTURE.md](ARCHITECTURE.md).
