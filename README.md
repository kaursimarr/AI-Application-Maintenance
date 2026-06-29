# 🤖 AI Application Maintenance System

An intelligent web application maintenance tool that lets users raise design or bug tickets, automatically generates AI-powered fixes, and gives the customer full control to **review**, **preview**, **diff**, **approve**, or **reject** every change before it goes live.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Setup & Installation](#setup--installation)
- [How to Run](#how-to-run)
- [How It Works](#how-it-works)
- [Issue Lifecycle](#issue-lifecycle)
- [API Routes](#api-routes)
- [Token Tracking](#token-tracking)
- [Tech Stack](#tech-stack)

---

## Overview

This system wraps the **Home Loan EMI Calculator** with an AI-powered maintenance dashboard. When a user notices a design problem (e.g. wrong button colour, bad layout), they raise a ticket. The AI engine analyses the HTML file, applies the fix, and puts the result up for human review. The customer can preview the fix live in an iframe, compare it line-by-line against the original using a built-in diff viewer, then accept or reject it with an optional rejection reason.

---

## Features

### 🎫 Issue Management
- Raise tickets with a title and description
- Real-time status tracking — Open → In Progress → In Review → Resolved / Rejected
- Search, export (JSON), and delete issues from the dashboard
- Resubmit rejected issues with pre-filled form

### 🤖 AI Engine
- Powered by **Groq API** using `llama-3.3-70b-versatile`
- Supports `.html`, `.css`, `.js` files (web analysis engine)
- Supports `.py` files (pytest retry loop — up to 3 attempts)
- AI returns bug explanation + UI/UX suggestions + full fixed file
- Fixed files stored in `output/` — never auto-applied without approval

### 👁 Human Review Workflow
- **Live Preview** — renders the AI-fixed HTML in a sandboxed iframe before deciding
- **Side-by-side Diff** — line-level diff with character highlights, added/removed counters, and scroll sync between panes
- **Approve** — promotes the fix to the live template immediately
- **Reject** — leaves the live file untouched; customer can provide a written reason
- Chained approvals — each approved fix becomes the base for the next ticket

### 🔄 Safety & Rollback
- On first run, a permanent `emi_calculator_ORIGINAL.html` master copy is saved
- **Restore Original** button undoes ALL approved changes in one click
- AI always works on the current approved version, never on a previous AI output

### 📊 Token Usage Drawer
- Fixed side tab button — opens a slide-in drawer from the right
- Daily usage bar (Groq free tier: 500,000 tokens/day)
- All-time stats: total, input, output tokens and average per issue
- **Live estimator** — updates as you type your issue description, showing estimated input/output/total tokens and how many will remain after submission
- Last 20 API call history table

---

## Project Structure

```
AI-Application-Maintenance/
│
├── app.py                          # Flask server — all routes
│
├── ai/
│   ├── ai_engine2.py               # AI engine — web + Python fix pipelines
│   └── groq_client.py              # Groq API client initialisation
│
├── templates/
│   ├── emi_calculator.html         # Live application (updated on approval)
│   ├── emi_calculator_ORIGINAL.html # Permanent master copy (never overwritten)
│   └── Maintainance_Dashboard.html # Maintenance dashboard UI
│
├── data/
│   ├── issues.json                 # All issue records
│   ├── resolved_issues.json        # AI results for resolved issues
│   └── token_usage.json            # Token usage log (auto-created)
│
├── input/                          # Staging area — AI reads from here
├── output/                         # AI-generated fixed files (fixed_<id>_*.html)
├── processed/                      # Archived originals before each AI fix
├── logs/                           # Structured log files per issue
│
├── .env                            # API keys (not committed to git)
└── requirements.txt                # Python dependencies
```

---

## Setup & Installation

### 1. Clone the repository
```bash
git clone <your-repo-url>
cd AI-Application-Maintenance
```

### 2. Install dependencies
```bash
pip install flask groq python-dotenv
```

### 3. Set up your API key
Create a `.env` file in the project root:
```
GROQ_API_KEY=your_groq_api_key_here
```
Get a free key at [console.groq.com](https://console.groq.com)

### 4. Create required data files
```bash
python -c "
import os, json
os.makedirs('data', exist_ok=True)
for f in ['data/issues.json', 'data/resolved_issues.json', 'data/token_usage.json']:
    if not os.path.exists(f):
        json.dump([], open(f, 'w'))
        print('Created', f)
"
```

---

## How to Run

```bash
python app.py
```

Then open your browser at:
```
http://127.0.0.1:5000        → EMI Calculator
http://127.0.0.1:5000/dashboard  → Maintenance Dashboard
```

> ⚠️ Always open via `http://127.0.0.1:5000` — **not** by double-clicking the HTML file. Flask routes only work through the server.

---

## How It Works

```
User submits ticket
       ↓
Flask saves issue (status: Open)
       ↓
Flask copies current emi_calculator.html → input/
       ↓
AI engine reads file + issue description
       ↓
Groq API (llama-3.3-70b) analyses & fixes
       ↓
Fixed file saved to output/fixed_<id>_emi_calculator.html
       ↓
Issue status → "In Review"
       ↓
Customer opens dashboard
       ├── 👁 Live Preview  → iframe renders the fixed page
       ├── 🔍 View Diff     → side-by-side line diff
       │
       ├── ✅ Approve → fixed file copied over templates/emi_calculator.html
       │              → status: Resolved
       │              → next ticket's AI starts from this approved version
       │
       └── ❌ Reject  → live file unchanged
                      → rejection reason saved to issues.json
                      → status: Rejected
```

---

## Issue Lifecycle

| Status | Meaning |
|---|---|
| `Open` | Just submitted, not yet picked up |
| `In Progress` | AI is currently analysing and fixing |
| `In Review` | AI fix is ready — waiting for customer decision |
| `Resolved` | Customer approved — fix is now live |
| `Rejected` | Customer rejected — original file unchanged |
| `AI Failed` | Groq API error or empty response |

---

## API Routes

| Method | Route | Description |
|---|---|---|
| `GET` | `/` | Serves the EMI Calculator |
| `GET` | `/dashboard` | Serves the Maintenance Dashboard |
| `POST` | `/submit_issue` | Submits a new issue and triggers AI |
| `GET` | `/get_issues` | Returns all issues as JSON |
| `POST` | `/approve_issue/<id>` | Approves fix — applies it live |
| `POST` | `/reject_issue/<id>` | Rejects fix with optional reason |
| `GET` | `/preview/<id>` | Returns fixed HTML for iframe preview |
| `GET` | `/diff/<id>` | Returns original + fixed HTML for diff view |
| `GET` | `/download/<id>` | Downloads fixed file as attachment |
| `POST` | `/restore_original` | Restores the very first original file |
| `DELETE` | `/delete_issue/<id>` | Deletes an issue record |
| `GET` | `/token_stats` | Returns token usage statistics |
| `POST` | `/estimate_tokens` | Returns token estimate for a given description |

---

## Token Tracking

Every Groq API call logs to `data/token_usage.json`:

```json
{
  "issue_id": "a1b2c3d4",
  "model": "llama-3.3-70b-versatile",
  "prompt_tokens": 1113,
  "completion_tokens": 1074,
  "total_tokens": 2187,
  "timestamp": "29-06-2026 14:22:10 UTC"
}
```

The dashboard drawer reads this file and shows:
- Daily usage vs the 500,000 token free tier limit
- All-time totals broken down by input and output
- Live estimate for the next submission based on current file size + description length

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python, Flask |
| AI Model | Groq API — `llama-3.3-70b-versatile` |
| Frontend | Vanilla HTML, CSS, JavaScript |
| Data Storage | JSON files (`data/`) |
| Python testing | pytest (for `.py` file fixes only) |
| Environment | python-dotenv |
