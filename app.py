# app.py — eLib Electronic Library (Flask website port of tkinter app)
import os
import difflib
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

import pytz
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "elib.db")

TEHRAN = pytz.timezone("Asia/Tehran")
ROLE_NAMES = {0: "Member", 1: "Librarian", 2: "Root", 3: "System"}

MAX_ACTIVE_LOANS = 10
LOAN_DAYS = 14
OVERDUE_FEE_PER_DAY = 1  # USD

app = Flask(__name__)
app.secret_key = os.environ.get("ELIB_SECRET", "elib-dev-secret-change-me")

# ---------------- DB ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS libraries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                code TEXT UNIQUE NOT NULL
            )
        """)
        cur.execute("INSERT OR IGNORE INTO libraries (id, name, code) VALUES (0, 'Main Library', '0')")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                author TEXT NOT NULL,
                available INTEGER DEFAULT 1,
                library_id INTEGER DEFAULT 0,
                FOREIGN KEY (library_id) REFERENCES libraries (id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS borrow_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                book_id INTEGER,
                borrow_date TEXT,
                return_date TEXT,
                status TEXT DEFAULT 'pending',
                expire_date TEXT
            )
        """)
        # migrations for old DBs
        for col, ddl in [
            ("library_id", "ALTER TABLE books ADD COLUMN library_id INTEGER DEFAULT 0"),
            ("status", "ALTER TABLE borrow_log ADD COLUMN status TEXT DEFAULT 'pending'"),
            ("expire_date", "ALTER TABLE borrow_log ADD COLUMN expire_date TEXT"),
        ]:
            try:
                cur.execute(f"PRAGMA table_info({'books' if col=='library_id' else 'borrow_log'})")
                cols = [r[1] for r in cur.fetchall()]
                if col not in cols:
                    cur.execute(ddl)
            except Exception:
                pass
        # close stale denied requests left open by older versions:
        # a denied request is finished and must not linger on the active shelf
        try:
            cur.execute(
                "UPDATE borrow_log SET return_date=? WHERE status='denied' AND return_date IS NULL",
                (datetime.now().isoformat(),),
            )
        except Exception:
            pass
        conn.commit()

init_db()

# ---------------- helpers ----------------

def format_date(iso_str, with_time=False):
    """English Gregorian date for the UI. Falls back to raw string."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = pytz.utc.localize(dt)
        dt = dt.astimezone(TEHRAN)
        return dt.strftime("%b %d, %Y %H:%M" if with_time else "%b %d, %Y")
    except Exception:
        return iso_str

def days_left(expire_iso):
    try:
        exp = datetime.fromisoformat(expire_iso)
        if exp.tzinfo is None:
            exp = pytz.utc.localize(exp)
        now = datetime.now(pytz.utc)
        return (exp - now).days
    except Exception:
        return None

app.jinja_env.filters["fmt_date"] = lambda v: format_date(v, with_time=True)
app.jinja_env.filters["fmt_day"] = format_date
# legacy aliases (old templates used jalali filters)
app.jinja_env.filters["jalali"] = lambda v: format_date(v, with_time=True)
app.jinja_env.filters["jalali_date"] = format_date

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM users WHERE id=?", (uid,))
        row = cur.fetchone()
        return dict(row) if row else None

@app.context_processor
def inject_user():
    u = current_user()
    return dict(me=u, role_names=ROLE_NAMES)

def verify_password(stored, provided):
    try:
        if stored and ("$" in stored or ":" in stored) and len(stored) > 30:
            if check_password_hash(stored, provided):
                return True, False
    except Exception:
        pass
    # legacy plaintext fallback (desktop app stored raw passwords)
    if stored == provided:
        return True, True
    return False, False

def login_required(f):
    @wraps(f)
    def w(*a, **kw):
        if not session.get("user_id"):
            flash("Please sign in to continue.", "warn")
            return redirect(url_for("login", next=request.path))
        u = current_user()
        if not u:
            session.clear()
            return redirect(url_for("login"))
        if u["banned"]:
            session.clear()
            flash("Your account has been suspended. Please contact a librarian.", "error")
            return redirect(url_for("login"))
        return f(*a, **kw)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if (u["role"] or 0) < 1:
            flash("Access denied: librarian role required.", "error")
            return redirect(url_for("dashboard"))
        return f(*a, **kw)
    return w

def can_modify(me, target_role, target_id):
    if not me or target_id == me["id"]:
        return False
    return (me["role"] or 0) > (target_role or 0)

def wants_json():
    """True when the client asked for JSON (AJAX) instead of a page redirect."""
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return "application/json" in request.headers.get("Accept", "")

def loan_counts(user_id):
    """Return (approved_active, pending, all_open) loan counts for a user."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE user_id=? AND return_date IS NULL AND status='approved'", (user_id,))
        active = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE user_id=? AND return_date IS NULL AND status='pending'", (user_id,))
        pending = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE user_id=? AND return_date IS NULL", (user_id,))
        open_loans = cur.fetchone()["c"]
    return active, pending, open_loans

def fresh_status(book_id, user_id):
    """Fresh (label, kind) status for a book card after a loan action."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, available FROM books WHERE id=?", (book_id,))
        row = cur.fetchone()
        if not row:
            return None, None
    return book_status_for({"id": row["id"], "available": row["available"]}, user_id)

# ---------------- borrow logic (port of borrow_system.py) ----------------

def request_book_logic(user_id, book_id):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM borrow_log WHERE user_id=? AND return_date IS NULL AND status='approved'", (user_id,))
        if cur.fetchone()[0] >= MAX_ACTIVE_LOANS:
            return False, f"You can't borrow more than {MAX_ACTIVE_LOANS} books at a time."
        cur.execute("SELECT available, title FROM books WHERE id=?", (book_id,))
        book = cur.fetchone()
        if not book:
            return False, "Book not found."
        if (book["available"] or 0) == 0:
            return False, "This book is currently checked out."
        cur.execute("SELECT id FROM borrow_log WHERE user_id=? AND book_id=? AND return_date IS NULL AND status='pending'", (user_id, book_id))
        if cur.fetchone():
            return False, "You already have a pending request for this book."
        now = datetime.now()
        exp = now + timedelta(days=LOAN_DAYS)
        cur.execute("INSERT INTO borrow_log (user_id, book_id, borrow_date, expire_date, status) VALUES (?,?,?,?,?)",
                    (user_id, book_id, now.isoformat(), exp.isoformat(), "pending"))
        conn.commit()
        return True, "Request sent. The book will be loaned to you once a librarian approves it."

def return_book_logic(user_id, book_id):
    with get_db() as conn:
        cur = conn.cursor()
        # Target the newest APPROVED open loan. Older denied/pending rows for the
        # same user+book stay open (return_date IS NULL), so an unfiltered
        # fetchone() could grab a stale row and wrongly refuse the return.
        cur.execute("""SELECT id, expire_date, status FROM borrow_log
                       WHERE user_id=? AND book_id=? AND return_date IS NULL AND status='approved'
                       ORDER BY id DESC LIMIT 1""", (user_id, book_id))
        entry = cur.fetchone()
        if not entry:
            cur.execute("""SELECT id FROM borrow_log
                           WHERE user_id=? AND book_id=? AND return_date IS NULL AND status='pending'
                           LIMIT 1""", (user_id, book_id))
            if cur.fetchone():
                return False, "Your request hasn't been approved yet."
            cur.execute("""SELECT id FROM borrow_log
                           WHERE user_id=? AND book_id=? AND return_date IS NULL AND status='denied'
                           LIMIT 1""", (user_id, book_id))
            if cur.fetchone():
                return False, "Your request was denied by a librarian."
            return False, "You haven't borrowed this book, or it was already returned."
        if entry["expire_date"]:
            try:
                exp = datetime.fromisoformat(entry["expire_date"])
                if exp.tzinfo is None:
                    exp = pytz.utc.localize(exp)
                overdue = (datetime.now(pytz.utc) - exp).days
                if overdue > 0:
                    fee = overdue * OVERDUE_FEE_PER_DAY
                    return False, f"Return blocked: {overdue} day(s) overdue — a ${fee:,} fine applies. Please contact a librarian."
            except Exception:
                pass
        cur.execute("UPDATE books SET available=1 WHERE id=?", (book_id,))
        cur.execute("UPDATE borrow_log SET return_date=? WHERE id=?", (datetime.now().isoformat(), entry["id"]))
        conn.commit()
        return True, "Book returned. Thanks!"

def book_status_for(book_row, user_id):
    """Return (label, kind) where kind in available|mine|borrowed|pending"""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT expire_date, status FROM borrow_log WHERE book_id=? AND user_id=? AND return_date IS NULL ORDER BY id DESC LIMIT 1",
                    (book_row["id"], user_id))
        r = cur.fetchone()
        if r and r["status"] == "pending":
            return "Pending approval", "pending"
        if (book_row["available"] or 0) == 1:
            return "Available", "available"
        if r and r["status"] == "approved" and r["expire_date"]:
            dl = days_left(r["expire_date"])
            if dl is not None:
                return f"With you · {max(0, dl)} days left", "mine"
            return "With you", "mine"
    return "Checked out", "borrowed"

# ---------------- assistant (lightweight, no torch) ----------------

HELP_DATA = {
    "sign in": "To sign in, use your username and password on the Sign in page. Usernames are lowercased. If you forgot your password, contact a librarian.",
    "login": "To sign in, use your username and password on the Sign in page. Usernames are lowercased. If you forgot your password, contact a librarian.",
    "sign up": "Open the Sign up page, pick a unique username and a password of at least 3 characters. Your account is created instantly with a 10-book loan limit.",
    "register": "Open the Sign up page, pick a unique username and a password of at least 3 characters. Your account is created instantly with a 10-book loan limit.",
    "search": "Use the search bar on the Home or Dashboard page to find books by title or author across all branches. You can also use the instant filter to narrow the visible cards.",
    "borrow": "Click Request on any available book. Loans last 14 days and you can hold up to 10 books at once. A librarian must approve the request first.",
    "request": "Click Request on any available book. Loans last 14 days and you can hold up to 10 books at once. A librarian must approve the request first.",
    "return": "Go to My Books and press Return on the loan. Late returns may carry a fine — settle it with a librarian first.",
    "fine": "Overdue books accrue a $1 fine per day. Returns are blocked while a fine is outstanding — contact a librarian to settle it.",
    "overdue": "Overdue books accrue a $1 fine per day. Returns are blocked while a fine is outstanding — contact a librarian to settle it.",
    "suspended": "Suspended accounts can't sign in or borrow. Contact a librarian for help.",
    "banned": "Suspended accounts can't sign in or borrow. Contact a librarian for help.",
    "branches": "The Main Library plus any number of branches. Each book belongs to one branch shown on its card.",
    "libraries": "The Main Library plus any number of branches. Each book belongs to one branch shown on its card.",
    "wrong password": "Make sure your username and password are correct (usernames are lowercase). If it still fails, contact support.",
    "how do i borrow a book": "Click Request on any available book, then wait for librarian approval. Track it under My Books.",
    "how do i return a book": "Go to My Books and press Return. Late returns may carry a fine.",
    "how do i search": "Use the search bar to look up books by title or author.",
}

BLOCKED = ["source", "config", "sql", "code", "database", "db", "sqlite",
           "system table", "internal", "root password"]

def assistant_answer(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return "Please type your question first."
    low = t.lower()
    if any(b in low for b in BLOCKED):
        return "Sorry, I can't help with internal system details."
    if not HELP_DATA:
        return "Sorry, I couldn't find help for that."
    for k, v in HELP_DATA.items():
        if k in low or low in k:
            return v
    keys = list(HELP_DATA.keys())
    m = difflib.get_close_matches(low, keys, n=1, cutoff=0.45)
    if m:
        return HELP_DATA[m[0]]
    best, best_score = None, 0
    toks = set(low.split())
    for k, v in HELP_DATA.items():
        overlap = len(toks & set(k.split()))
        if overlap > best_score:
            best, best_score = v, overlap
    if best:
        return best
    return "Sorry, I couldn't find help for that. Try keywords like: sign in, search, borrow, return, fine."

# ---------------- routes ----------------

@app.route("/")
def index():
    q = (request.args.get("q") or "").strip().lower()
    with get_db() as conn:
        cur = conn.cursor()
        if q:
            cur.execute("""
                SELECT b.id, b.title, b.author, b.available, b.library_id,
                       COALESCE(l.name,'Main Library') AS lib_name
                FROM books b LEFT JOIN libraries l ON b.library_id=l.id
                WHERE LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ?
                ORDER BY b.id DESC LIMIT 60
            """, (f"%{q}%", f"%{q}%"))
        else:
            cur.execute("""
                SELECT b.id, b.title, b.author, b.available, b.library_id,
                       COALESCE(l.name,'Main Library') AS lib_name
                FROM books b LEFT JOIN libraries l ON b.library_id=l.id
                ORDER BY b.id DESC LIMIT 24
            """)
        books = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) c FROM books")
        total_books = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM libraries")
        total_libs = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM users")
        total_users = cur.fetchone()["c"]
    return render_template("index.html", books=books, q=(request.args.get("q") or "").strip(),
                           total_books=total_books, total_libs=total_libs, total_users=total_users)

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Please enter your username and password.", "error")
            return render_template("login.html")
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE username=?", (username,))
            row = cur.fetchone()
            if not row:
                flash("Incorrect username or password.", "error")
                return render_template("login.html")
            d = dict(row)
            if d["banned"]:
                flash("Your account has been suspended.", "error")
                return render_template("login.html")
            ok, upgrade = verify_password(d["password"], password)
            if not ok:
                flash("Incorrect username or password.", "error")
                return render_template("login.html")
            if upgrade:
                try:
                    cur.execute("UPDATE users SET password=? WHERE id=?",
                                (generate_password_hash(password), d["id"]))
                    conn.commit()
                except Exception:
                    pass
            session["user_id"] = d["id"]
            session["username"] = d["username"]
            flash(f"Welcome back, {d['username']}!", "ok")
            nxt = request.args.get("next") or url_for("dashboard")
            return redirect(nxt)
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Username and password are required.", "error")
            return render_template("register.html")
        if len(username) < 2 or len(password) < 3:
            flash("Username/password is too short (min 2 / 3 characters).", "error")
            return render_template("register.html")
        role = 3 if username in ("system", "root") else 0
        with get_db() as conn:
            cur = conn.cursor()
            try:
                cur.execute("INSERT INTO users (username, password, role) VALUES (?,?,?)",
                            (username, generate_password_hash(password), role))
                conn.commit()
                cur.execute("SELECT id FROM users WHERE username=?", (username,))
                uid = cur.fetchone()["id"]
                session["user_id"] = uid
                session["username"] = username
                flash("Account created. Welcome to eLib!", "ok")
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("That username is already taken.", "error")
    return render_template("register.html")

@app.route("/logout")
def logout():
    session.clear()
    flash("Signed out successfully.", "ok")
    return redirect(url_for("index"))

@app.route("/dashboard")
@login_required
def dashboard():
    me = current_user()
    q = (request.args.get("q") or "").strip()
    lib = request.args.get("lib") or ""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, code FROM libraries ORDER BY id")
        libs = [dict(r) for r in cur.fetchall()]
        sql = """
            SELECT b.id, b.title, b.author, b.available, b.library_id,
                   COALESCE(l.name,'Main Library') AS lib_name
            FROM books b LEFT JOIN libraries l ON b.library_id=l.id WHERE 1=1
        """
        params = []
        if q:
            sql += " AND (LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ?)"
            ql = f"%{q.strip().lower()}%"
            params += [ql, ql]
        if lib and lib != "all":
            try:
                sql += " AND b.library_id=?"
                params.append(int(lib))
            except ValueError:
                pass
        sql += " ORDER BY b.id DESC LIMIT 200"
        cur.execute(sql, params)
        books = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE user_id=? AND return_date IS NULL AND status='approved'", (me["id"],))
        active = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE user_id=? AND return_date IS NULL AND status='pending'", (me["id"],))
        pending = cur.fetchone()["c"]
    annotated = []
    for b in books:
        label, kind = book_status_for(b, me["id"])
        b["status_label"] = label
        b["status_kind"] = kind
        annotated.append(b)
    return render_template("dashboard.html", books=annotated, libs=libs, q=q, lib=lib or "all",
                           active=active, pending=pending, max_loans=MAX_ACTIVE_LOANS)

@app.route("/request/<int:book_id>", methods=["POST"])
@login_required
def request_book(book_id):
    me = current_user()
    as_json = wants_json()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT library_id FROM books WHERE id=?", (book_id,))
        r = cur.fetchone()
        if r and (r["library_id"] or 0) != 0:
            cur.execute("SELECT b.title, l.name FROM books b LEFT JOIN libraries l ON b.library_id=l.id WHERE b.id=?", (book_id,))
            info = cur.fetchone()
            title = info["title"] if info else ""
            lname = info["name"] if info and info["name"] else "Main Library"
            msg = f"'{title}' lives at the '{lname}' branch. Please request it there."
            if as_json:
                active, pending, open_loans = loan_counts(me["id"])
                label, kind = fresh_status(book_id, me["id"])
                return jsonify({"ok": False, "message": msg, "status_label": label,
                                "status_kind": kind, "active": active,
                                "pending": pending, "open": open_loans}), 200
            flash(msg, "warn")
            return redirect(url_for("dashboard", q=request.form.get("q", "")))
    ok, msg = request_book_logic(me["id"], book_id)
    if as_json:
        active, pending, open_loans = loan_counts(me["id"])
        label, kind = fresh_status(book_id, me["id"])
        return jsonify({"ok": ok, "message": msg, "status_label": label,
                        "status_kind": kind, "active": active,
                        "pending": pending, "open": open_loans}), 200
    flash(msg, "ok" if ok else "warn")
    return redirect(request.referrer or url_for("dashboard"))

@app.route("/return/<int:book_id>", methods=["POST"])
@login_required
def return_book(book_id):
    me = current_user()
    ok, msg = return_book_logic(me["id"], book_id)
    if wants_json():
        active, pending, open_loans = loan_counts(me["id"])
        payload = {"ok": ok, "message": msg, "book_id": book_id,
                   "status_label": "Available", "status_kind": "available",
                   "active": active, "pending": pending, "open": open_loans}
        if ok:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute("""SELECT b.title, b.author, bl.borrow_date, bl.return_date, bl.status
                               FROM borrow_log bl JOIN books b ON bl.book_id=b.id
                               WHERE bl.user_id=? AND bl.book_id=? ORDER BY bl.id DESC LIMIT 1""",
                            (me["id"], book_id))
                row = cur.fetchone()
                if row:
                    payload["history_row"] = {
                        "title": row["title"], "author": row["author"],
                        "borrowed": format_date(row["borrow_date"], True),
                        "returned": format_date(row["return_date"], True),
                        "status": row["status"],
                    }
        return jsonify(payload), 200
    flash(msg, "ok" if ok else "warn")
    return redirect(request.referrer or url_for("mybooks"))

@app.route("/mybooks")
@login_required
def mybooks():
    me = current_user()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT b.id, b.title, b.author, bl.borrow_date, bl.expire_date, bl.status, bl.return_date,
                   COALESCE(l.name,'Main Library') AS lib_name
            FROM borrow_log bl JOIN books b ON bl.book_id=b.id
            LEFT JOIN libraries l ON b.library_id=l.id
            WHERE bl.user_id=? AND bl.return_date IS NULL AND bl.status IN ('pending','approved')
            ORDER BY bl.id DESC
        """, (me["id"],))
        active = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT b.id, b.title, b.author, bl.borrow_date, bl.expire_date, bl.status, bl.return_date
            FROM borrow_log bl JOIN books b ON bl.book_id=b.id
            WHERE bl.user_id=? AND (bl.return_date IS NOT NULL OR bl.status='denied')
            ORDER BY bl.id DESC LIMIT 50
        """, (me["id"],))
        history = [dict(r) for r in cur.fetchall()]
    for r in active:
        if r["expire_date"] and r["status"] == "approved":
            dl = days_left(r["expire_date"])
            r["days_left"] = dl
            r["overdue"] = (dl is not None and dl < 0)
            r["fee"] = abs(dl) * OVERDUE_FEE_PER_DAY if r["overdue"] else 0
        else:
            r["days_left"] = None
            r["overdue"] = False
            r["fee"] = 0
    return render_template("mybooks.html", active=active, history=history,
                           loan_days=LOAN_DAYS, fee_per_day=OVERDUE_FEE_PER_DAY)

# ---------------- admin ----------------

@app.route("/admin", methods=["GET", "POST"])
@login_required
@admin_required
def admin():
    me = current_user()
    tab = request.args.get("tab", "books")
    view_user = request.args.get("view_user", type=int)

    if request.method == "POST":
        action = request.form.get("action")
        with get_db() as conn:
            cur = conn.cursor()
            try:
                if action == "add_book":
                    title = (request.form.get("title") or "").strip()
                    author = (request.form.get("author") or "").strip()
                    library_id = int(request.form.get("library_id") or 0)
                    if title and author:
                        cur.execute("INSERT INTO books (title, author, library_id) VALUES (?,?,?)", (title, author, library_id))
                        conn.commit()
                        flash("Book added.", "ok")
                    else:
                        flash("Title and author are required.", "error")
                    tab = "books"
                elif action == "delete_book":
                    cur.execute("DELETE FROM books WHERE id=?", (int(request.form.get("book_id")),))
                    conn.commit()
                    flash("Book deleted.", "ok")
                    tab = "books"
                elif action == "set_available":
                    bid = int(request.form.get("book_id"))
                    cur.execute("UPDATE books SET available=1 WHERE id=?", (bid,))
                    cur.execute("UPDATE borrow_log SET return_date=? WHERE book_id=? AND return_date IS NULL",
                                (datetime.now().isoformat(), bid))
                    conn.commit()
                    flash("Book marked as available.", "ok")
                    tab = "books"
                elif action == "add_library":
                    name = (request.form.get("name") or "").strip()
                    code = (request.form.get("code") or "").strip()
                    if name and code:
                        cur.execute("INSERT INTO libraries (name, code) VALUES (?,?)", (name, code))
                        conn.commit()
                        flash("Branch added.", "ok")
                    else:
                        flash("Name and code are required.", "error")
                    tab = "libraries"
                elif action == "delete_library":
                    lid = int(request.form.get("library_id"))
                    if lid == 0:
                        flash("The Main Library can't be deleted.", "error")
                    else:
                        cur.execute("SELECT COUNT(*) c FROM books WHERE library_id=?", (lid,))
                        if cur.fetchone()["c"] > 0:
                            flash("This branch still holds books.", "error")
                        else:
                            cur.execute("DELETE FROM libraries WHERE id=?", (lid,))
                            conn.commit()
                            flash("Branch deleted.", "ok")
                    tab = "libraries"
                elif action == "approve":
                    rid = int(request.form.get("request_id"))
                    cur.execute("SELECT book_id FROM borrow_log WHERE id=?", (rid,))
                    r = cur.fetchone()
                    if r:
                        cur.execute("UPDATE borrow_log SET status='approved' WHERE id=?", (rid,))
                        cur.execute("UPDATE books SET available=0 WHERE id=?", (r["book_id"],))
                        conn.commit()
                        flash("Request approved.", "ok")
                    tab = "requests"
                elif action == "deny":
                    # denying closes the request so it leaves the member's active shelf
                    cur.execute("UPDATE borrow_log SET status='denied', return_date=? WHERE id=? AND return_date IS NULL",
                                (datetime.now().isoformat(), int(request.form.get("request_id"))))
                    conn.commit()
                    flash("Request denied.", "ok")
                    tab = "requests"
                elif action in ("ban", "unban", "delete_user", "promote", "demote"):
                    uid = int(request.form.get("user_id"))
                    cur.execute("SELECT role FROM users WHERE id=?", (uid,))
                    t = cur.fetchone()
                    if not t:
                        flash("User not found.", "error")
                    elif not can_modify(me, t["role"], uid):
                        flash("You can't modify this user.", "error")
                    else:
                        if action == "ban":
                            cur.execute("UPDATE users SET banned=1 WHERE id=?", (uid,))
                        elif action == "unban":
                            cur.execute("UPDATE users SET banned=0 WHERE id=?", (uid,))
                        elif action == "delete_user":
                            cur.execute("DELETE FROM users WHERE id=?", (uid,))
                        elif action == "promote":
                            cur.execute("UPDATE users SET role=1 WHERE id=?", (uid,))
                        elif action == "demote":
                            cur.execute("UPDATE users SET role=0 WHERE id=?", (uid,))
                        conn.commit()
                        flash("User updated.", "ok")
                    tab = "users"
            except sqlite3.IntegrityError:
                flash("Error: duplicate code or name.", "error")
            except Exception as e:
                flash(f"Error: {e}", "error")
        return redirect(url_for("admin", tab=tab))

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, name, code FROM libraries ORDER BY id")
        libs = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT b.id, b.title, b.author, b.available, b.library_id,
                       COALESCE(l.name,'Main Library') AS lib_name
                       FROM books b LEFT JOIN libraries l ON b.library_id=l.id ORDER BY b.id DESC LIMIT 200""")
        books = [dict(r) for r in cur.fetchall()]
        cur.execute("""
            SELECT bl.id, u.username, b.title, b.author, bl.borrow_date, bl.expire_date
            FROM borrow_log bl JOIN users u ON bl.user_id=u.id JOIN books b ON bl.book_id=b.id
            WHERE bl.status='pending' ORDER BY bl.id DESC
        """)
        reqs = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id, username, role, banned FROM users ORDER BY id")
        users = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT COUNT(*) c FROM books")
        n_books = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE status='pending'")
        n_pending = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM borrow_log WHERE return_date IS NULL AND status='approved'")
        n_borrowed = cur.fetchone()["c"]
        viewed = None
        if view_user:
            cur.execute("SELECT id, username, role, banned FROM users WHERE id=?", (view_user,))
            u = cur.fetchone()
            if u:
                viewed = dict(u)
                cur.execute("""
                    SELECT b.title, b.author, bl.borrow_date, bl.expire_date
                    FROM borrow_log bl JOIN books b ON bl.book_id=b.id
                    WHERE bl.user_id=? AND bl.status='approved' AND bl.return_date IS NULL
                    ORDER BY bl.id DESC
                """, (view_user,))
                viewed["borrowed"] = [dict(r) for r in cur.fetchall()]

    return render_template("admin.html", tab=tab, libs=libs, books=books, reqs=reqs,
                           users=users, me=me, viewed=viewed,
                           n_books=n_books, n_pending=n_pending, n_borrowed=n_borrowed,
                           can_modify=lambda tr, tid: can_modify(me, tr, tid))

@app.route("/assistant", methods=["GET"])
def assistant():
    topics = list(HELP_DATA.keys())
    return render_template("assistant.html", topics=topics, help_data=HELP_DATA)

@app.route("/api/assistant", methods=["POST"])
def api_assistant():
    data = request.get_json(silent=True) or {}
    q = data.get("q") or request.form.get("q") or ""
    return jsonify({"answer": assistant_answer(q)})

@app.route("/api/books")
def api_books():
    q = (request.args.get("q") or "").strip().lower()
    with get_db() as conn:
        cur = conn.cursor()
        if q:
            cur.execute("SELECT id, title, author, available FROM books WHERE LOWER(title) LIKE ? OR LOWER(author) LIKE ? LIMIT 20",
                        (f"%{q}%", f"%{q}%"))
        else:
            cur.execute("SELECT id, title, author, available FROM books LIMIT 20")
        return jsonify([dict(r) for r in cur.fetchall()])

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
