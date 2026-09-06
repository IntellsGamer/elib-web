#!/usr/bin/env python3
"""Teach and verify the eLib assistant — no torch, no checkpoints, no models.

The assistant is a deterministic scoring engine (see assistant_engine.py).
"Training" it means teaching it new phrasings: example sentences mapped to
intents, stored as plain text in assistant_examples.jsonl (always safe to
commit — unlike model checkpoints or output weights, which must NEVER be
committed; see TRAINING.md and .gitignore).

Usage:
    python train_assistant.py check
        Run the accuracy suite. Exit 0 only if every case passes.
    python train_assistant.py add <intent> <example phrasing...>
        Teach one new phrasing, then re-run the suite.
    python train_assistant.py teach
        Interactive loop: type a phrasing, see the prediction,
        confirm or correct it to teach.
"""

import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from assistant_engine import Assistant, INTENTS  # noqa: E402

INTENT_NAMES = [i["name"] for i in INTENTS] + [
    "availability", "my_loans", "recommend", "branches", "stats", "me",
    "borrow_followup", "details_followup", "help", "fallback",
]

# (user text, expected intent name, needs_login)
EVAL_CASES = [
    # conversational
    ("hi", "greeting", False),
    ("hello!", "greeting", False),
    ("good morning", "greeting", False),
    ("hey there", "greeting", False),
    ("salam", "greeting", False),
    ("how are you", "how_are_you", False),
    ("how's it going?", "how_are_you", False),
    ("who are you", "identity", False),
    ("are you human", "identity", False),
    ("are you an AI", "identity", False),
    ("what can you do", "capabilities", False),
    ("help", "capabilities", False),
    ("help me please", "capabilities", False),
    ("commands", "capabilities", False),
    # borrowing & returns
    ("how do i borrow a book", "borrow", False),
    ("how to check out", "borrow", False),
    ("loan period", "borrow", False),
    ("how many books can i borrow", "borrow", False),
    ("can i borrow books", "borrow", False),
    ("borrowing limit", "borrow", False),
    ("how do i return a book", "return", False),
    ("give it back", "return", False),
    ("should i return this book", "return", False),
    ("do i have to return books", "return", False),
    ("renew my loan", "renew", False),
    ("can i extend", "renew", False),
    ("keep it longer", "renew", False),
    # fines
    ("how much is the fine", "fine", False),
    ("late fees", "fine", False),
    ("i have an overdue book", "fine", False),
    ("do you have fines", "fine", False),
    ("do i have to pay fines", "fine", False),
    # accounts
    ("how do i sign up", "account", False),
    ("change my password", "account", False),
    ("display name", "account", False),
    ("create an account", "account", False),
    ("how do i join", "account", False),
    ("sign me up", "account", False),
    ("delete my account", "cancel", False),
    ("how do i log in", "signin", False),
    ("wrong password", "signin", False),
    ("can't log in", "signin", False),
    ("log out", "signout", False),
    ("sign me out", "signout", False),
    ("am i banned", "suspended", False),
    ("have i been suspended", "suspended", False),
    # librarian / cancel / hours
    ("how do approvals work", "librarian", False),
    ("i want to add a book", "librarian", False),
    ("upload a cover", "librarian", False),
    ("cancel my request", "cancel", False),
    ("undo my request", "cancel", False),
    ("when are you open", "hours", False),
    ("is the library open today", "hours", False),
    ("talk to a human", "hours", False),
    ("phone number", "hours", False),
    ("where is the main library", "hours", False),
    # search
    ("how do i search", "search", False),
    ("find books by author", "search", False),
    # live catalog
    ("do you have dune", "availability", False),
    ("is the hobbit available", "availability", False),
    ("looking for 1984", "availability", False),
    ("find me frank herbert", "availability", False),
    ("do you have branches", "help", False),
    # live personal
    ("my loans", "my_loans", True),
    ("what books do i have", "my_loans", True),
    ("when is dune due", "my_loans", True),
    ("my shelf", "my_loans", True),
    ("do i have any books out", "my_loans", True),
    ("who am i", "me", False),
    ("what is my username", "me", True),
    # live discovery
    ("recommend a book", "recommend", False),
    ("what should i read", "recommend", False),
    ("surprise me", "recommend", False),
    ("how many books do you have", "stats", False),
    ("collection size", "stats", False),
    ("list the branches", "branches", False),
    ("what branches are there", "branches", False),
    # manners
    ("thanks!", "thanks", False),
    ("thank you so much", "thanks", False),
    ("bye", "bye", False),
    ("good night", "bye", False),
    ("see you later", "bye", False),
]


def make_test_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT, code TEXT);
        INSERT INTO libraries VALUES (0, 'Main Library', '0');
        CREATE TABLE books (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
            author TEXT, available INTEGER DEFAULT 1, library_id INTEGER DEFAULT 0,
            cover TEXT DEFAULT '');
        INSERT INTO books (title, author, available) VALUES
            ('Dune', 'Frank Herbert', 1),
            ('The Hobbit', 'J.R.R. Tolkien', 0),
            ('1984', 'George Orwell', 1);
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT,
            password TEXT, role INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
            display_name TEXT DEFAULT '');
        INSERT INTO users (username, password, display_name) VALUES ('tester', 'x', 'Test User');
        CREATE TABLE borrow_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            book_id INTEGER, borrow_date TEXT, return_date TEXT, status TEXT, expire_date TEXT);
        INSERT INTO borrow_log (user_id, book_id, borrow_date, expire_date, status)
            VALUES (1, 2, '2026-01-01', '2026-01-15', 'approved');
    """)
    conn.commit()
    return path, conn


def run_eval(verbose=True):
    path, conn = make_test_db()
    user = {"id": 1, "username": "tester", "display_name": "Test User", "role": 0}
    assistant = Assistant()
    passed, failed = 0, []
    db_factory_row = lambda: _row_conn(path)  # noqa: E731
    for text, expected, needs_login in EVAL_CASES:
        try:
            res = assistant.respond(
                text,
                user=(user if needs_login or expected in ("me",) else None),
                db=db_factory_row, ctx={})
            got = res.get("intent")
            # availability with zero hits falls through by design; our seeds hit.
            if got == expected:
                passed += 1
            else:
                failed.append((text, expected, got, res.get("answer", "")[:90]))
        except Exception as e:  # noqa: BLE001
            failed.append((text, expected, "EXC: %s" % e, ""))
    conn.close()
    try:
        os.remove(path)
    except OSError:
        pass
    if verbose:
        print("%d/%d cases passed." % (passed, len(EVAL_CASES)))
        for text, exp, got, ans in failed:
            print("  FAIL %-32r expected=%-12s got=%-12s | %s" % (text, exp, got, ans))
    return passed, failed


def _row_conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def cmd_add(intent, text):
    if intent not in INTENT_NAMES:
        print("Unknown intent %r. Known: %s" % (intent, ", ".join(sorted(INTENT_NAMES))))
        return 2
    text = " ".join(text).strip().lower()
    if not text:
        print("Empty phrasing — nothing taught.")
        return 2
    dest = os.path.join(HERE, "assistant_examples.jsonl")
    with open(dest, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"intent": intent, "text": text}) + "\n")
    print("Taught: [%s] %r" % (intent, text))
    passed, failed = run_eval(verbose=False)
    print("Suite now: %d/%d passed." % (passed, len(EVAL_CASES)))
    for text_, exp, got, _ in failed:
        print("  FAIL %-32r expected=%-12s got=%s" % (text_, exp, got))
    return 0 if not failed else 1


def cmd_teach():
    assistant = Assistant()
    print("Teach mode — type a phrasing (empty line quits).")
    while True:
        try:
            text = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            break
        res = assistant.respond(text)
        print("AI predicts: [%s] %s" % (res.get("intent"), res["answer"][:120]))
        try:
            fix = input("Correct intent (Enter = prediction is right): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if fix:
            sys.exit(cmd_add(fix, [text]))
    return 0


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    if argv[1] == "check":
        passed, failed = run_eval(verbose=True)
        return 0 if not failed else 1
    if argv[1] == "add":
        if len(argv) < 4:
            print("Usage: train_assistant.py add <intent> <example phrasing...>")
            return 2
        return cmd_add(argv[2], argv[3:])
    if argv[1] == "teach":
        return cmd_teach()
    print("Unknown command %r.\n%s" % (argv[1], __doc__))
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
