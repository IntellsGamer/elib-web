# Teaching the eLib assistant

The assistant (`assistant_engine.py`) is a **deterministic scoring engine**:
exact phrases → weighted keywords → guarded fuzzy matching, plus live answers
from the library database (catalog lookup, your loans, recommendations,
branches) and multi-turn follow-ups ("the first one", "borrow it").

There is deliberately **no neural model in the repo**, so there is nothing to
download, nothing to version, and answers are reproducible.

## Teach it new phrasings

```bash
# 1. check current accuracy (must stay green)
python train_assistant.py check

# 2a. teach one phrasing
python train_assistant.py add borrow "how do i take a book out"

# 2b. or interactive: type phrasings, confirm/correct the prediction
python train_assistant.py teach
```

Taught phrasings land in `assistant_examples.jsonl` — **plain text, always
safe to commit**. The engine loads them on top of its built-in examples.

## Add a whole new topic

1. Add an entry to `INTENTS` in `assistant_engine.py`
   (`name`, `title`, `desc`, `examples`, `keywords`, `answer`, `suggestions`).
2. Add 2–4 cases to `EVAL_CASES` in `train_assistant.py`.
3. Run `python train_assistant.py check` until everything passes.

## Scoring rules (why it stays accurate)

- Exact example match wins outright.
- Full-phrase containment beats fuzzy similarity.
- Fuzzy whole-string matches are gated: every content word must resemble a
  word on the other side (kills "log in" vs "log out" confusion), and fuzzy
  evidence is capped below certain evidence.
- Live triggers (catalog, loans, …) have disambiguation guards so "how many
  books can I borrow" doesn't answer collection stats.

## ⚠️ Never commit model artifacts

If you ever attach a heavy model of your own (embeddings, transformers,
fine-tunes), **train it yourself on your own machine and keep the outputs
out of git**:

- `models/`, `checkpoints/`
- `*.pt`, `*.bin`, `*.safetensors`, `*.onnx`

These are already in `.gitignore`. Committable: `assistant_engine.py`,
`assistant_examples.jsonl`, `train_assistant.py`, help keys and docs.
