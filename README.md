# Session Memory for Claude Code

A Claude Code plugin that keeps a summary of each session. It writes one at
every compaction and at every session end, keeps it on a git branch that
never merges, and hands the newest to the next session as it starts, or to
the same session after it compacts.

It was built for a homelab run almost entirely by Claude Code, where a
session's context compacts several times a day and a new session starts
cold. It's published here so it can be used anywhere.

## Install

In Claude Code:

```
/plugin marketplace add jstanton617/claude-session-memory
/plugin install session-memory@jstanton617
```

It records from the next compaction or session end, in any git repository you
open a session in. Before you rely on it, read [Privacy](#privacy) and edit the
rules.

## What a record holds

Each record is a Markdown file in `sessions/` on the `memory` branch, with the
same sections every time:

- **Done**: what was finished, with its evidence;
- **In progress**: exactly where each thing stands;
- **Decided**: what you decided, in your own words, quoted;
- **Rules to keep**: standing instructions you gave, worth writing into
  `CLAUDE.md`;
- **Not decided**: every open question;
- **Continue from here**: at a compaction, the next step.

`sessions/README.md` indexes them, newest first. By default the branch is the
ref `refs/session-memory/memory` in your clone, outside your own branches, so
no checkout or `git push --all` ever touches it. Browse it with:

```bash
git log refs/session-memory/memory
```

```bash
git show refs/session-memory/memory:sessions/README.md
```

With pushing on, the records are on `origin/memory` instead, so it's
`git log origin/memory`.

## How it works

| Hook | What it does |
|---|---|
| **PreCompact** | Summarizes the stretch since the last compaction from the transcript, saves the record, and keeps the stretch for PostCompact. It prints nothing, since a PreCompact hook's output joins the compaction's instructions |
| **SessionStart** (compact) | Hands the continued session exactly that record, or a one-line note that it wasn't written. It hands on nothing when this compaction kept no stretch, rather than an older one |
| **PostCompact** | The only hook given Claude Code's own compaction summary. When it has one, a detached process rewrites the record from both accounts: the transcript's digest wins where they disagree |
| **SessionEnd** | The record for the stretch since the last compaction, in a detached process |
| **SessionStart** (startup, resume, clear) | Hands a new session the newest record |

- **The summarizer is a separate `claude -p` call**, boxed in: a temporary
  directory, `--restricted` (no user or project settings, so none of their
  hooks; managed settings still apply), and `--tools ""` with
  `--strict-mcp-config` (text only). An environment flag stops it from
  summarizing itself. It uses your own Claude Code login.
- **A hand-off fits Claude Code's 10,000-character cap.** A longer record loses
  bullets from Done first, then Decided, Rules to keep, Not decided and In
  progress, and keeps Continue from here. Each cut is marked, and the hand-off
  names the whole record's local copy. Only a record still too long after all
  that is cut at a line.
- **Records are saved by git plumbing**, through a temporary index, so your
  working tree and checked-out branch are never touched.
- **Nothing stops a session.** Every failure goes to `.git/session-memory.log`
  and the hook exits 0. A copy of each record stays in `.git/session-memory/`.

## Options

Claude Code asks for these when you enable the plugin, and lists them in
`/config`. An environment variable overrides each one.

| Option | Variable | Default | What it does |
|---|---|---|---|
| Push records to the remote | `SESSION_MEMORY_PUSH` | off | Pushes the `memory` branch to the remote, so another machine or a cloud session gets the records. Off, the branch stays in this clone |
| File undecided items as GitHub issues | `SESSION_MEMORY_ISSUES` | off | Files each record's undecided items as issues with `gh`, ten at most per record, and cites existing issues and pull requests instead of duplicating them |

Also from the environment:

| Variable | Default |
|---|---|
| `SESSION_MEMORY_BRANCH` | `memory` |
| `SESSION_MEMORY_REMOTE` | `origin` |
| `SESSION_MEMORY_MODEL` | `sonnet` |
| `SESSION_MEMORY_CLAUDE`, `SESSION_MEMORY_GH` | `claude`, `gh`: the commands to run |
| `SESSION_MEMORY_DIGEST_CHARS` | `400000`: the most of a stretch the summarizer reads |
| `SESSION_MEMORY_SUMMARIZE_SECONDS` | `540`, inside PreCompact's 600 |
| `SESSION_MEMORY_GH_SECONDS` | `30`, per GitHub call |
| `SESSION_MEMORY_REPO` | taken from the remote's URL; set `owner/repo` when that doesn't say |
| `SESSION_MEMORY_CHILD` | unset. The plugin sets it for its own summarizer, and any value turns the hooks off |

The options in `/config` reach only the hooks. When you run the script by hand,
set the variables yourself.

## Privacy

**Records are raw model output.** The summarizer is told what never to write,
and common token shapes are redacted, but nothing reviews a record before it's
saved. Treat the `memory` branch like any other place a transcript could land.

- **Edit the rules first.** The summarizer follows the rules in
  [`session-memory-rules.md`](plugins/session-memory/scripts/session-memory-rules.md):
  no one's name but yours, no customer's name or data, no private hostnames.
  To give a repository its own, copy that file to
  `.claude/session-memory-rules.md` in the repository and edit the copy.
- **Pushing is off by default.** Turn it on only for a repository whose remote
  you'd trust with a transcript, never a shared or public one.
- **Redaction catches token shapes:** GitHub, Slack, Anthropic, Tailscale and
  Vault tokens, AWS key ids, private keys, and Datadog keys where they're named
  (`DD_API_KEY=...`, a `DD-API-KEY` header). A bare hex key has no shape that a
  commit hash wouldn't also match, so the real defence is never putting a
  credential value in a session.
- **Some files skip the privacy rules.** They're readable by you alone, and
  never pushed. All are under `.git/session-memory/`:
  - `own/`: Claude Code's own compaction summaries, which quote your messages
    in full, with only token shapes redacted;
  - `pending/`: each compaction's digest, and Claude Code's summary until the
    second pass reads it, both unredacted. One left behind is removed by a
    later compaction once it's a day old.

## Requirements

- macOS or Linux, with Python 3.9 or later. It uses only the standard library.
- git, in a repository.
- The Claude Code CLI on your `PATH`, logged in, and recent: the summarizer
  needs `--restricted`, and the hooks follow Claude Code 2.1.296's order. It's
  tested on 2.1.310.
- `gh`, logged in, only if you turn on issue filing.

**Cost:** each compaction makes two summarizer calls, one before and one
after, and each session end makes one. Each reads the stretch's digest: your
messages, Claude's replies, and one line per tool call, up to 400,000
characters. The second call also reads up to 60,000 characters of Claude
Code's own summary.

## By hand

The plugin's script also runs from a shell. Clone this repository, then run
the script from inside your own:

```bash
git clone https://github.com/jstanton617/claude-session-memory ~/claude-session-memory
```

```bash
python3 ~/claude-session-memory/plugins/session-memory/scripts/session-memory.py latest
```

prints the newest record.

```bash
python3 ~/claude-session-memory/plugins/session-memory/scripts/session-memory.py backfill --transcript ~/.claude/projects/<project>/<session>.jsonl --dry-run
```

lists an old session's finished stretches. Without `--dry-run` it records each
one, and files no issues for them. If you turned pushing on in `/config`, put
`SESSION_MEMORY_PUSH=1` in front of each command, since `/config`'s options
reach only the hooks.

## Known gaps

- **A subagent's own compaction** runs these hooks with the main session's id,
  and can rewrite the main record with the subagent's summary.
- **SessionEnd may not fire** when a cloud session is archived, so its last
  stretch can go unrecorded.
- **Windows** isn't supported.

## Tests

The tests need pytest, which the plugin itself doesn't:

```bash
python3 -m pip install pytest
```

```bash
python3 -m pytest tests
```

`claude` and `gh` are stubs, and the remote is a bare repository in a
temporary directory: nothing reaches a model or GitHub.

## License

MIT. See [LICENSE](LICENSE).
