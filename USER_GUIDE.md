# PunchCard — User Guide

PunchCard is a Windows desktop application that records time spent on Jira tickets. It can recognise Microsoft Teams calls, help you review and correct your recorded sessions, export them as CSV, or send eligible worklogs directly to Tempo.

## 1. Open PunchCard

1. Extract the downloaded PunchCard files to a folder on your Windows computer, if they came in a ZIP.
2. Double-click **PunchCard.exe**.
3. The small PunchCard **widget** appears. You can drag it using its header.

PunchCard also runs in the Windows **system tray** (the notification area by the clock). Closing the widget hides its window; it does **not** stop an active timer or quit the application. Use the tray icon to reopen the widget, access Timesheet or Settings, or **Quit** PunchCard completely.

## 2. Connect Jira

1. Open **Settings** from the widget or the system tray.
2. Expand **Jira connection**.
3. Enter your **Jira Cloud URL**, such as `https://company.atlassian.net`, and your Jira account email.
4. Paste your **Jira API token** and choose the token's **expiry date**.
5. Select **Save**. PunchCard retrieves your unfinished assigned tickets and their parent issues.

**Permissions needed**

- Jira API token: `read:jira-work`
- Jira account: **Browse Projects** (plus access to any restricted issues you need)

Use the **Refresh** control in the ticket picker whenever you want to fetch the latest Jira tickets. PunchCard remembers your connection details, so you don't need to re-enter the token every time you open it.

## 3. Track your work

1. In the widget, select the **Choose ticket** icon.
2. Search or browse the list and select an unfinished Jira ticket. Tracking starts for that ticket.
3. The widget shows the active ticket and elapsed time.
4. Select **Stop** to end the current session. Select **Start** to resume tracking your last selected ticket.

Choosing a different ticket completes the previous session and starts a new one. Your recorded sessions are saved locally, so you can review or correct them later.

**Windows locking:** Locking Windows pauses tracking. Unlocking does not automatically resume a ticket; use **Start** when you're ready.

**Closing the widget:** Use the **X** to hide the widget while PunchCard continues running in the tray. To exit the application, choose **Quit** from the tray menu.

## 4. Automatic Teams calls

PunchCard can recognise an active call in the Microsoft Teams desktop application and record it as a separate session. If you were tracking a Jira ticket, PunchCard normally resumes that ticket after the call ends.

Teams sessions initially have no Jira ticket assigned. To include one in Tempo, open **Timesheet** and associate the completed session with the appropriate ticket.

Call detection depends on the controls exposed by Teams, so it may not identify every call or the exact start/end time. Screen-sharing dialogs can temporarily hide call controls; PunchCard allows a short period for these interruptions before deciding a call has ended. Check the recorded times if precision matters.

PunchCard does **not** record meeting audio, video or transcripts.

## 5. Review and edit your timesheet

Open **Timesheet** from the widget or the system tray.

- Use the **left/right arrows** to move between days, **Today** to return to the current date, or the **date picker** to choose another day.
- Each session shows its activity and its Tempo synchronisation status, if relevant.
- Select the **Edit** icon to correct a completed session's times, title, notes or associated Jira ticket, then save the change.
- Select the **Delete** icon to remove a session after confirming.

An active session must be stopped before its times can be edited. PunchCard prevents edits that would overlap other recorded sessions.

### Export a CSV for any date range

1. In Timesheet, select the **Export CSV** icon.
2. Choose the **From** and **To** dates, both inclusive.
3. Select **Export** and save the file to your preferred location.

The export includes sessions overlapping your chosen range, with their full recorded start and end timestamps. **CSV export works without enabling Tempo synchronisation.** This is a general-purpose CSV export; it is not guaranteed to match a Tempo import template.

## 6. Optional: synchronise directly to Tempo

Direct Tempo synchronisation is **off by default**. You can continue using PunchCard purely for tracking and CSV export if you prefer.

To enable it:

1. Create a Tempo API token in Tempo's API integration settings.
2. Open PunchCard **Settings** and expand **Tempo synchronisation**.
3. Paste the **Tempo API token**, choose its **expiry date**, and select **Save**. Usually you can leave the API endpoint at its default.
4. Open **Timesheet** and switch on **Tempo sync** beside the daily total.

**Permissions needed**

- Tempo API token: **Worklogs → Manage**
- Jira account: **Browse Projects**, **Work on Issues**, **Edit Own Worklogs**, **Delete Own Worklogs**

When enabled, PunchCard attempts to synchronise eligible completed sessions automatically. Editing a previously synchronised session can update the corresponding Tempo worklog; deleting one can remove its linked worklog. You don't need to trigger synchronisation manually.

Unassigned Teams sessions remain local until you associate them with a Jira ticket. Very short sessions that cannot form a whole-minute Tempo worklog also remain local. PunchCard keeps the original recorded times locally while aligning submitted worklogs to whole minutes.

**Tempo changes made outside PunchCard aren't imported back into your timesheet.** Review any remote changes before editing a linked session locally. Turning synchronisation off prevents new automatic submissions; it does not delete existing Tempo worklogs.

### Understanding session statuses

| Status | Meaning |
|---|---|
| **Recording** | The session is still active. |
| **Local** | The session is stored locally and isn't currently being synchronised. |
| **Pending / Syncing** | A Tempo operation is queued or in progress. |
| **Synced** | Tempo acknowledged the worklog. |
| **Too short** | The session cannot be submitted as a whole-minute worklog. |
| **Failed** | Tempo rejected or failed the operation; hover over the status for details. |
| **Check Tempo** | The result is uncertain; check Tempo before attempting to create the worklog again. |

## 7. API token expiry and privacy

Settings shows the expiry date you entered for each token. PunchCard warns you when a token is close to expiring. If you renew a token, enter the replacement token and its new expiry date in Settings, then select **Save**.

PunchCard stores sessions and settings locally. API tokens are encrypted using Windows user-account protection. Do not share your application data or credentials with others. When moving PunchCard to another Windows account or computer, you may need to enter your tokens again.

## 8. Troubleshooting

| Problem | What to do |
|---|---|
| **No Jira tickets appear** | Check the Jira connection, token expiry and permissions in Settings, then use **Refresh** in Choose ticket. Only unfinished assigned issues are selectable. |
| **Nothing is appearing in Tempo** | Confirm Tempo sync is enabled in Timesheet, the Tempo token has the required permission, and the session is finished and linked to a Jira ticket. Check its Timesheet status. |
| **Tempo sync cannot be enabled** | Save a Tempo token and refresh Jira tickets, then try the switch again. |
| **Teams call wasn't detected correctly** | Check and edit the recorded session in Timesheet. Teams detection relies on its desktop accessibility controls. |
| **The PunchCard window disappeared** | Find the PunchCard icon in the Windows system tray and reopen it. |
| **A token expiry warning appears** | Renew the relevant API token and update it in Settings. |
| **A worklog says Check Tempo** | Inspect the worklog in Tempo before retrying to avoid recording the same time twice. |

For everyday use, the essentials are simple: **choose a ticket, let PunchCard record your time, review anything that needs correcting, then export a CSV or let Tempo sync do the rest.**
