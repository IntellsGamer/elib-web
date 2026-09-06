# eLib — Electronic Library (Flask website)

A complete port of the desktop **tkinter** app to a **Flask + SQLite + Tailwind** website.

## Features (1:1 with the desktop version)

- Member sign-in / sign-up (lowercased usernames, display names with first-name welcomes, roles: Member 0 / Librarian 1 / Root 2 / System 3, suspension flag)
- Search by title/author + branch filter
- Multi-branch libraries (protected Main Library id=0 + branches)
- Book covers (admin upload, auto-resized to max 600×900 JPEG)
- Loan requests (10 active-book cap, duplicate-request guard, 14-day terms)
- Returns ($1/day overdue fine, return blocked until settled with a librarian)
- Admin console: books (add/edit/delete/mark available + covers), branches, requests (approve/deny), members (suspend/reinstate/delete/promote/demote/view loans + role hierarchy)
- Gregorian dates (Asia/Tehran timezone)
- English assistant (deterministic scoring engine, no torch — live catalog lookup, loan summaries, recommendations, follow-ups like "borrow it" + `/api/assistant`; teach it with `train_assistant.py`, see `TRAINING.md`)
- Tailwind + Inter/Fraunces + Font Awesome, LTR, responsive

## Run

```bash
pip install -r requirements.txt
python app.py
# http://127.0.0.1:5000
```

Default seeded account: `system / 123` (system admin).

> No database ships with this repo (`elib.db` is gitignored and auto-created
> on first run). To get an admin immediately, register the username `system`
> — or copy a desktop `elib.db` next to `app.py` (schema is compatible and
> legacy plaintext passwords auto-upgrade on first login).

Rebuild styles after editing templates (requires node):

```bash
npm install
npm run build:css
```

## Structure

```
app.py              # whole web app (auth, dashboard, loans, admin, assistant)
assistant_engine.py # conversational assistant (intents, live answers, follow-ups)
train_assistant.py  # teach phrasings + accuracy suite (no model weights)
assistant_examples.jsonl  # community-taught phrasings (optional, safe to commit)
TRAINING.md         # how to teach the assistant (never commit models/)
templates/          # base, index, login, register, dashboard, mybooks, account, admin, assistant
static/             # app.css (theme), app.js (interactions), tailwind.min.css (built), covers/ (uploads, gitignored)
elib.db             # auto-created on first run, gitignored (desktop-compatible schema)
requirements.txt    # Flask, pytz, Pillow
```

## Security

- New passwords are hashed with `werkzeug.security`.
- Legacy plaintext passwords are auto-upgraded to hashes on first successful login.
- Set `ELIB_SECRET` in production.

## Differences from the tkinter version

| Desktop | Web |
|---|---|
| `sentence-transformers` + torch | deterministic scoring engine (no heavy deps) + live answers + `/api/assistant` |
| `eLibUtil.disable_maximize_button` (Windows) | removed (desktop-only) |
| tkinter windows | responsive pages + Tailwind |
| `auth.py` (debug) | secure session login |
