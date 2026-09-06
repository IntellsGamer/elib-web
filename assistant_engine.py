# assistant_engine.py — eLib conversational assistant.
"""Rule-based conversational engine with live library answers.

Deterministic by design: intent matching is exact-phrase + weighted-keyword
+ fuzzy scoring over the committed help keys below (stdlib only — no torch,
no embeddings, no downloaded models, so there is nothing to train, nothing
to checkpoint, and answers are reproducible).

Each deployer can still *teach* it: add example phrasings with
``train_assistant.py`` (they land in the committed ``assistant_examples.jsonl``
— plain text, always safe to commit) and run its accuracy suite until green.

Public API:
    Assistant().respond(text, user=None, db=None, ctx=None, actions=None)
        -> {"answer": str, "suggestions": [str, ...], "context": {...}}

    assistant_answer(text)  # backwards-compatible plain-string wrapper
"""

import difflib
import json
import os
import re
import sqlite3
from datetime import datetime

# ---------------------------------------------------------------------------
# Guardrails (never answer these — same policy as before)

BLOCKED = ["source", "config", "sql", "code", "database", "db", "sqlite",
           "system table", "internal", "root password"]

# ---------------------------------------------------------------------------
# Committed knowledge base ("help keys" — safe to commit, no model weights)

HELP_DATA = {
    "sign in": "To sign in, use your username and password on the Sign in page. Usernames are lowercased. If you forgot your password, contact a librarian.",
    "login": "To sign in, use your username and password on the Sign in page. Usernames are lowercased. If you forgot your password, contact a librarian.",
    "sign up": "Open the Sign up page, pick a unique username, a display name and a password of at least 3 characters. Your account is created instantly with a 10-book loan limit.",
    "register": "Open the Sign up page, pick a unique username, a display name and a password of at least 3 characters. Your account is created instantly with a 10-book loan limit.",
    "search": "Use the search bar on the Home or Dashboard page to find books by title or author across all branches. You can also use the instant filter to narrow the visible cards.",
    "borrow": "Click Request on any available book. Loans last 14 days and you can hold up to 10 books at once. A librarian must approve the request first.",
    "request": "Click Request on any available book. Loans last 14 days and you can hold up to 10 books at once. A librarian must approve the request first.",
    "return": "Go to My Books and press Return on the loan. Late returns may carry a fine — settle it with a librarian first.",
    "fine": "Overdue books accrue a {fine} fine per day. Returns are blocked while a fine is outstanding — contact a librarian to settle it.",
    "overdue": "Overdue books accrue a {fine} fine per day. Returns are blocked while a fine is outstanding — contact a librarian to settle it.",
    "suspended": "Suspended accounts can't sign in or borrow. Contact a librarian for help.",
    "banned": "Suspended accounts can't sign in or borrow. Contact a librarian for help.",
    "branches": "The Main Library plus any number of branches. Each book belongs to one branch shown on its card.",
    "libraries": "The Main Library plus any number of branches. Each book belongs to one branch shown on its card.",
    "wrong password": "Make sure your username and password are correct (usernames are lowercase). If it still fails, contact support.",
    "how do i borrow a book": "Click Request on any available book, then wait for librarian approval. Track it under My Books.",
    "how do i return a book": "Go to My Books and press Return. Late returns may carry a fine.",
    "how do i search": "Use the search bar to look up books by title or author.",
}

# ---------------------------------------------------------------------------
# Intents: title + description (shown in the knowledge grid), example
# phrasings, weighted keywords, answer, follow-up suggestions.

INTENTS = [
    {
        "name": "greeting",
        "title": "Greetings",
        "desc": "Say hello — I greet you by name when you're signed in.",
        "examples": ["hi", "hello", "hey", "hey there", "hi there", "hello there",
                     "good morning", "good afternoon", "good evening", "salam",
                     "yo", "howdy", "greetings"],
        "keywords": {"hi": 4, "hello": 4, "hey": 4, "morning": 3, "afternoon": 3,
                     "evening": 3, "salam": 4, "howdy": 4, "greetings": 4, "yo": 2},
        "answer": "Hey {first}! Great to see you. Ask me if we have a book, about your loans, or anything about borrowing, returns and fines.",
        "suggestions": ["what can you do", "do you have dune", "my loans"],
    },
    {
        "name": "how_are_you",
        "title": "Small talk",
        "desc": "How I'm doing — the honest server-status version.",
        "examples": ["how are you", "how is it going", "how are you doing",
                     "are you ok", "are you well", "how do you feel"],
        "keywords": {"how": 2, "are": 1, "you": 1, "going": 3, "feel": 3, "feeling": 3},
        "answer": "Running smoothly, shelves dusted, due dates tracked. More importantly — how can I help you read something great today?",
        "suggestions": ["recommend a book", "what can you do", "my loans"],
    },
    {
        "name": "identity",
        "title": "Who I am",
        "desc": "What the eLib assistant is and isn't.",
        "examples": ["who are you", "your name", "what are you", "about yourself",
                     "are you human", "are you real", "are you an ai", "are you a robot"],
        "keywords": {"who": 3, "name": 3, "human": 4, "real": 3, "robot": 4, "ai": 3,
                     "yourself": 3, "your": 1},
        "answer": "I'm the eLib assistant — a built-in helper that knows this library inside out: the live catalog, your loans, borrowing rules and fines. No account of mine, no sleep, just books.",
        "suggestions": ["what can you do", "recommend a book", "how do i search"],
    },
    {
        "name": "capabilities",
        "title": "What I can do",
        "desc": "The full tour of my abilities.",
        "examples": ["what can you do", "help", "help me", "what do you do",
                     "options", "features", "commands", "how does this work",
                     "what else can you do", "show me what you can do", "abilities"],
        "keywords": {"help": 5, "can": 2, "do": 1, "abilities": 5, "features": 4,
                     "options": 4, "commands": 4, "capable": 4},
        "answer": ("Here's my repertoire: check if we have a book (try 'do you have dune'), "
                   "summarize your loans and due dates ('my loans'), recommend something to read, "
                   "explain borrowing, returns, fines and accounts, list our branches — and if you "
                   "pick a book I can even request it for you. What sounds good?"),
        "suggestions": ["do you have dune", "my loans", "recommend a book"],
    },
    {
        "name": "borrow",
        "title": "Borrowing",
        "desc": "How loans work: limits, terms, approval.",
        "examples": ["how do i borrow a book", "how to borrow", "borrow a book",
                     "check out a book", "checkout", "loan a book", "take out a book",
                     "how long can i keep", "loan period", "how many books",
                     "borrowing limit", "can i borrow", "request a book", "get a book"],
        "keywords": {"borrow": 6, "borrowing": 6, "checkout": 5, "check": 1, "loan": 5,
                     "keep": 3, "long": 2, "many": 2, "limit": 4, "take": 2},
        "answer": ("Click Request on any available book — loans last 14 days and you can hold up to "
                   "10 books at once. A librarian approves each request first, then it lands on your "
                   "shelf under My Books. Want me to find something? Just ask, e.g. 'do you have dune'."),
        "suggestions": ["do you have dune", "my loans", "fine"],
    },
    {
        "name": "return",
        "title": "Returns",
        "desc": "How to give a book back.",
        "examples": ["how do i return a book", "return a book", "give back",
                     "returning", "how to return", "bring back"],
        "keywords": {"return": 6, "give": 2, "back": 3, "bring": 3},
        "answer": "Go to My Books and press Return on the loan. If it's overdue a fine applies — returns stay blocked until a librarian settles it with you.",
        "suggestions": ["my loans", "fine", "renew"],
    },
    {
        "name": "renew",
        "title": "Renewals",
        "desc": "Keeping a book longer.",
        "examples": ["renew", "renewal", "extend my loan", "extend", "keep it longer",
                     "more time", "prolong"],
        "keywords": {"renew": 7, "renewal": 7, "extend": 6, "prolong": 5, "longer": 3},
        "answer": "There's no renew button — return the book from My Books and request it again once it's back on the shelf. If nobody else grabbed it, approval is usually quick.",
        "suggestions": ["my loans", "how do i return a book", "recommend a book"],
    },
    {
        "name": "fine",
        "title": "Fines & overdue",
        "desc": "Late fees and blocked returns.",
        "examples": ["fine", "fines", "overdue", "late fee", "late fees", "late return",
                     "how much is the fine", "penalty", "blocked return", "can't return",
                     "past due"],
        "keywords": {"fine": 6, "fines": 6, "overdue": 6, "late": 5, "fee": 5, "fees": 5,
                     "penalty": 5, "blocked": 3, "due": 3, "past": 2},
        "answer": "Overdue books accrue a {fine} fine per day, and returns are blocked while a fine is outstanding — contact a librarian to settle it, then return as normal. Check My Books to see exactly what's overdue.",
        "suggestions": ["my loans", "how do i return a book", "branches"],
    },
    {
        "name": "search",
        "title": "Searching",
        "desc": "Finding books by title, author or branch.",
        "examples": ["how do i search", "how to search", "search", "find books",
                     "look up", "filter", "search by author", "browse"],
        "keywords": {"search": 6, "find": 4, "lookup": 4, "look": 2, "filter": 5,
                     "browse": 4, "author": 2},
        "answer": ("Use the search bar on Home or Dashboard to find books by title or author, filter by "
                   "branch, or use the instant filter on the page. Shortcut: just ask me — 'do you have "
                   "dune' searches the live catalog right here."),
        "suggestions": ["do you have dune", "branches", "recommend a book"],
    },
    {
        "name": "account",
        "title": "Accounts",
        "desc": "Sign up, display names, passwords, profiles.",
        "examples": ["sign up", "register", "create account", "new account", "join",
                     "join the library", "sign me up", "become a member", "membership",
                     "display name", "change my name", "change password", "new password",
                     "my account", "my profile", "update profile", "forgot password",
                     "change my username"],
        "keywords": {"signup": 5, "sign": 2, "up": 1, "register": 6, "join": 5,
                     "member": 3, "membership": 5, "display": 5, "password": 5,
                     "profile": 5, "account": 5, "username": 5, "forgot": 4},
        "answer": ("Sign up free with a unique username (lowercase, min 2 characters — usernames are "
                   "permanent), a display name and a password of at least 3 characters. Later you can "
                   "change your display name or password anytime on the My Account tab — I greet you by "
                   "your first name everywhere."),
        "suggestions": ["sign in", "my loans", "what can you do"],
    },
    {
        "name": "signout",
        "title": "Sign out",
        "desc": "Logging out of your session.",
        "examples": ["log out", "sign out", "sign me out", "log me out",
                     "how do i log out", "how to sign out"],
        "keywords": {"logout": 6, "signout": 6, "out": 2},
        "answer": "Tap Sign out in the top bar (or the menu on mobile) — you'll land back on Home, signed out everywhere on this device.",
        "suggestions": ["sign in", "my loans", "bye"],
    },
    {
        "name": "signin",
        "title": "Sign in",
        "desc": "Logging in and password trouble.",
        "examples": ["sign in", "login", "log in", "log me in", "wrong password",
                     "can't log in", "cannot login", "forgot password", "password not working"],
        "keywords": {"signin": 5, "login": 6, "sign": 2, "in": 1, "log": 3,
                     "password": 4, "wrong": 3},
        "answer": "Sign in with your username (lowercased) and password. If it fails, double-check caps lock — and if it's still stuck, a librarian can sort you out.",
        "suggestions": ["sign up", "suspended", "what can you do"],
    },
    {
        "name": "suspended",
        "title": "Suspended accounts",
        "desc": "What suspension means.",
        "examples": ["suspended", "banned", "blocked account", "account blocked",
                     "can't sign in", "suspension"],
        "keywords": {"suspended": 7, "banned": 7, "suspension": 6, "blocked": 4},
        "answer": "Suspended accounts can't sign in or borrow. Contact a librarian — only they can reinstate you.",
        "suggestions": ["sign in", "branches", "what can you do"],
    },
    {
        "name": "librarian",
        "title": "Librarians & admin",
        "desc": "Approvals, adding books, covers, members.",
        "examples": ["librarian", "admin", "approve", "approval", "add a book",
                     "upload cover", "add cover", "manage books", "pending requests",
                     "become librarian", "promote"],
        "keywords": {"librarian": 6, "admin": 5, "approve": 5, "approval": 5,
                     "manage": 4, "promote": 4, "permission": 3},
        "answer": ("Librarians approve loan requests, add and edit books (covers included — uploads are "
                   "auto-resized), manage branches and members from the Admin console. Only librarians see "
                   "that tab; promotions are handled by higher roles, never by request here."),
        "suggestions": ["branches", "how do i borrow a book", "what can you do"],
    },
    {
        "name": "cancel",
        "title": "Cancelling requests",
        "desc": "Undoing a pending request.",
        "examples": ["cancel my request", "cancel request", "undo request",
                     "withdraw request", "remove my request", "delete my account",
                     "remove my account"],
        "keywords": {"cancel": 7, "undo": 5, "withdraw": 6},
        "answer": "There's no self-cancel button — a librarian can deny the pending request for you, which frees the slot instantly. Ask at your branch with the book title ready. (Same goes for deleting your account: only a librarian can do that.)",
        "suggestions": ["my loans", "branches", "how do i borrow a book"],
    },
    {
        "name": "hours",
        "title": "Hours & contact",
        "desc": "Opening times and reaching a human.",
        "examples": ["opening hours", "open hours", "when are you open", "hours",
                     "is the library open", "are you open today", "opening times",
                     "where is the library", "where is the main library",
                     "library location", "location", "where are you", "address", "phone", "contact",
                     "talk to a human", "talk to someone", "real person"],
        "keywords": {"hours": 6, "opening": 5, "open": 3, "location": 5, "located": 5,
                     "address": 5, "phone": 5, "contact": 5, "human": 5, "person": 3,
                     "where": 2},
        "answer": "I'm online around the clock, but branch hours and desks vary — check with your branch directly. For anything I can't do (fines, suspensions, cancellations), a librarian is the human to talk to.",
        "suggestions": ["branches", "what can you do", "fine"],
    },
    {
        "name": "thanks",
        "title": "Thanks",
        "desc": "You're welcome, anytime.",
        "examples": ["thanks", "thank you", "thx", "merci", "appreciated",
                     "great help", "awesome", "perfect"],
        "keywords": {"thanks": 6, "thank": 6, "thx": 6, "merci": 5, "appreciated": 5,
                     "awesome": 3, "perfect": 2},
        "answer": "Anytime, {first} — happy reading! Anything else I can look up for you?",
        "suggestions": ["recommend a book", "my loans", "what can you do"],
    },
    {
        "name": "bye",
        "title": "Goodbye",
        "desc": "See you at the shelves.",
        "examples": ["bye", "goodbye", "see you", "good night", "goodnight",
                     "later", "gotta go", "farewell"],
        "keywords": {"bye": 6, "goodbye": 6, "farewell": 5, "later": 3, "goodnight": 5,
                     "night": 3},
        "answer": "See you at the shelves, {first}! Your loans and due dates will be waiting on My Books.",
        "suggestions": ["my loans", "recommend a book"],
    },
]

# ---------------------------------------------------------------------------
# Normalization & scoring

STOPWORDS = {"a", "an", "the", "is", "are", "was", "were", "do", "does", "did",
             "i", "me", "my", "you", "your", "we", "it", "this", "that", "to",
             "for", "of", "in", "on", "at", "and", "or", "please", "can", "could",
             "would", "should", "how", "what", "when", "where", "which", "there",
             "here", "with", "have", "has", "had", "be", "been", "am", "anymore"}

_NON_ALNUM = re.compile(r"[^a-z0-9\s]")
_WS = re.compile(r"\s+")


def _norm(text):
    return _WS.sub(" ", _NON_ALNUM.sub(" ", (text or "").lower())).strip()


def _tokens(normed):
    return [t for t in normed.split() if t and t not in STOPWORDS]


def _content_tokens(text):
    """Non-stopword tokens — the words that carry meaning.

    Single characters are dropped: they are apostrophe debris ("how's" ->
    "how s") and carry no signal.
    """
    return [t for t in _WS.sub(" ", _NON_ALNUM.sub(" ", text.lower())).split()
            if t and t not in STOPWORDS and len(t) > 1]


def _fuzzy_ok(input_toks, example_toks):
    """Guard whole-string fuzzy matches against minimal-pair confusion.

    Every content word on the shorter side must closely resemble (>=0.6) some
    content word on the other side. This keeps genuine typos ("borow a book")
    matching while rejecting same-shape different-meaning inputs
    ("how to check out" vs "how to sign out"). Combined with the fuzzy cap
    below, uncertain fuzzy evidence can never outrank certain evidence
    (exact phrases, containment, keyword hits).
    """
    short, long = ((input_toks, example_toks)
                   if len(input_toks) <= len(example_toks)
                   else (example_toks, input_toks))
    if not short:
        return True
    for tok in short:
        if not any(difflib.SequenceMatcher(None, tok, other).ratio() >= 0.6
                   for other in long):
            return False
    return True


# Fuzzy whole-string evidence is inherently uncertain: cap it below the
# containment range so a near-miss can never beat an exact phrase hit
# ("how do i log in" must resolve to sign-in, never sign-out).
_FUZZY_CAP = 67.0


def _score_intent(normed, toks, intent):
    """Best 0–100 match of the input against one intent."""
    best = 0
    in_content = _content_tokens(normed)
    for ex in intent["examples"]:
        if normed == ex:
            return 100.0
        if len(ex) >= 4 and ex in normed:
            best = max(best, min(92.0, 62.0 + len(ex)))
        elif len(normed) >= 4 and normed in ex:
            # Short input inside a longer example: only with word boundaries,
            # so "out" doesn't match "about yourself" but "log" matches "log in".
            if re.search(r"\b%s\b" % re.escape(normed), ex):
                best = max(best, 70.0)
        else:
            r = difflib.SequenceMatcher(None, normed, ex).ratio()
            if r >= 0.62 and _fuzzy_ok(in_content, _content_tokens(ex)):
                best = max(best, min(r * 100.0, _FUZZY_CAP))
    kw_hit = sum(w for k, w in intent.get("keywords", {}).items() if k in toks)
    if kw_hit >= 5:
        best = max(best, min(95.0, 52.0 + kw_hit * 4.0))
    return best


def classify(normed, toks):
    """Return (intent, score) for the best intent, or (None, 0)."""
    best, best_score = None, 0.0
    for intent in INTENTS:
        s = _score_intent(normed, toks, intent)
        if s > best_score:
            best, best_score = intent, s
    return best, best_score


# ---------------------------------------------------------------------------
# Live-data triggers (pattern first, content second)

_AVAIL_TRIGGERS = [
    "do you have", "do u have", "have you got", "have u got",
    "do you stock", "do you carry",
    "is there a book", "is there any book", "looking for", "look for",
    "find me", "find for me", "search for", "get me", "books by",
    "anything by", "authored by", "written by",
]
_AVAIL_STRIP = [
    "do you have", "do u have", "have you got", "have u got",
    "do you stock", "do you carry",
    "is there a book called", "is there a book", "is there any book",
    "i am looking for", "i'm looking for", "looking for", "look for",
    "please find me", "find me", "find for me", "search for",
    "please get me", "get me", "authored by", "written by", "books by",
    "anything by", "called", "titled",
    "is available", "available", "in stock", "in the library",
    "at the library", "please", "book", "by", "any",
]

_MYLOANS_TRIGGERS = ["my books", "my loans", "my borrow", "checked out",
                     "what do i have", "books i have", "books do i have",
                     "book do i have", "books i borrowed", "have i borrowed",
                     "what do i have", "do i have", "have i", "currently have",
                     "am borrowing", "my current", "still have", "have out",
                     "books out", "on my shelf", "my requests", "my shelf",
                     "when is", "when are", "due date", "due dates",
                     "what is due", "whats due", "overdue for me", "my fines",
                     "my fees"]
# Phrasings that merely *contain* a my-loans trigger but ask something else.
_MYLOANS_GUARD_WORDS = {"banned", "suspended", "suspension", "ban",
                        "open", "hours", "hour", "location", "address"}
_MYLOANS_GUARD_RE = re.compile(r"\b(have to|need to|must|should i|do i need)\b")

_RECOMMEND_TRIGGERS = ["recommend", "suggestion", "suggest", "what should i read",
                       "something to read", "good book", "popular", "bestseller",
                       "anything interesting", "surprise me"]

_STATS_TRIGGERS = ["how many books", "how many members", "how many branches",
                   "how big", "collection size", "library stats", "statistics"]
# Words proving a "how many books …" question is about borrowing, not stats.
_STATS_BORROW_WORDS = {"borrow", "borrowing", "loan", "loans", "checkout",
                       "request", "requests", "keep", "limit", "renew"}

_ME_TRIGGERS = ["my username", "my display name", "who am i", "my user",
                "my account name"]

_BRANCHES_TRIGGERS = ["branches", "branch", "libraries", "locations"]

_ORDINAL = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
            "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}
_BORROW_FOLLOW = re.compile(
    r"\b(borrow|request|take|grab|get|checkout|check out)\b.{0,20}"
    r"\b(it|them|that|those|this|these|one|first|second|third|fourth|fifth)\b"
    r"|\b(borrow|request)\s+(the\s+)?(first|second|third|fourth|fifth)\s+(one)?\b")
_DETAILS_FOLLOW = re.compile(
    r"\btell me more\b|\bmore about\b|\bdetails?\b|\bdescribe\b|"
    r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(one)?\b|"
    r"^\s*[1-5]\s*$|\bwhat.?s it about\b|\bsynopsis\b")
_YES = re.compile(r"^\s*(yes|yeah|yep|sure|ok|okay|please|do it|go ahead)\s*[!.]*\s*$")


def _strip_avail_query(normed):
    q = " %s " % normed
    for pat in sorted(_AVAIL_STRIP, key=len, reverse=True):
        q = q.replace(" %s " % pat, " ")
    q = _WS.sub(" ", q).strip()
    return " ".join(t for t in q.split() if t not in STOPWORDS)


def _fmt_fine(rate):
    """Format the per-day fine: 1 -> $1, 0.5 -> $0.50."""
    try:
        v = round(float(rate), 2)
    except (TypeError, ValueError):
        v = 1.0
    if v == int(v):
        return "$%d" % int(v)
    return "$%.2f" % v


def _fmt_day(iso_str):
    try:
        return datetime.fromisoformat(iso_str).strftime("%b %d, %Y")
    except Exception:
        return iso_str or "—"


def _first_name(user):
    if not user:
        return "there"
    dn = ""
    try:
        dn = (user.get("display_name") or "").strip()
    except AttributeError:
        dn = ""
    name = dn or user.get("username", "there")
    return (name.split() or ["there"])[0]


# ---------------------------------------------------------------------------
# The assistant

class Assistant:
    def __init__(self):
        self._fine_str = "$1"
        self._load_community_examples()

    # -- teachable examples (plain text, always safe to commit) --------------
    def _load_community_examples(self):
        """Merge deployer-taught phrasings from assistant_examples.jsonl."""
        here = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(here, "assistant_examples.jsonl")
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    name = obj.get("intent")
                    text = (obj.get("text") or "").strip().lower()
                    if not name or not text:
                        continue
                    for intent in INTENTS:
                        if intent["name"] == name and text not in intent["examples"]:
                            intent["examples"].append(text)
        except OSError:
            pass

    # -- main entry ----------------------------------------------------------
    def respond(self, text, user=None, db=None, ctx=None, actions=None,
                fine_per_day=1.0):
        """Answer `text`. `db` is an optional connection factory, `ctx` the
        session follow-up context, `actions` optional callables like
        {"request_book": (user_id, book_id) -> (ok, msg)}, and `fine_per_day`
        the admin-configured overdue rate for fine answers."""
        self._fine_str = _fmt_fine(fine_per_day)
        t = (text or "").strip()
        if not t:
            return self._reply("Please type your question first.",
                               ["what can you do", "recommend a book"], ctx)
        low = t.lower()
        if any(b in low for b in BLOCKED):
            return self._reply("Sorry, I can't help with internal system details.",
                               ["what can you do"], ctx)

        normed = _norm(t)
        toks = _tokens(normed)
        if not toks:
            # Nothing meaningful ("?", "…") — unless an exact known example.
            for intent in INTENTS:
                if normed in intent["examples"]:
                    return self._reply(self._fill(intent["answer"], user),
                                       intent.get("suggestions", []), ctx or {},
                                       intent=intent["name"])
            return self._reply("That was a bit short for me — ask in a few words, e.g. 'do you have dune' or 'my loans'.",
                               ["what can you do", "do you have dune", "my loans"],
                               ctx or {}, intent="fallback")
            for intent in INTENTS:
                if normed in intent["examples"]:
                    return self._reply(self._fill(intent["answer"], user),
                                       intent.get("suggestions", []), ctx or {},
                                       intent=intent["name"])
            return self._reply("That was a bit short for me — ask in a few words, e.g. 'do you have dune' or 'my loans'.",
                               ["what can you do", "do you have dune", "my loans"],
                               ctx or {}, intent="fallback")
        ctx = dict(ctx or {})
        conn = None
        if db is not None:
            try:
                conn = db() if callable(db) else db
            except Exception:
                conn = None
        try:
            # 1. follow-ups on previous results
            if ctx.get("book_ids"):
                r = self._followup(normed, user, conn, ctx, actions)
                if r is not None:
                    return r
            # 2. live triggers (with disambiguation guards)
            if (any(p in normed for p in _MYLOANS_TRIGGERS)
                    and not (_MYLOANS_GUARD_WORDS & set(toks))
                    and not _MYLOANS_GUARD_RE.search(normed)):
                return self._my_loans(user, conn, ctx)
            if any(p in normed for p in _ME_TRIGGERS):
                return self._me(user, ctx)
            if any(p in normed for p in _RECOMMEND_TRIGGERS):
                return self._recommend(conn, ctx)
            if (any(p in normed for p in _STATS_TRIGGERS)
                    and not (_STATS_BORROW_WORDS & set(toks))):
                return self._stats(conn, ctx)
            if any(p in normed for p in _AVAIL_TRIGGERS) or re.search(
                    r"\bavailable\b|\bavailability\b", normed):
                r = self._availability(normed, conn, ctx)
                if r is not None:
                    return r
            if normed in _BRANCHES_TRIGGERS or any(
                    p in normed for p in ("list branches", "show branches",
                                          "what branches", "which branches",
                                          "the branches", "all branches",
                                          "about branches", "our branches",
                                          "list librar", "our branches")):
                return self._branches(conn, ctx)
            if (("branch" in toks or "branches" in toks)
                    and any(w in toks for w in ("list", "show", "what", "which",
                                                "all", "tell", "about", "our",
                                                "your"))):
                return self._branches(conn, ctx)
            # 3. scored intents (deterministic — no embeddings, no drift)
            intent, score = classify(normed, toks)
            if intent is not None and score >= 45:
                return self._reply(self._fill(intent["answer"], user),
                                   intent.get("suggestions", []), ctx,
                                   intent=intent["name"])
            # 4. legacy HELP_DATA substring fallback (keeps old keys working)
            for k, v in HELP_DATA.items():
                if k in normed or (len(normed) >= 4 and normed in k):
                    return self._reply(v, ["what can you do", "recommend a book"],
                                       ctx, intent="help")
            m = difflib.get_close_matches(normed, list(HELP_DATA.keys()),
                                          n=1, cutoff=0.55)
            if m:
                return self._reply(HELP_DATA[m[0]],
                                   ["what can you do", "recommend a book"],
                                   ctx, intent="help")
            # 5. fallback
            return self._reply(
                ("I didn't quite get that — but here's what I'm good at: checking if we "
                 "have a book ('do you have dune'), summarizing your loans ('my loans'), "
                 "recommending reads, and explaining borrowing, returns, fines and accounts."),
                ["what can you do", "do you have dune", "my loans",
                 "recommend a book"], ctx, intent="fallback")
        finally:
            try:
                if conn is not None and callable(db):
                    conn.close()
            except Exception:
                pass

    # -- plumbing ------------------------------------------------------------
    def _reply(self, answer, suggestions, ctx, intent=None):
        return {"answer": (answer or "").replace("{fine}", self._fine_str),
                "suggestions": list(suggestions or [])[:4],
                "context": dict(ctx or {}),
                "intent": intent}

    @staticmethod
    def _fill(answer, user):
        if "{first}" in answer:
            return answer.format(first=_first_name(user))
        if user:
            return answer
        return answer

    # -- follow-ups ----------------------------------------------------------
    def _followup(self, normed, user, conn, ctx, actions):
        ids = list(ctx.get("book_ids") or [])
        if not ids:
            return None
        idx = None
        for word, i in _ORDINAL.items():
            if re.search(r"\b%s\b" % re.escape(word), normed):
                idx = min(i, len(ids) - 1)
                break
        if idx is None:
            m = re.match(r"^\s*([1-5])\s*$", normed)
            if m:
                idx = min(int(m.group(1)) - 1, len(ids) - 1)
        wants_borrow = bool(_BORROW_FOLLOW.search(normed)) or (
            bool(_YES.match(normed)) and ctx.get("offer_borrow"))
        wants_details = bool(_DETAILS_FOLLOW.search(normed))
        if not wants_borrow and not wants_details:
            return None
        pick = ids[idx if idx is not None else 0]
        book = self._book_by_id(conn, pick) if conn is not None else None
        if book is None:
            book = {"id": pick, "title": ctx.get("titles", {}).get(str(pick), "that book"),
                    "author": "", "available": 1, "lib_name": ""}
        if wants_borrow:
            if not user:
                return self._reply(
                    "I'd love to request it for you — sign in first, then say 'borrow it' again and I'll place the request.",
                    ["sign in", "how do i borrow a book"], ctx, intent="borrow_followup")
            fn = (actions or {}).get("request_book")
            if fn is None:
                return self._reply(
                    "I can't place requests from here — tap Request on its card on the Dashboard and I'll track it under My Books.",
                    ["my loans"], ctx, intent="borrow_followup")
            try:
                ok, msg = fn(user["id"], book["id"])
            except Exception:
                ok, msg = False, "Something went wrong placing that request."
            ctx["offer_borrow"] = False
            return self._reply(
                "%s %s" % ("Done — %s" % msg if ok else msg,
                           "Anything else?" if ok else "Want me to find something else?"),
                ["my loans", "recommend a book", "do you have dune"], ctx,
                intent="borrow_followup")
        title = book.get("title", "that book")
        author = (" by %s" % book["author"]) if book.get("author") else ""
        status = ("Available at %s" % book.get("lib_name", "the library")
                  if book.get("available") else "Currently checked out")
        ctx["offer_borrow"] = bool(book.get("available"))
        tail = (" Say 'borrow it' and I'll request it for you."
                if ctx["offer_borrow"] else "")
        return self._reply("'%s'%s — %s.%s" % (title, author, status, tail),
                           ["borrow it", "recommend a book", "my loans"], ctx,
                           intent="details_followup")

    # -- live answers --------------------------------------------------------
    def _me(self, user, ctx):
        if not user:
            return self._reply("You're browsing as a guest — sign in and I'll know you by name, greet you personally, and summarize your loans.",
                               ["sign in", "sign up"], ctx, intent="me")
        dn = ""
        try:
            dn = (user.get("display_name") or "").strip()
        except AttributeError:
            dn = ""
        if dn and dn != user.get("username"):
            return self._reply("You're signed in as %s (@%s)." % (dn, user.get("username")),
                               ["my loans", "my account"], ctx, intent="me")
        return self._reply("You're signed in as %s." % user.get("username"),
                           ["my loans", "my account"], ctx, intent="me")

    def _availability(self, normed, conn, ctx):
        q = _strip_avail_query(normed)
        if not q:
            return self._reply("Which title or author should I look up? Give me a name and I'll check the live catalog.",
                               ["recommend a book", "how do i search"], ctx,
                               intent="availability")
        if conn is None:
            return self._reply("Use the search bar on Home or Dashboard to look up '%s' by title or author." % q,
                               ["how do i search"], ctx, intent="availability")
        rows = self._search_books(conn, q, 5)
        if not rows:
            return None  # fall through to intent matching (maybe a help topic)
        ctx = dict(ctx)
        ctx["book_ids"] = [r["id"] for r in rows]
        ctx["titles"] = {str(r["id"]): r["title"] for r in rows}
        ctx["offer_borrow"] = any(r["available"] for r in rows)
        if len(rows) == 1:
            r = rows[0]
            state = ("Available at %s" % r["lib_name"] if r["available"]
                     else "Currently checked out")
            return self._reply(
                "Yes — '%s' by %s: %s.%s" % (
                    r["title"], r["author"], state,
                    " Say 'borrow it' and I'll request it for you." if r["available"] else ""),
                ["borrow it", "my loans", "recommend a book"], ctx,
                intent="availability")
        lines = ["I found %d matches:" % len(rows)]
        for i, r in enumerate(rows):
            state = "Available at %s" % r["lib_name"] if r["available"] else "Checked out"
            lines.append("%d. '%s' by %s — %s" % (i + 1, r["title"], r["author"], state))
        lines.append("Say 'the first one' for details or 'borrow it' and I'll request it for you.")
        return self._reply(" ".join(lines),
                           ["the first one", "borrow it", "recommend a book"], ctx,
                           intent="availability")

    def _my_loans(self, user, conn, ctx):
        if not user:
            return self._reply("Your loans live under My Books — sign in first and I'll summarize everything: active loans, pending requests and due dates.",
                               ["sign in", "how do i borrow a book"], ctx,
                               intent="my_loans")
        if conn is None:
            return self._reply("Check My Books for your current shelf — active loans, pending requests and due dates.",
                               ["how do i return a book"], ctx, intent="my_loans")
        cur = conn.cursor()
        cur.execute("""SELECT b.title, bl.expire_date, bl.status FROM borrow_log bl
                       JOIN books b ON bl.book_id=b.id
                       WHERE bl.user_id=? AND bl.return_date IS NULL
                       ORDER BY bl.id DESC""", (user["id"],))
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return self._reply("Your shelf is empty, %s — nothing borrowed, nothing pending. Tell me a title and I'll check the catalog." % _first_name(user),
                               ["recommend a book", "do you have dune"], ctx,
                               intent="my_loans")
        approved = [r for r in rows if r["status"] == "approved"]
        pending = [r for r in rows if r["status"] == "pending"]
        parts = []
        if approved:
            bits = []
            for r in approved[:5]:
                try:
                    exp = datetime.fromisoformat(r["expire_date"])
                    left = (exp.replace(tzinfo=None) - datetime.now()).days
                    when = ("overdue!" if left < 0 else "%d day(s) left" % max(0, left))
                except Exception:
                    when = "due %s" % _fmt_day(r["expire_date"])
                bits.append("'%s' (%s)" % (r["title"], when))
            parts.append("With you (%d): %s." % (len(approved), "; ".join(bits)))
        if pending:
            parts.append("Awaiting librarian (%d): %s." % (
                len(pending), "; ".join("'%s'" % r["title"] for r in pending[:5])))
        return self._reply("Here's your shelf, %s — %s Full details live under My Books." % (
            _first_name(user), " ".join(parts)),
            ["how do i return a book", "fine", "recommend a book"], ctx,
            intent="my_loans")

    def _recommend(self, conn, ctx):
        if conn is None:
            return self._reply("Tell me a genre or title you liked and I'll point you at the shelves — or browse the featured shelves on Home.",
                               ["do you have dune", "how do i search"], ctx,
                               intent="recommend")
        cur = conn.cursor()
        cur.execute("""SELECT b.id, b.title, b.author, COALESCE(l.name,'Main Library') AS lib_name
                       FROM books b LEFT JOIN libraries l ON b.library_id=l.id
                       WHERE b.available=1 ORDER BY RANDOM() LIMIT 5""")
        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return self._reply("Every book is checked out right now — impressive demand! Try again soon or ask a librarian for arrivals.",
                               ["my loans", "branches"], ctx, intent="recommend")
        ctx = dict(ctx)
        ctx["book_ids"] = [r["id"] for r in rows]
        ctx["titles"] = {str(r["id"]): r["title"] for r in rows}
        ctx["offer_borrow"] = True
        lines = ["Fresh off the available shelf:"]
        for i, r in enumerate(rows):
            lines.append("%d. '%s' by %s (%s)" % (i + 1, r["title"], r["author"], r["lib_name"]))
        lines.append("Say 'the first one' for details or 'borrow it' to request it.")
        return self._reply(" ".join(lines), ["the first one", "borrow it", "my loans"],
                           ctx, intent="recommend")

    def _branches(self, conn, ctx):
        if conn is None:
            return self._reply(HELP_DATA["branches"], ["how do i search"], ctx,
                               intent="branches")
        cur = conn.cursor()
        cur.execute("SELECT id, name, code FROM libraries ORDER BY id")
        libs = [dict(r) for r in cur.fetchall()]
        names = ", ".join("'%s'" % l["name"] for l in libs)
        return self._reply("We have %d %s: %s. Every book card shows its home branch — and you can filter by branch on the Dashboard." % (
            len(libs), "branch" if len(libs) == 1 else "branches", names),
            ["how do i search", "do you have dune"], ctx, intent="branches")

    def _stats(self, conn, ctx):
        if conn is None:
            return self._reply("The Home page shows live counts of books, branches and members.",
                               ["how do i search"], ctx, intent="stats")
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) c FROM books")
        n_books = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM books WHERE available=1")
        n_avail = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM libraries")
        n_libs = cur.fetchone()["c"]
        return self._reply("The collection holds %d books (%d available right now) across %d %s." % (
            n_books, n_avail, n_libs, "branch" if n_libs == 1 else "branches"),
            ["recommend a book", "do you have dune"], ctx, intent="stats")

    # -- db helpers ----------------------------------------------------------
    @staticmethod
    def _search_books(conn, q, limit=5):
        cur = conn.cursor()
        like = "%%%s%%" % q.lower()
        cur.execute("""SELECT b.id, b.title, b.author, b.available,
                              COALESCE(l.name,'Main Library') AS lib_name
                       FROM books b LEFT JOIN libraries l ON b.library_id=l.id
                       WHERE LOWER(b.title) LIKE ? OR LOWER(b.author) LIKE ?
                       ORDER BY b.id DESC LIMIT ?""", (like, like, limit))
        return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def _book_by_id(conn, book_id):
        cur = conn.cursor()
        cur.execute("""SELECT b.id, b.title, b.author, b.available,
                              COALESCE(l.name,'Main Library') AS lib_name
                       FROM books b LEFT JOIN libraries l ON b.library_id=l.id
                       WHERE b.id=?""", (book_id,))
        row = cur.fetchone()
        return dict(row) if row else None


_ASSISTANT = Assistant()


def assistant_answer(text):
    """Backwards-compatible plain-string wrapper (no user/db context)."""
    return _ASSISTANT.respond(text)["answer"]
