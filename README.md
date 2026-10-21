# YouTube Channel Monitor

A local Streamlit app for monitoring YouTube channels, detecting new long-form videos, generating summaries, sending email notifications, and analyzing trends.

## Features

- Register YouTube channels by URL, @handle, or channel ID
- Browse videos by channel and open details, links, and summaries
- Summarize transcripts with `youtube-transcript-api` and Google Gemini
- Automatically distinguish long-form videos from Shorts
- Check for new videos on a schedule with the worker and Windows Task Scheduler
- Send email notifications and show new-video badges and an in-app notifications tab
- View weekly and monthly upload frequency, keyword frequency, and AI-generated trend summaries

## Project Structure

```
youtube-monitor/
├─ app.py                  # Main Streamlit app
├─ worker.py               # Scheduled worker
├─ transcript.py           # Transcript extraction
├─ summarizer.py           # Gemini summaries
├─ notifier.py             # SMTP email notifications
├─ register_scheduler.ps1  # Task Scheduler setup script
├─ requirements.txt
├─ .env.example
└─ youtube_monitor.db      # SQLite database (created automatically)
```

## Installation

```powershell
cd "C:\path\to\youtube-monitor"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Environment Variables

Copy `.env.example` to `.env` and add your credentials:

```powershell
Copy-Item .env.example .env
notepad .env
```

Required keys:
- `YOUTUBE_API_KEY` — Create a key after enabling YouTube Data API v3 in Google Cloud Console
- `GOOGLE_API_KEY` — Create a key in [Google AI Studio](https://aistudio.google.com/app/apikey)
- `SMTP_USER` / `SMTP_PASSWORD` — Use a [Gmail app password](https://myaccount.google.com/apppasswords) when sending mail through Gmail
- `NOTIFY_TO` — Email address that should receive notifications

## Running the App

### 1) Streamlit App

```powershell
streamlit run app.py
```

The app opens automatically at `http://localhost:8501`.

### 2) Run the Worker Manually

```powershell
python worker.py            # Check for new videos and send notifications
python worker.py --no-mail  # Update the database without sending notifications
```

### 3) Register the Scheduled Task (Hourly)

Run PowerShell **as Administrator**:

```powershell
cd "C:\path\to\youtube-monitor"
.\register_scheduler.ps1
```

Verify:
```powershell
Get-ScheduledTask -TaskName YouTubeMonitor_Worker
Start-ScheduledTask -TaskName YouTubeMonitor_Worker  # Run a test immediately
```

Remove:
```powershell
Unregister-ScheduledTask -TaskName YouTubeMonitor_Worker -Confirm:$false
```

Logs are written to `worker.log`.

## Limitations

- **NotebookLM** has no official API, so it cannot be called automatically. Gemini is used instead.
- Transcript retrieval depends on the unofficial `youtube-transcript-api`. Videos without transcripts cannot be summarized; a speech-to-text service such as Whisper could be added later.
- Shorts detection uses a heuristic: duration of 60 seconds or less, or a `#shorts` tag. It is not guaranteed to be fully accurate.
- The YouTube Data API daily quota is 10,000 units. Checking 100 channels hourly with `playlistItems` polling (1 unit per request) is within the quota.
- Gmail SMTP blocks regular account passwords; use an app password.
