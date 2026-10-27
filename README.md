# Mail Priority Assistant

**English** | [中文](README.zh-CN.md)

A small agent that sorts a Gmail inbox into five priority levels (P0–P4) and labels each
message in place. It is the "five folders + mail agent" kernel of an AISC 1000A case proposal:
everything below is runnable, the chatbot / vector-search layers are still on the roadmap.

```
Gmail API ──fetch / incremental sync──> SQLite (mail.db) ──DeepSeek──> Gmail labels (P0–P4)
                                              │
                                              └── structured rows, ready for a vector store,
                                                  a chatbot, or calendar extraction
```

## Priority levels

| Label | Meaning | Examples |
|---|---|---|
| `P0-Action required` | Must act, usually with a deadline | add/drop, exam changes, tuition, visa, housing, ITSC alerts |
| `P1-Major & career` | Relevant to major or career | CPEG/SENG/COMP notices, internships, co-op, research, competitions, tech talks |
| `P2-Admin info` | Administrative, no action needed | library, system maintenance, policy reminders, weekly digests |
| `P3-Events` | Enjoyable but not career-critical | arts, sports, volunteering, society recruitment, GE talks |
| `P4-Noise` | Marketing or sensitive noise | credit cards, OTP codes, bank statements, unrelated vendors |

Exactly one label per message — re-running is idempotent and never stacks labels.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows; macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env            # then fill in DEEPSEEK_API_KEY
python auth.py                    # opens a browser, writes token.json

python run_pipeline.py --limit 10 -v
```

`.env.example` lists every supported variable. Paths default to the project root and can be
overridden with `AIDE_DB_PATH`, `AIDE_TOKEN_PATH`, `AIDE_CREDENTIALS_PATH`, `AIDE_DATA_DIR`.

### Google Cloud OAuth (one-time, per Google account)

`client_secret*.json` is deliberately **not** in this repository — it belongs to whoever runs
the tool. Each user creates their own:

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create a project.
2. **APIs & Services → Library** → enable **Gmail API**.
3. **OAuth consent screen** → External → add your own address under **Test users**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**, then download the JSON.
5. Save it in the project root as `client_secret_*.json` (any file matching that pattern is
   picked up automatically, or point `AIDE_CREDENTIALS_PATH` at it).

Then run `python auth.py` and complete the browser flow.

## Setup window

Double-click `setup.bat`, or run `python setup_gui.py`:

![Setup window](docs/setup_window.png)

- Leave both fields blank and press Enter to keep the values already in `.env`.
- The Gmail address gets three checks: format, Gmail domain (non-Gmail asks for confirmation),
  and a `getProfile` call against the authorized token. If no usable token exists it says so
  explicitly instead of silently passing.
- The API key is checked for the `sk-` prefix, length and stray whitespace. Saving edits only
  those two lines in `.env`; comments and other keys are preserved byte for byte.

## Commands

| Command | What it does |
|---|---|
| `python run_pipeline.py` | Incremental sync, then classify whatever is pending |
| `python run_pipeline.py --limit 5 --dry-run` | Classify only — no labels, no DB writes (tune the rubric with this) |
| `python run_pipeline.py --full` | Ignore `historyId` and rescan the inbox |
| `python run_pipeline.py --reset` | Clear classifications and re-judge everything |
| `python run_pipeline.py --non-interactive` | Never open a browser (cron / headless) |
| `python -m unittest discover -s tests -v` | Offline test suite (no network, no real mailbox) |

## How it works

**Fetch.** `gmail_reader.py` walks the MIME tree, prefers `text/plain`, converts `text/html`
to text (scripts and styles dropped, links expanded), skips attachments, and decodes with
`charset → utf-8 → gb18030` fallbacks. Messages are pulled with `batchGet` (N requests → 1)
and, after the first run, with Gmail's `historyId` cursor.

**Store.** `store.py` keeps a single `emails` table plus a `sync_state` key/value table.
The schema self-migrates on startup (`ALTER TABLE` for missing columns), writes use `UPSERT`
so a re-fetch refreshes metadata without clobbering a classification, and each failure bumps
`error_count`; after three attempts a message is left alone.

**Classify.** `classify.py` calls DeepSeek's Responses API with a strict tool schema and a
rubric aimed at a first-year CPEG student. `tool_choice="required"` is attempted first and
degraded to `auto` if the endpoint rejects it; if the model answers in prose anyway, the
priority is recovered from the text. Authentication errors abort the run instead of burning
through the whole batch.

**Label.** `label.py` creates the five labels and applies them in a single `modify` call that
adds the target and removes the other four, so the five folders stay mutually exclusive.

## Layout

| File | Responsibility |
|---|---|
| `config.py` | Paths, environment variables, constants |
| `utils.py` | Logging, exponential-backoff retry, text cleaning |
| `auth.py` | OAuth (`gmail.modify`: read + label, cannot send or delete) |
| `gmail_reader.py` | MIME parsing, HTML→text, batch fetch, incremental sync |
| `store.py` | SQLite schema/migration, UPSERT, retry accounting, sync state |
| `classify.py` | DeepSeek call, tool-call parsing, rubric |
| `label.py` | P0–P4 labels, exclusive application |
| `settings.py` | Email/key validation, `.env` editing, account cross-check |
| `setup_gui.py` | Tkinter setup window |
| `run_pipeline.py` | CLI entry point |

## Tests

31 offline tests cover MIME parsing, HTML extraction, schema migration, UPSERT semantics,
exclusive labelling, model-response parsing and fallbacks, and one end-to-end run of `main()`
with a fake Gmail service and a fake model client.

```bash
python -m unittest discover -s tests -v
```

## Privacy

- Only `gmail.modify` is requested: read mail and change labels, but never send or permanently
  delete anything.
- `.env`, `token.json`, `client_secret*.json` and `mail.db` are git-ignored; nothing leaves the
  machine except the prompt sent to the model API.
- The model sees sender, subject, date and up to 6000 characters of body text per message.

## Known limitations

- If the OAuth app stays in **Testing** mode, Google expires the refresh token after 7 days;
  re-run `python auth.py` when that happens. Publishing the app avoids it.
- `gmail.modify` is required to apply labels. For read-only analysis, switch the scope to
  `gmail.readonly` and drop the labeling step.
- This covers inbox triage only — no chatbot, vector search, calendar extraction or daily
  digest yet.

## Roadmap

The case proposal describes a larger system; this repository covers the inbox-triage layer.
Still missing: chatbot front end, vector retrieval over mail history, schedule extraction
against the UST timetable, a daily lunch-time digest, and the private-mailbox relay.

## License

[MIT](LICENSE)
