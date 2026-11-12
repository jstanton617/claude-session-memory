#!/usr/bin/env python3
"""SESSION MEMORY: each Claude Code session's summary, written at every
compaction and session end, kept on a `memory` branch, and the newest handed
to the next session.

    session-memory.py hook        the hook entry: reads the hook's JSON on stdin
    session-memory.py record --transcript T --session S --trigger compact:auto
    session-memory.py combine --session S --trigger compact:auto --own FILE
                                  PostCompact's second pass, FILE holding the session's summary
    session-memory.py latest      prints the newest summary
    session-memory.py digest T    prints what the summarizer would read
    session-memory.py backfill --transcript T [--dry-run]   one summary per finished stretch

As hooks, in the plugin's hooks/hooks.json, in the order a compaction runs
them (Claude Code 2.1.296's own code):
- PreCompact: summarize the stretch since the last compaction from the digest
  and save it, before Claude Code compacts. It has 600 s, and runs in the
  foreground. It keeps the stretch's digest for PostCompact, and prints
  nothing: a PreCompact hook's stdout is joined into the compaction's
  instructions.
- SessionStart (compact): hand the continued session exactly the record
  PreCompact named, or a note that it wasn't written, never another. Only
  SessionStart's additionalContext reaches the model, and only up to 10,000
  UTF-16 units, so a longer record is shortened to fit (fit(), below).
- PostCompact: the only hook given the session's own summary, compact_summary,
  and the last to run. A detached process keeps it as written in
  .git/session-memory/own/, never pushed, then rewrites the record under the
  same name from the kept stretch and it together. The continued session
  already holds the session's own summary; a later one is handed the combined
  record while it's the newest.
- SessionEnd: the digest's record for the stretch since the last compaction
  or the start, in a detached process. SessionEnd hooks can't be async, and
  their budget, 1.5 s by default, is far short of a model call.
- SessionStart (startup, resume, clear): hand a new session the newest summary.
- PreCompact and PostCompact both log the fields they were given, and the
  length of compact_summary, never its text. A real compaction showed it
  absent from PreCompact and present in PostCompact.

THE SUMMARY IS A SEPARATE MODEL CALL, over two accounts of the stretch. One is
the digest, below. After a compaction the other is the session's own summary:
Claude Code's, written in the session with its whole context window. No hook
can speak to the session, so both go to `claude -p`:
- in a temporary directory, so the project's hooks don't load;
- with `--restricted`, which ignores the user's and the project's settings
  files, and so every hook in them (managed settings still apply);
- with `--tools ""` and `--strict-mcp-config`, so it can only write text;
- with SESSION_MEMORY_CHILD set, which makes this script's `hook` return at
  once, so it can never summarize the summarizer.
Not `--bare`: that skips hooks too, but accepts only ANTHROPIC_API_KEY, never
the user's own login. The instructions are session-memory-prompt.md, beside
this, with the privacy rules of the project's .claude/session-memory-rules.md,
or else of session-memory-rules.md, beside this.

THE DIGEST IS THE TRANSCRIPT SINCE ITS LAST compact_boundary. That's the user's
and the assistant's text, and one line per tool call, with tool output left
out: about 40k tokens for a busy stretch. The previous compaction's own
summary leads it, as context.

WITH SESSION_MEMORY_ISSUES ON, UNDECIDED ITEMS BECOME ISSUES. The summarizer is
shown the open issues and, apart, the open pull requests, ends each undecided
bullet with an existing `(#N)` or a `(new: K)`, and lists the new ones. A
merge waiting on a pull request takes the pull request's number. A new one
whose title starts with merge, merges or merging, and whose every #N is an
open pull request, is never filed (held_by_pulls). This script files the new
ones with `gh` and writes their numbers in. Ten at most per summary, so a
confused summary can't flood the tracker. Off, the default, nothing here calls
GitHub, and undecided items are plain bullets.

SAVED BY GIT PLUMBING, never a checkout. The file and a regenerated
sessions/README.md are written into a temporary index built from the memory
branch, and committed onto it. By default that's the local ref
refs/session-memory/memory, outside refs/heads/, so no push of branches sends it.
With SESSION_MEMORY_PUSH on, it's the remote's: built from <remote>/memory and
pushed, and a rejected push is refetched and retried. The working tree and its
branch are never touched. A copy stays under .git/session-memory/ either way.

NOTHING HERE STOPS A SESSION. Every failure is logged to
.git/session-memory.log, and the hook exits 0.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPT = HERE / "session-memory-prompt.md"
RULES = HERE / "session-memory-rules.md"
PROJECT_RULES = ".claude/session-memory-rules.md"  # a project's own rules, over RULES
CHILD = "SESSION_MEMORY_CHILD"


def setting(name, default=""):
    """A knob: SESSION_MEMORY_<NAME> in the environment, else the plugin's own
    option, which Claude Code exports to its hooks as CLAUDE_PLUGIN_OPTION_<NAME>."""
    for var in (f"SESSION_MEMORY_{name}", f"CLAUDE_PLUGIN_OPTION_{name}"):
        value = os.environ.get(var, "").strip()
        if value:
            return value
    return default


def switch(name):
    return setting(name).lower() in ("1", "true", "yes", "on")


BRANCH = setting("BRANCH", "memory")
REMOTE = setting("REMOTE", "origin")
CLAUDE = setting("CLAUDE", "claude")
GH = setting("GH", "gh")
MODEL = setting("MODEL", "sonnet")
PUSH = switch("PUSH")      # the branch is the remote's, and pushed; off, it never leaves this clone
ISSUES = switch("ISSUES")  # undecided items are filed as GitHub issues; off, nothing calls GitHub
DIGEST_CHARS = int(setting("DIGEST_CHARS", "400000"))
SUMMARIZE_SECONDS = int(setting("SUMMARIZE_SECONDS", "540"))
PREVIOUS_CHARS = 30000
TOOL_CHARS = 200
NET_SECONDS = 30
GH_SECONDS = int(setting("GH_SECONDS", "30"))
TIMED_OUT = 124  # gh_api's return code for a call that ran past its timeout, as timeout(1)'s
MAX_NEW_ISSUES = 10
LABEL = "session-memory"
DIR = "sessions"
INDEX = f"{DIR}/README.md"
OWN_HEADING = "The session's own compaction summary"
OWN_CHARS = 60000
OWN_DIR = "own"
PENDING = "pending"  # under .git/session-memory/: PreCompact's digest, kept for PostCompact
HANDED = "handed"    # under .git/session-memory/: other writers' records, copied for a hand-off
# Claude Code caps a hook's additionalContext at 10,000 characters, counted as
# JavaScript does, in UTF-16 units. Over it, Claude gets a file path and the
# first 2,000, and isn't asked to read the file (the hooks reference). So a
# longer record is shortened to this, least actionable section first.
HANDOFF_UNITS = 9500
HANDOFF_WITHIN = 1800  # seconds: a compaction's stretch is handed on by its own SessionStart
TRIM_FIRST = ("## Done", "## Decided", "## Rules to keep", "## Not decided", "## In progress")
SECTIONS = ("## Done", "## In progress", "## Decided", "## Rules to keep", "## Not decided",
            "## Continue from here")
# Whether a record is a compaction's is the code's to say, never the
# summarizer's. Asked to write a next step "only when the trigger says
# compact", it wrote "Not a compaction." in 16 of 29 compaction records,
# counted 2026-10-14, the first pass of a live compact:auto among them.
CONTINUE = {
    True: ("This is a compaction. So 'Continue from here' is a short prompt the continued "
           "session can follow: the next step, and anything it must check first."),
    False: "This isn't a compaction. So 'Continue from here' holds only: Not a compaction.",
}
NO_NEXT_STEP = ("The summary gave no next step. Read In progress, and check the state it "
                "describes before acting.")

# Common credential shapes, redacted before anything is saved or filed. The
# prompt forbids them; this is what holds if a summary writes one anyway. A
# bare hex key, Datadog's among them, has no shape that every commit hash
# wouldn't match, so Datadog's are caught only where they're named: as
# DD_API_KEY=..., DATADOG_APP_KEY: ..., TF_VAR_DD_API_KEY=..., a DD-API-KEY
# header, or a --dd-api-key flag.
SECRETS = [
    re.compile(r"(?i)(?<![A-Za-z0-9])(?:(?:DD|DATADOG)_(?:API|APP|APPLICATION)_KEY"
               r"|DD-(?:API|APPLICATION)-KEY|--dd-(?:api|app|application)-key)"
               r"[\"']?(?:\s*[:=]\s*|\s+)[\"']?[0-9a-f]{32,40}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\btskey-[a-z]+-[A-Za-z0-9-]{10,}"),  # the id and the secret after it
    re.compile(r"\bhv[sbr]\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # key ids: a secret key has no shape
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
]
# Harness noise: reminders, and the notices background tasks send on finishing.
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>"
                      r"|<task-notification>.*?</task-notification>",
                      re.DOTALL)
NOTICE_CHARS = 300


# --- small helpers ----------------------------------------------------------

def now():
    return datetime.datetime.now(datetime.timezone.utc)


def git(root, *args, env=None, stdin=None, timeout=NET_SECONDS, check=True):
    p = subprocess.run(["git", "-C", str(root), *args], input=stdin, capture_output=True,
                       text=True, env=env, timeout=timeout, check=False)
    if check and p.returncode:
        raise RuntimeError(f"git {args[0]} exited {p.returncode}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def repo_root(cwd=None):
    start = cwd or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    p = subprocess.run(["git", "-C", start, "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True, check=False)
    if p.returncode:
        raise RuntimeError(f"{start} is not in a git repository")
    return Path(p.stdout.strip())


def git_dir(root):
    return Path(git(root, "rev-parse", "--absolute-git-dir"))


def log(root, msg):
    try:
        with open(git_dir(root) / "session-memory.log", "a") as f:
            f.write(f"{now():%Y-%m-%dT%H:%M:%SZ} {msg}\n")
    except Exception:  # noqa: BLE001, S110 - logging must never be what fails
        pass


def redact(text):
    count = 0
    for pat in SECRETS:
        text, n = pat.subn("[REDACTED: a credential was here]", text)
        count += n
    return text, count


# --- the digest -------------------------------------------------------------

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def tool_line(block):
    i = block.get("input") or {}
    what = i.get("description") or i.get("command") or i.get("file_path") or i.get("url") \
        or i.get("query") or i.get("sql_query") or ""
    return f"{block.get('name', '?')}: {' '.join(str(what).split())[:TOOL_CHARS]}"


def timeline(lines):
    """The session's entries once each, in time order. A transcript isn't one line
    of history: a resumed or rewound session appends copies of earlier entries,
    so the same compaction can appear three times, and a copy can be re-parented,
    so parentUuid can loop (both measured 2026-10-13). So each uuid counts once,
    its first copy, and the order is the timestamps'. A sidechain, a subagent's
    own thread, and an entry without a uuid or a time, the app's bookkeeping, are
    left out."""
    by_uuid, out = {}, []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or not e.get("uuid") or not e.get("timestamp"):
            continue
        if e["uuid"] in by_uuid or e.get("isSidechain"):
            continue
        by_uuid[e["uuid"]] = e
        out.append(e)

    # A compaction's summary is stamped a moment BEFORE its boundary, with
    # attachments between, so it sorts by its boundary's time instead, just
    # after it: the summary opens the stretch it summarizes the past for. Its
    # boundary is up its parentUuid, one or four steps away (measured).
    def when(e):
        if e.get("isCompactSummary"):
            u = e.get("parentUuid")
            for _ in range(10):
                up = by_uuid.get(u)
                if up is None:
                    break
                if is_boundary(up):
                    return (up["timestamp"], 1)
                u = up.get("parentUuid")
        return (e["timestamp"], 0)

    out.sort(key=when)
    return out


def is_boundary(e):
    return e.get("type") == "system" and e.get("subtype") == "compact_boundary"


def stretches(entries):
    """(start, end) ranges of `entries`: from the start, or a compaction, to the
    next."""
    edges = [0, *[n for n, e in enumerate(entries) if is_boundary(e)], len(entries)]
    return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1) if edges[i + 1] > edges[i]]


def read_chain(transcript):
    return timeline(Path(transcript).read_text(errors="replace").splitlines())


def digest(transcript, limit=DIGEST_CHARS, span=None):
    """(text, meta) for the session since its last compaction, or for the
    (start, end) range `span` of its timeline."""
    entries = read_chain(transcript)
    if span is None:
        span = stretches(entries)[-1] if entries else (0, 0)
    return digest_lines(entries[span[0]:span[1]], limit)


def digest_lines(entries, limit=DIGEST_CHARS):
    previous, items, first, last, branch = "", [], None, None, None
    for e in entries:
        if e.get("timestamp"):
            first = first or e["timestamp"]
            last = e["timestamp"]
        branch = e.get("gitBranch") or branch
        content = (e.get("message") or {}).get("content")
        # A message sent while a turn runs is an attachment, a queued_command:
        # the user's when its origin is human, otherwise the app's own notice.
        # A task's notice, a background command finishing, is left out.
        if e.get("type") == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "queued_command" and isinstance(a.get("prompt"), str):
                origin = (a.get("origin") or {}).get("kind")
                said = REMINDER.sub("", a["prompt"]).strip()
                if origin == "human" and said:
                    items.append(f"USER (while a turn ran): {said}")
                elif origin is None and said:
                    items.append(f"NOTICE: {' '.join(said.split())[:NOTICE_CHARS]}")
            continue
        if e.get("type") == "user":
            if e.get("isCompactSummary"):
                previous = text_of(content)[:PREVIOUS_CHARS]
                continue
            said = REMINDER.sub("", text_of(content)).strip()
            if said:
                items.append(f"USER: {said}")
        elif e.get("type") == "assistant" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append(f"ASSISTANT: {b['text'].strip()}")
                elif b.get("type") == "tool_use":
                    items.append(f"TOOL {tool_line(b)}")
    head = f"PREVIOUS COMPACTION'S SUMMARY, for context:\n{previous}\n\n" if previous else ""
    dropped = 0
    body = "\n\n".join(items)
    while items and len(head) + len(body) > limit:
        items.pop(0)
        dropped += 1
        body = "\n\n".join(items)
    if dropped:
        body = f"[{dropped} earlier items left out, over the digest's limit]\n\n{body}"
    meta = {"items": len(items), "dropped": dropped, "started": first, "ended": last,
            "branch": branch}
    return head + "SINCE THEN:\n\n" + body, meta


# --- the summary ------------------------------------------------------------

# --- GitHub, by its REST API ---------------------------------------------------
#
# Through `gh api`, never `gh issue`: `gh issue` is GraphQL, which a cloud
# session refuses ("HTTP 403: GitHub GraphQL is not available from Claude Code
# sessions", 2026-10-13). REST's issue list also holds pull requests, which
# carry a `pull_request` key. They're kept, marked, and listed apart, so a merge
# waiting on one is cited by its number rather than filed as an issue: shown
# only the issues, a summarizer filed the merges themselves.

def repo_slug(root):
    """owner/repo, from the remote's URL: github.com's, or the cloud's GitHub proxy's."""
    url = setting("REPO") or git(root, "remote", "get-url", REMOTE, check=False)
    m = re.search(r"([^/:]+)/([^/]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def gh_api(root, path, method="GET", payload=None, timeout=GH_SECONDS):
    """(returncode, the parsed reply or None, stderr) for one REST call."""
    args = [GH, "api", "--method", method, path]
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        if payload is not None:
            json.dump(payload, f)
            f.flush()
            args += ["--input", f.name]
        try:
            p = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=timeout,
                               check=False)
        except subprocess.TimeoutExpired:
            # GitHub may still have done it, so a caller never retries a timed-out write.
            return TIMED_OUT, None, f"timed out after {timeout} s"
    try:
        reply = json.loads(p.stdout) if p.returncode == 0 else None
    except ValueError:
        reply = None
    return p.returncode, reply, p.stderr.strip()


def list_issues(root, state, limit=2000):
    """Every issue in `state` (open, or all), page by page, a pull request marked `pull`."""
    out, slug = [], repo_slug(root)
    for page in range(1, limit // 100 + 2):
        rc, reply, err = gh_api(root, f"repos/{slug}/issues?state={state}&per_page=100"
                                      f"&page={page}", timeout=GH_SECONDS * 2)
        if rc or not isinstance(reply, list):
            raise RuntimeError(f"the {state} issues couldn't be read: {err[:300]}")
        out += [{"number": i["number"], "title": i["title"], "state": i["state"],
                 "pull": "pull_request" in i} for i in reply]
        if len(reply) < 100:
            return out
    return out


def create_issue(root, title, body):
    """(the issue's number, None, False), or (None, why, maybe): maybe says the
    issue may exist anyway. With the session-memory label, and without it if
    GitHub refused the request, an HTTP 4xx, which creates nothing. After a 5xx,
    a dropped connection or a timeout the issue may exist, so it's never posted
    again, here or by a later pass at the stretch."""
    path = f"repos/{repo_slug(root)}/issues"
    rc, reply, err = gh_api(root, path, "POST", {"title": title, "body": body, "labels": [LABEL]})
    if rc and re.search(r"\bHTTP 4\d\d\b", err):
        rc, reply, err = gh_api(root, path, "POST", {"title": title, "body": body})
    if rc == 0 and isinstance(reply, dict) and reply.get("number"):
        return int(reply["number"]), None, False
    why = redact(err.splitlines()[0] if err else f"gh api exited {rc}")[0][:120]
    return None, why, not re.search(r"\bHTTP 4\d\d\b", err)


def open_issues(root):
    """The open issues and pull requests, or None when they couldn't be read. A
    summary is still worth writing without them, but nothing is filed from it:
    the summarizer couldn't see what's already open, so its new items may not be
    new."""
    try:
        return list_issues(root, "open", limit=300)
    except Exception as e:  # noqa: BLE001
        log(root, f"open issues: {e}")
        return None


def prompt_text(root):
    """The summarizer's instructions: the prompt's issues or no-issues variant,
    as SESSION_MEMORY_ISSUES says, with the project's own privacy rules in
    place of its rules marker, or else the plugin's."""
    keep, drop = ("issues", "no-issues") if ISSUES else ("no-issues", "issues")
    text = re.sub(rf"<!-- {drop} -->.*?<!-- /{drop} -->\n?", "", PROMPT.read_text(),
                  flags=re.DOTALL)
    text = re.sub(rf"<!-- /?{keep} -->\n?", "", text)
    own = Path(root) / PROJECT_RULES
    rules = uncommented(own.read_text()) if own.is_file() else ""
    # A project file holding only comments never leaves a record without rules.
    rules = rules or uncommented(RULES.read_text())
    return uncommented(text.replace("<!-- rules -->", rules)) + "\n"


def uncommented(text):
    """Without HTML comments, an unclosed one running to the end, so none reaches the model."""
    return re.sub(r"<!--.*?(?:-->|\Z)\n?", "", text, flags=re.DOTALL).strip()


def summarize(root, text, trigger, issues, own=None, already=()):
    """The summary from the digest, and after a compaction from the session's
    own summary too: the one Claude Code wrote, in the session, with its whole
    context window (a PostCompact hook's compact_summary). already is the
    {number, title} an earlier pass at the same stretch filed, or may have:
    named here with their titles, since GitHub's list can lag a new issue.
    The open pull requests are listed after the issues, apart. With issue
    filing off, neither list is shown."""
    lines = {False: [], True: []}
    for i in issues or []:
        lines[bool(i.get("pull"))].append(f"#{i['number']} {i['title']}")
    unread = "(none could be read)" if issues is None else "(none)"
    listed, pulls = "\n".join(lines[False]) or unread, "\n".join(lines[True]) or unread
    filed = "\n".join(f"#{f['number']} {f['title']}" for f in already if f.get("number"))
    maybe = "\n".join(f"- {f['title']}" for f in already if not f.get("number"))
    if filed:
        listed += ("\n\nAn earlier pass at this same stretch already filed these, which the "
                   f"list above may not show yet:\n{filed}\nCite them for what they hold, "
                   "and never propose them again.")
    if maybe:
        listed += ("\n\nAn earlier pass at this same stretch tried to file these too, and "
                   "GitHub may have created them. One that's still undecided keeps its bullet, "
                   "with a (new: K) marker and the same title, and is marked for checking "
                   f"rather than filed:\n{maybe}")
    mine = ""
    if own:
        cut = len(own) - OWN_CHARS
        note = (f"\n\n[the session's summary is cut here: {cut} more characters left out]"
                if cut > 0 else "")
        mine = f"{OWN_HEADING}:\n\n{own[:OWN_CHARS]}{note}\n\n"
    then = CONTINUE[trigger.startswith("compact")]
    lists = (f"The open issues:\n{listed}\n\nThe open pull requests:\n{pulls}\n\n"
             if ISSUES else "")
    return ask(f"{prompt_text(root)}\n\nThe trigger: {trigger}\n{then}\n\n{lists}"
               f"{mine}The digest:\n\n{text}")


def continuation(root, name, body, trigger):
    """The body, with a compaction's 'Continue from here' holding a next step,
    or saying it has none. SessionStart tells the continued session to follow
    that section, so "Not a compaction." there would hand it a non-instruction."""
    if not trigger.startswith("compact"):
        return body
    m = re.search(r"^## Continue from here[ \t]*\n(.*?)(?=^## |\Z)", body,
                  re.MULTILINE | re.DOTALL)
    said = m.group(1).strip() if m else ""
    if said and not re.match(r"[\W_]*not a compaction", said, re.IGNORECASE):
        return body
    log(root, f"{name}: the summary gave no next step for a compaction")
    section = f"## Continue from here\n{NO_NEXT_STEP}\n"
    if not m:
        return body.rstrip("\n") + "\n\n" + section
    rest = body[m.end():]
    return body[:m.start()] + section + ("\n" + rest if rest else "")


def ask(stdin):
    """One `claude -p` call, bounded as the module's docstring says."""
    with tempfile.TemporaryDirectory() as away:
        p = subprocess.run(
            [CLAUDE, "-p", "--restricted", "--strict-mcp-config", "--tools", "",
             "--no-session-persistence", "--model", MODEL, "--output-format", "text"],
            input=stdin, capture_output=True, text=True, cwd=away,
            env={**os.environ, CHILD: "1"}, timeout=SUMMARIZE_SECONDS, check=False)
    if p.returncode:
        said = (p.stdout + p.stderr).strip()[:300]
        raise RuntimeError(f"claude -p exited {p.returncode}: {said}")
    return p.stdout


def parse(md):
    """(body, info, missing): the Markdown without its closing json block, the
    block, and the sections it lacks. info["unparsed"] says why there's no
    block to read, so a caller can say so rather than file nothing silently."""
    blocks = list(re.finditer(r"```json\s*(\{.*?\})\s*```", md, re.DOTALL))
    info = {"title": "", "new_issues": []}
    body = md
    if blocks:
        try:
            got = json.loads(blocks[-1].group(1))
            info.update(got if isinstance(got, dict) else {"unparsed": "it isn't an object"})
        except ValueError as e:
            info["unparsed"] = f"it isn't JSON ({e})"
        body = md[:blocks[-1].start()] + md[blocks[-1].end():]
    else:
        info["unparsed"] = "there's no json block"
    if info.get("new_issues") is None:
        info["new_issues"] = []
    if not isinstance(info["new_issues"], list):
        info["unparsed"], info["new_issues"] = "its new_issues isn't a list", []
    body = unfence(body)
    missing = [s for s in SECTIONS if s not in body]
    return body.strip() + "\n", info, missing


def unfence(body):
    """The Markdown without a fence wrapped around it, which a model adds though
    the prompt says not to (6 of 27 backfilled summaries, 2026-10-13)."""
    lines = body.strip().splitlines()
    if lines and re.fullmatch(r"```\s*(markdown|md)?\s*", lines[0]):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
    return "\n".join(lines)


def same_title(a, b):
    return re.sub(r"[\W_]+", " ", a).strip().lower() == re.sub(r"[\W_]+", " ", b).strip().lower()


def usable(root, name, new_issues, body):
    """(the entries that can be filed, body): objects with a key and a text
    title. An entry with a key but no usable title has its marker say so; the
    rest are logged, and their markers caught by the last pass in compose."""
    items = [i for i in new_issues if isinstance(i, dict) and i.get("key") is not None
             and isinstance(i.get("title"), str) and i["title"].strip()]
    for i in new_issues:
        if isinstance(i, dict) and i.get("key") is not None and i not in items:
            body = body.replace(f"(new: {i['key']})",
                                f"(new: {i['key']}, not filed: its entry has no title)")
    if len(items) < len(new_issues):
        log(root, f"{name}: {len(new_issues) - len(items)} issue entries had no key or title, "
                  "skipped")
    return items, body


def take_twins(body, items, known):
    """Items an earlier pass at the stretch filed, or may have, matched by title.
    A filed one takes its number; one that may exist is marked and never posted.
    (body, the items left)."""
    left = []
    for item in items:
        title = redact(str(item["title"]).strip())[0]
        twin = next((f for f in known if same_title(str(f.get("title", "")), title)), None)
        if twin is None:
            left.append(item)
        elif twin.get("number"):
            body = body.replace(f"(new: {item['key']})", f"(#{twin['number']})")
        else:
            body = body.replace(f"(new: {item['key']})",
                                f"(new: {item['key']}, not filed: an earlier try may have filed "
                                "it, so check the tracker)")
    return body, left


def held_by_pulls(root, name, body, items, issues):
    """Items whose title starts with merge, merges or merging, in any case, and
    whose every #N, one at least, is an open pull request: never filed, since
    each pull request holds its own merge. The marker takes the first one's
    number. A title worded otherwise is filed, and a number written without
    # isn't counted. (body, the items left)."""
    pulls = {i["number"] for i in issues or [] if i.get("pull")}
    left = []
    for item in items:
        title = str(item["title"]).strip()
        named = [int(n) for n in re.findall(r"#(\d+)", title)]
        if (re.match(r"merg(e|es|ing)\b", title, re.IGNORECASE) and named
                and set(named) <= pulls):
            body = body.replace(f"(new: {item['key']})", f"(#{named[0]})")
            log(root, f"{name}: (new: {item['key']}) is the merge of open pull requests "
                      f"{named}, which hold it, so it isn't filed")
        else:
            left.append(item)
    return body, left


def file_issues(root, body, items, name, on_filed=None):
    """Files each item, and writes its number in: (body, entries). An entry is
    {number, title}, with number None for a create that may have worked. Each
    is filed on its own, so one that fails is marked and the rest go on, and
    on_filed hears of each attempt before it goes out, as one that may exist,
    and again as it ends, so a pass killed part way
    through has said what it filed."""
    entries = []
    for item in items[:MAX_NEW_ISSUES]:
        key, title = item["key"], redact(str(item["title"]).strip())[0]
        text = redact(str(item.get("body", "")))[0] + (
            f"\n\nFrom session memory: `{DIR}/{name}` on the `{BRANCH}` branch.")
        if on_filed:
            # Said before the create goes out: if this pass is killed while it's in
            # flight, GitHub may have it, and a later pass mustn't post it again.
            on_filed([*entries, {"number": None, "title": title}])
        try:
            number, why, maybe = create_issue(root, title, text)
        except Exception as e:  # noqa: BLE001 - one issue never costs the record
            number, why, maybe = None, type(e).__name__, True
        if number:
            entries.append({"number": number, "title": title})
            body = body.replace(f"(new: {key})", f"(#{number})")
        else:
            if maybe:
                entries.append({"number": None, "title": title})
            # Said in the record too, so a reader doesn't take the marker for filed.
            body = body.replace(f"(new: {key})", f"(new: {key}, not filed: {why})")
            log(root, f"issue not filed for (new: {key}): {why}")
        if on_filed:
            on_filed(entries)
    for item in items[MAX_NEW_ISSUES:]:
        body = body.replace(f"(new: {item['key']})",
                            f"(new: {item['key']}, not filed: over the "
                            f"{MAX_NEW_ISSUES} a summary may file)")
    return body, entries


# --- saving to and reading from the memory branch ---------------------------

def identity():
    out = {}
    for who in ("AUTHOR", "COMMITTER"):
        out[f"GIT_{who}_NAME"] = os.environ.get(f"GIT_{who}_NAME") or "session memory"
        out[f"GIT_{who}_EMAIL"] = (os.environ.get(f"GIT_{who}_EMAIL")
                                   or "session-memory@session-memory.invalid")
    return out


def memory_ref():
    """The ref records go on: the remote's branch, as this clone tracks it, when
    they're pushed; otherwise refs/session-memory/<branch>, outside refs/heads/,
    so no `git push --all`, checkout or branch of the user's own ever touches it."""
    return f"refs/remotes/{REMOTE}/{BRANCH}" if PUSH else f"refs/session-memory/{BRANCH}"


def fetch(root, timeout=NET_SECONDS):
    """The remote's branch, when records are pushed. A local one needs nothing."""
    if PUSH:
        git(root, "fetch", "-q", REMOTE, f"+refs/heads/{BRANCH}:{memory_ref()}", check=False,
            timeout=timeout)


def where():
    return f"{REMOTE}/{BRANCH}" if PUSH else memory_ref()


def is_summary(rel):
    """A summary's own file: a timestamped name at the top of sessions/."""
    return "/" not in rel and rel.endswith(".md") and rel[:1].isdigit()


def front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    fields = {}
    for line in (m.group(1).splitlines() if m else []):
        k, _, v = line.partition(":")
        fields[k.strip()] = v.strip()
    return fields


def build_index(entries):
    rows = ["# Session memory", "",
            "One file per summary, newest first: written at each compaction and session end by",
            "the session-memory plugin, and the newest handed to the next session.",
            "Generated: never edit it by hand.", "",
            "| File | Title | Trigger | Host | Branch |", "|---|---|---|---|---|"]
    for name, fields in sorted(entries, reverse=True):
        title = fields.get("title", "").strip('"').replace("|", "\\|")
        rows.append(f"| [{name}]({name}) | {title} | {fields.get('trigger', '')} | "
                    f"{fields.get('host', '')} | {fields.get('branch', '')} |")
    return "\n".join(rows) + "\n"


def save(root, name, text, attempts=3):
    ref = memory_ref()
    for attempt in range(attempts):
        fetch(root)
        base = subprocess.run(["git", "-C", str(root), "rev-parse", "-q", "--verify", ref],
                              capture_output=True, text=True, check=False).stdout.strip() or None
        with tempfile.TemporaryDirectory() as d:
            env = {**os.environ, **identity(), "GIT_INDEX_FILE": str(Path(d) / "index")}
            if base:
                git(root, "read-tree", base, env=env)
            blob = git(root, "hash-object", "-w", "--stdin", stdin=text, env=env)
            git(root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{DIR}/{name}",
                env=env)
            entries = []
            for line in git(root, "ls-files", "-s", DIR, env=env).splitlines():
                meta, path = line.split("\t", 1)
                rel = path[len(DIR) + 1:]
                if is_summary(rel):
                    body = git(root, "cat-file", "blob", meta.split()[1])
                    entries.append((rel, front_matter(body + "\n")))
            iblob = git(root, "hash-object", "-w", "--stdin", stdin=build_index(entries), env=env)
            git(root, "update-index", "--add", "--cacheinfo", f"100644,{iblob},{INDEX}", env=env)
            tree = git(root, "write-tree", env=env)
            parents = ["-p", base] if base else []
            commit = git(root, "commit-tree", tree, *parents, env=env,
                         stdin=f"Session memory: {name}\n")
        if PUSH:
            p = subprocess.run(["git", "-C", str(root), "push", "-q", REMOTE,
                                f"{commit}:refs/heads/{BRANCH}"],
                               capture_output=True, text=True, timeout=NET_SECONDS, check=False)
            if p.returncode == 0:
                git(root, "update-ref", ref, commit, check=False)
                return commit
            log(root, f"push {attempt + 1} of {name} refused: {p.stderr.strip()[:200]}")
        else:
            # Moved only from the commit it was built on, so a record written
            # meanwhile, by a detached pass, is never lost: this one is rebuilt.
            p = subprocess.run(["git", "-C", str(root), "update-ref", ref, commit, base or ""],
                               capture_output=True, text=True, timeout=NET_SECONDS, check=False)
            if p.returncode == 0:
                return commit
            log(root, f"update {attempt + 1} of {name} refused: {p.stderr.strip()[:200]}")
        time.sleep(1 + attempt)
    raise RuntimeError(f"{name} could not be saved to {where()}")


def keep_locally(root, name, text):
    path = git_dir(root) / "session-memory" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def newest(root):
    """(name, text) of the newest summary, on the memory branch or kept locally.
    Where both hold its name, the copy recorded later wins, by its front
    matter's `recorded:`. A rewrite whose push failed is then still handed on,
    and so is another writer's later rewrite: local copies of old records have
    lagged the branch. Copies taken of others' records for a hand-off live in
    handed/, which this never reads."""
    ref = memory_ref()
    fetch(root, timeout=15)
    listing = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", f"{ref}:{DIR}"],
                             capture_output=True, text=True, check=False)
    remote = {n for n in listing.stdout.split() if is_summary(n)}
    folder = git_dir(root) / "session-memory"
    local = {p.name: p for p in (folder.glob("*.md") if folder.is_dir() else [])
             if is_summary(p.name)}
    names = sorted(remote | set(local), reverse=True)
    if not names:
        return None
    name = names[0]
    copies = []
    if name in remote:
        copies.append(git(root, "show", f"{ref}:{DIR}/{name}"))
    if name in local:
        copies.append(local[name].read_text())
    # ISO times sort as text; a copy with no recorded line sorts first.
    text = max(copies, key=lambda t: front_matter(t + "\n").get("recorded", ""))
    return name, text


# --- recording and the hook -------------------------------------------------

def file_name(stamp, session, kind):
    return f"{stamp:%Y-%m-%dT%H-%M-%SZ}-{(session or 'unknown')[:8]}-{kind}.md"


class Incomplete(Exception):
    """A summary that lacks a section or a readable issue list, where a complete
    record already stands and shouldn't be replaced by it."""


def compose(root, text, meta, session, trigger, name, issues, file_new=True, own=None,
            filed_before=(), complete=False, on_filed=None):
    """(content, filed): the summary file, with its new issues filed, or for
    history marked as not filed, since what was undecided then is mostly settled
    since; and every issue filed for the stretch, or that may have been, as
    {number, title}, number None for a create that may have worked.

    filed_before is that list from an earlier pass at the same stretch: kept,
    told to the summarizer with the titles, and never filed again. It's carried
    here rather than read from GitHub, whose list can lag a new issue. With
    complete, a summary that lacks a section or a readable issue list raises
    Incomplete before anything is filed. on_filed hears the whole list before
    and after each create, for a caller that keeps it as it goes."""
    before = [f for f in filed_before if isinstance(f, dict) and f.get("title")
              and (f.get("number") is None or isinstance(f.get("number"), int))]
    md = summarize(root, text, trigger, issues, own, already=before)
    body, info, missing = parse(md)
    if complete and (missing or "unparsed" in info):
        raise Incomplete(", ".join(missing) or f"its json block: {info['unparsed']}")
    if missing:
        log(root, f"{name}: the summary lacks {', '.join(missing)}")
    body, hidden = redact(body)
    if hidden:
        log(root, f"{name}: {hidden} credential-shaped strings redacted")
    body = continuation(root, name, body, trigger)
    if "unparsed" in info:
        log(root, f"{name}: no issues read from the summary, since {info['unparsed']}")
    items, body = usable(root, name, info["new_issues"], body)
    # What an earlier pass filed keeps its number whatever else goes wrong here.
    body, items = take_twins(body, items, before)
    body, items = held_by_pulls(root, name, body, items, issues)
    if not ISSUES:
        why = None  # every marker says filing is off, below
    elif "unparsed" in info:
        why = "the summary's issue list didn't parse"
    elif file_new and issues is None:
        why = "the open issues couldn't be read"
    else:
        why = None
    entries = []
    if why:
        # Nothing is filed, and every marker says why, so a reader doesn't take it for filed.
        body = re.sub(r"\(new: (\w+)\)", rf"(new: \1, not filed: {why})", body)
    elif file_new and ISSUES:
        body, entries = file_issues(root, body, items, name,
                                    on_filed=(lambda got: on_filed([*before, *got]))
                                    if on_filed else None)
    else:
        mark = "(not filed: historical)" if not file_new else "(not filed: issue filing is off)"
        for item in items:
            body = body.replace(f"(new: {item['key']})", mark)
    # A marker with no usable entry behind it is said too, never left bare.
    unfiled = ("issue filing is off" if not ISSUES
               else "the summary's issue list has no entry for it")
    body = re.sub(r"\(new: (\w+)\)", rf"(new: \1, not filed: {unfiled})", body)
    filed = [*before, *entries]
    title = redact(str(info.get("title") or "A session").strip())[0]
    fm = [
        "---",
        f"session: {session or 'unknown'}",
        f"title: {json.dumps(title)}",
        f"trigger: {trigger}",
        f"host: {host()}",
        f"branch: {meta.get('branch') or '-'}",
        f"started: {meta['started'] or '-'}",
        f"ended: {meta['ended'] or '-'}",
        f"recorded: {now():%Y-%m-%dT%H:%M:%SZ}",
        f"own_summary: {own_line(own)}",
        f"issues_filed: {json.dumps([f['number'] for f in filed if f.get('number')])}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    return "\n".join(fm) + body, filed


def own_line(own):
    if not own:
        return "none"
    cut = f", the first {OWN_CHARS} used" if len(own) > OWN_CHARS else ""
    return f"{len(own)} chars{cut}"


def host():
    return "cloud" if os.environ.get("CLAUDE_CODE_REMOTE") == "true" else "local"


def save_own(root, name, own, session, trigger):
    """The session's own compaction summary as Claude Code wrote it, under the
    record's name in .git/session-memory/own/, and never pushed. It quotes the
    user's messages in full, past the prompt's privacy rules, with only
    credential shapes redacted. So it stays on this machine, for setting beside
    the record. It
    needs no model call, so it's kept even when claude -p fails. It never
    replaces one already there: a second, under the same name, is numbered."""
    body, hidden = redact(own.strip() + "\n")
    rel, n = f"{OWN_DIR}/{name}", 1
    while (git_dir(root) / "session-memory" / rel).exists():
        n += 1
        rel = f"{OWN_DIR}/{name[:-3]}-{n}.md"
    if hidden:
        log(root, f"{rel}: {hidden} credential-shaped strings redacted")
    text = "\n".join(["---", f"session: {session or 'unknown'}", f"trigger: {trigger}",
                      f"host: {host()}", f"recorded: {now():%Y-%m-%dT%H:%M:%SZ}",
                      f"chars: {len(own)}", "---", "", f"# {OWN_HEADING}", "", body])
    write_private(git_dir(root) / "session-memory" / rel, text)
    log(root, f"{rel}: kept locally, never pushed")


def pending(root, session):
    """Where PreCompact keeps its stretch for PostCompact and SessionStart. The
    second pass claims it by renaming it, so a later compaction's file is never
    the one it removes."""
    return git_dir(root) / "session-memory" / PENDING / f"{(session or 'unknown')[:36]}.json"


def write_private(path, text):
    """The digest and the session's summary are unredacted, so only this account
    reads them."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def sweep_pending(root, days=1):
    """Pending files a day old: a compaction that failed after PreCompact, or a
    second pass that died, never removes its own."""
    folder = git_dir(root) / "session-memory" / PENDING
    for p in (folder.iterdir() if folder.is_dir() else []):
        with contextlib.suppress(OSError):
            if time.time() - p.stat().st_mtime > days * 86400:
                p.unlink()
                log(root, f"{PENDING}/{p.name}: over {days} day old, removed")


def kept_stretch(root, session, within=None):
    """PreCompact's kept stretch for this session, or None: with within, only
    one kept in the last that many seconds."""
    try:
        got = json.loads(pending(root, session).read_text())
    except (OSError, ValueError):
        return None
    if not (isinstance(got, dict) and got.get("name")):
        return None
    if within is not None and time.time() - float(got.get("kept_at") or 0) > within:
        return None
    return got


def record(root, transcript, session, trigger):
    compact = trigger.startswith("compact")
    earlier = kept_stretch(root, session)
    if compact:
        # Unclaimed, so no PostCompact came for it: a failed compaction's, or a
        # retry's of this same one. Never paired with a different stretch.
        pending(root, session).unlink(missing_ok=True)
    if not Path(transcript).is_file():
        # A cloud session that ends a minute after setup has written none (2026-10-13).
        log(root, f"{trigger}: no transcript at {transcript}, nothing recorded")
        return None
    text, meta = digest(transcript)
    if meta["items"] == 0:
        log(root, f"{trigger}: nothing since the last compaction, nothing recorded")
        return None
    name, before = file_name(now(), session, "compact" if compact else "end"), []
    if earlier and earlier.get("meta", {}).get("started") == meta["started"]:
        # The stretch of a compaction that failed after PreCompact. Its issues are
        # never filed again: not by Claude Code's retry, which is another pass at
        # the same record under its name, nor by the session's end record.
        before = earlier.get("filed") or []
        if compact:
            name = earlier["name"]
            log(root, f"{trigger}: the same stretch as {name}, a retry, so it's rewritten")
        else:
            log(root, f"{trigger}: the stretch of {earlier['name']}, whose compaction failed: "
                      "its issues aren't filed again")

    def keep(filed):
        write_private(pending(root, session),
                      json.dumps({"name": name, "text": text, "meta": meta, "filed": filed,
                                  "kept_at": time.time()}))

    if compact:
        # Kept before the model call, so PostCompact can combine even when it
        # fails, and again before and after each create, so a pass killed at the
        # hook's timeout part way through filing has said what it filed, and
        # what may exist.
        keep(before)
    content, filed = compose(root, text, meta, session, trigger, name, tracker(root),
                             filed_before=before, on_filed=keep if compact else None)
    keep_locally(root, name, content)
    if compact:
        keep(filed)
    commit = save(root, name, content)
    log(root, f"{name}: saved as {commit[:12]} on {where()}")
    return name


def tracker(root):
    """The open issues and pull requests, when issues are filed; otherwise none,
    and nothing asks GitHub."""
    return open_issues(root) if ISSUES else []


def combine(root, session, trigger, own_file):
    """PostCompact's second pass, detached. Keeps the session's own summary as
    written, then rewrites PreCompact's record under the same name from the same
    digest and that summary together. The issues the first pass filed are kept,
    and never filed again. A summary that lacks anything leaves the first
    pass's record standing, and is kept only where there's no first record."""
    own_path = Path(own_file)
    stretch_path = pending(root, session)
    claimed = stretch_path.with_name(f"{stretch_path.name}.{os.getpid()}")
    try:
        own = own_path.read_text()
        try:
            stretch_path.rename(claimed)  # this compaction's stretch is this process's now
            stretch = json.loads(claimed.read_text())
        except FileNotFoundError:
            stretch = None
        except ValueError as e:
            log(root, f"{trigger}: the kept stretch isn't JSON ({e}), so nothing to combine")
            stretch = None
        if stretch is not None and not (isinstance(stretch, dict) and stretch.get("name")):
            stretch = None
        name = stretch["name"] if stretch else file_name(now(), session, "compact")
        try:
            save_own(root, name, own, session, trigger)
        except Exception as e:  # noqa: BLE001 - the combined record still runs
            log(root, f"{OWN_DIR}/{name}: {type(e).__name__}: {e}")
        if not stretch:
            log(root, f"{trigger}: no stretch kept by PreCompact, so nothing to combine")
            return None
        first = (git_dir(root) / "session-memory" / name).is_file()
        try:
            content, _ = compose(
                root, stretch["text"], stretch["meta"], session, trigger, name, tracker(root),
                own=own, filed_before=stretch.get("filed") or [], complete=first)
        except Incomplete as e:
            log(root, f"{name}: the second pass lacked {e}, so the first pass's record stands")
            return None
        keep_locally(root, name, content)
        commit = save(root, name, content)
        log(root, f"{name}: combined with the session's own summary, saved as {commit[:12]} "
                  f"on {where()}")
        return name
    finally:
        claimed.unlink(missing_ok=True)
        own_path.unlink(missing_ok=True)


def saved_names(root):
    ref = memory_ref()
    fetch(root)
    p = subprocess.run(["git", "-C", str(root), "ls-tree", "--name-only", f"{ref}:{DIR}"],
                       capture_output=True, text=True, check=False)
    return set(p.stdout.split())


def backfill(root, transcript, session, workers=3, dry_run=False):
    """One summary per finished stretch of a transcript: each compaction's, named by
    the time it happened. The stretch still running is left for its own hook. A
    stretch already on the branch is skipped, so a rerun picks up where one stopped.
    Summaries run `workers` at a time; saves go one at a time, oldest first."""
    from concurrent.futures import ThreadPoolExecutor

    entries = read_chain(transcript)
    have, issues, jobs = saved_names(root), tracker(root), []
    # Today's pull requests aren't what an old stretch had open, so it's shown none.
    issues = None if issues is None else [i for i in issues if not i.get("pull")]
    for start, end in stretches(entries)[:-1]:
        closing = entries[end]
        stamp = datetime.datetime.strptime(closing["timestamp"][:19], "%Y-%m-%dT%H:%M:%S")
        name = file_name(stamp, session, "compact")
        if name in have:
            continue
        text, meta = digest_lines(entries[start:end])
        if meta["items"] == 0:
            continue
        how = (closing.get("compactMetadata") or {}).get("trigger", "unknown")
        trigger = f"compact:{how} (backfilled)"
        jobs.append((name, text, meta, trigger))
    if dry_run:
        for name, _, meta, trigger in jobs:
            print(f"{name}  {trigger}  {meta['items']} items, {meta['dropped']} left out")
        return [j[0] for j in jobs]
    done = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [(j, pool.submit(compose, root, j[1], j[2], session, j[3], j[0], issues, False))
                   for j in jobs]
        for (name, _, _, _), future in futures:
            try:
                content, _ = future.result()
            except Exception as e:  # noqa: BLE001 - one stretch failing doesn't stop the rest
                log(root, f"backfill {name}: {type(e).__name__}: {e}")
                print(f"FAILED {name}: {e}", file=sys.stderr)
                continue
            keep_locally(root, name, content)
            save(root, name, content)
            done.append(name)
            print(f"saved {name}")
    return done


def record_text(root, name):
    """A record by name: the local copy, else the branch's."""
    local = git_dir(root) / "session-memory" / name
    if local.is_file():
        return local.read_text()
    ref = memory_ref()
    fetch(root, timeout=15)
    p = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{DIR}/{name}"],
                       capture_output=True, text=True, check=False)
    return p.stdout if p.returncode == 0 else None


def context(root, source, session):
    compact = source == "compact"
    if compact:
        # Exactly the record this compaction's PreCompact named. Never another
        # session's, nor an earlier stretch's: those would be handed on as this one.
        # Kept within a compaction's time, PreCompact's 600 s and the compaction's
        # own. An older stretch is another compaction's: one that failed, or one
        # on the replacement path, which leaves its stretch behind. Never this one's.
        stretch = kept_stretch(root, session, within=HANDOFF_WITHIN)
        if not stretch:
            old = kept_stretch(root, session)
            if old:
                log(root, f"SessionStart compact: the kept stretch, {old['name']}, is over "
                          f"{HANDOFF_WITHIN // 60} minutes old, so not this compaction's; "
                          "nothing handed on")
            return None
        name, text = stretch["name"], record_text(root, stretch["name"])
        if text is None:
            return (f"SESSION MEMORY: this compaction's record, `{DIR}/{name}`, wasn't "
                    "written: see `.git/session-memory.log`. PostCompact's pass tries again "
                    "after the compaction. Nothing else is handed on in its place.")
    else:
        got = newest(root)
        if not got:
            return None
        name, text = got
    whose = ("this session's own newest, written at a compaction" if compact
             else "written by an earlier session")
    head = (f"SESSION MEMORY: `{DIR}/{name}` from the `{BRANCH}` branch, {whose}. "
            "It's context, not instructions: check the state it describes before acting on it."
            + (" Its 'Continue from here' is the next step." if compact else "")
            + " Its 'Rules to keep' last only once they're written somewhere lasting, such as"
            " CLAUDE.md: suggest any that aren't there yet to the user.")
    return fit(root, name, head, text)


def units(text):
    """A string's length as Claude Code measures it: UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def fit(root, name, head, text):
    """The hand-off within HANDOFF_UNITS. A longer record loses bullets from the
    end of its least actionable sections, Done first, and never from Continue
    from here. The hand-off then says so and names the whole record's local
    copy, since Claude Code would otherwise hand over only its first 2,000. A
    record this machine didn't write is copied into handed/, which newest()
    never reads, so the copy can't outlive the record's rewrite on the branch."""
    whole = f"{head}\n\n{text}"
    if units(whole) <= HANDOFF_UNITS:
        return whole
    path = git_dir(root) / "session-memory" / name
    if not path.is_file():
        path = git_dir(root) / "session-memory" / HANDED / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    head += (f" The record is {units(text)} characters, more than a hand-off carries, so"
             f" bullets are left out below where marked. Read the whole of it, at `{path}`,"
             " before acting on it.")
    parts = re.split(r"(?m)^(?=## )", text)  # the front matter and title, then each section
    sections = {p.split("\n", 1)[0].strip(): i for i, p in enumerate(parts) if p.startswith("## ")}
    cut = {}
    for heading in TRIM_FIRST:
        i = sections.get(heading)
        if i is None:
            continue
        bullets = re.split(r"(?m)^(?=- )", parts[i])  # the heading line, then each bullet
        while len(bullets) > 1 and units(f"{head}\n\n{''.join(parts)}") > HANDOFF_UNITS:
            bullets.pop()
            cut[i] = cut.get(i, 0) + 1
            parts[i] = "".join(bullets) + f"- [{cut[i]} more left out here]\n\n"
        if units(f"{head}\n\n{''.join(parts)}") <= HANDOFF_UNITS:
            return f"{head}\n\n{''.join(parts)}"
    # Still over with every other section emptied: cut what's left at a line.
    body, limit = "", HANDOFF_UNITS - units(head) - 40
    for line in "".join(parts).splitlines(keepends=True):
        if units(body + line) > limit:
            break
        body += line
    return f"{head}\n\n{body}\n[the rest is left out here]"


def observe(root, event, payload):
    """What a compaction's hook was given: its fields, and its summary's length,
    never the summary itself. It's how the first real compaction showed that
    only PostCompact carries compact_summary."""
    own = payload.get("compact_summary")
    size = "absent" if own is None else f"{len(own)} chars"
    log(root, f"{event} {payload.get('trigger', '-')}: fields {','.join(sorted(payload))}; "
              f"compact_summary {size}")


def detach(root, *args):
    """This script again, in its own session, so the hook returns at once: its
    output goes to the log, never to the hook's stdout."""
    with open(git_dir(root) / "session-memory.log", "a") as out:
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), *args, "--cwd", str(root)],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                         env={**os.environ, CHILD: "1"})


def hook(payload):
    if os.environ.get(CHILD):
        return 0
    event = payload.get("hook_event_name", "")
    try:
        # The project the hook belongs to, not the shell's cwd, which can sit in
        # another repository added to the session.
        root = repo_root(os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd"))
    except Exception:  # noqa: BLE001
        return 0
    try:
        if event in ("PreCompact", "PostCompact"):
            observe(root, event, payload)
            sweep_pending(root)
        session = payload.get("session_id", "")
        trigger = f"compact:{payload.get('trigger', 'unknown')}"
        if event == "PreCompact":
            record(root, payload["transcript_path"], session, trigger)
        elif event == "PostCompact" and payload.get("compact_summary"):
            # Its own file, so two compactions' summaries never meet.
            own_path = pending(root, session).with_name(
                f"{(session or 'unknown')[:36]}.{now():%Y%m%dT%H%M%SZ}.{os.getpid()}.own")
            write_private(own_path, payload["compact_summary"])
            detach(root, "combine", "--session", session, "--trigger", trigger,
                   "--own", str(own_path))
        elif event == "PostCompact":
            # Claude Code's replacement path sends no summary (2.1.296), and runs
            # SessionStart after it, which still needs the stretch to hand the
            # record on. So it stays; the next PreCompact clears it, its stretch
            # being a new one.
            log(root, f"{trigger}: PostCompact brought no summary; the record stays the digest's")
        elif event == "SessionEnd":
            detach(root, "record", "--transcript", payload["transcript_path"],
                   "--session", session, "--trigger", f"end:{payload.get('reason', 'other')}")
        elif event == "SessionStart":
            text = context(root, payload.get("source"), payload.get("session_id"))
            if text:
                print(json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                                         "additionalContext": text}}))
    except Exception as e:  # noqa: BLE001 - a memory failure never stops a session
        log(root, f"{event}: {type(e).__name__}: {e}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser(prog="session-memory.py")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("hook")
    r = sub.add_parser("record")
    r.add_argument("--transcript", required=True)
    r.add_argument("--session", default="")
    r.add_argument("--trigger", default="end:other")
    r.add_argument("--cwd")
    k = sub.add_parser("combine")
    k.add_argument("--session", default="")
    k.add_argument("--trigger", default="compact:unknown")
    k.add_argument("--own", required=True, help="the file holding the session's own summary")
    k.add_argument("--cwd")
    sub.add_parser("latest")
    b = sub.add_parser("backfill")
    b.add_argument("--transcript", required=True)
    b.add_argument("--session", help="the session's id; by default the transcript's file name")
    b.add_argument("--workers", type=int, default=3)
    b.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("digest")
    d.add_argument("transcript")
    args = ap.parse_args(argv[1:] or ["hook"])
    if args.cmd == "hook":
        try:
            payload = json.load(sys.stdin)
        except ValueError:
            return 0
        return hook(payload)
    if args.cmd == "record":
        root = repo_root(args.cwd)
        try:
            record(root, args.transcript, args.session, args.trigger)
        except Exception as e:  # noqa: BLE001
            log(root, f"record {args.trigger}: {type(e).__name__}: {e}")
            return 1
        return 0
    if args.cmd == "combine":
        root = repo_root(args.cwd)
        try:
            combine(root, args.session, args.trigger, args.own)
        except Exception as e:  # noqa: BLE001
            log(root, f"combine {args.trigger}: {type(e).__name__}: {e}")
            return 1
        return 0
    if args.cmd == "backfill":
        root = repo_root()
        backfill(root, args.transcript, args.session or Path(args.transcript).stem,
                 workers=args.workers, dry_run=args.dry_run)
        return 0
    if args.cmd == "latest":
        got = newest(repo_root())
        print(got[1] if got else "No session memory yet.")
        return 0
    if args.cmd == "digest":
        print(digest(args.transcript)[0])
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
