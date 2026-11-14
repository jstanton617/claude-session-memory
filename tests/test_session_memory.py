"""Session memory: a summary on the memory branch at each compaction and
session end, and the newest handed to the next session.

The plugin's scripts/session-memory.py runs here against a synthetic transcript
built in the shapes a real one has: a resumed session's copies of earlier
entries, a compaction's summary stamped before its boundary and a few entries
under it, a sidechain, task notices, a message sent while a turn ran, and an
app's notice. `claude` and `gh` are stubs that record what they're given; the
remote is a bare repository in tmp_path. Nothing reaches a model, GitHub or a
real repository.

The `mem` fixture turns pushing and issue filing on, so most tests exercise
the whole path; the tests near the end check the defaults, with both off.

THE STUBS ARE ASSUMPTIONS about `claude -p` and `gh`. The evidence is a real
compaction and a real session end leaving a record on the memory branch.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/session-memory"
HOOK = PLUGIN / "scripts/session-memory.py"
PROMPT = PLUGIN / "scripts/session-memory-prompt.md"
RULES = PLUGIN / "scripts/session-memory-rules.md"
HOOKS = PLUGIN / "hooks/hooks.json"
SESSION = "abcdef12-0000-4000-8000-000000000000"

SUMMARY = """```markdown
## Done
- Built the probe (#3).

## In progress
- The docs.

## Decided
- "Option 4 for now" (#4).

## Rules to keep
- "Always run the tests before committing", because others build on each commit.

## Not decided
- Whether to keep it (#5)
- Whether to split it (new: 1)
- Whether to rename it

## Continue from here
- Run the tests next.
```

```json
{"title": "Built the probe",
 "new_issues": [{"key": 1, "title": "Decide the split", "body": "It is open."}]}
```
"""

CLAUDE = """#!/bin/bash
printf '%s\\n' "$@" > "$STUB/claude-argv"
pwd > "$STUB/claude-cwd"
echo "${SESSION_MEMORY_CHILD:-unset}" > "$STUB/claude-child"
cat > "$STUB/claude-stdin"
{ cat "$STUB/claude-stdin"; echo "$CALL_SEPARATOR"; } >> "$STUB/claude-stdins"
sleep "${STUB_SLEEP:-0}"
[ "${STUB_CLAUDE_RC:-0}" = 0 ] || { echo "stub failure"; exit "$STUB_CLAUDE_RC"; }
n=$(( $(cat "$STUB/claude-calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$STUB/claude-calls"
if [ -n "$STUB_FAIL_FIRST" ] && [ ! -e "$STUB/failed-once" ]; then
  touch "$STUB/failed-once"; echo "stub failure"; exit 1
fi
reply="$STUB/summary"; [ "$n" -ge 2 ] && [ -e "$STUB/summary2" ] && reply="$STUB/summary2"
cat "$reply"
"""
CALL_SEPARATOR = "=== the next claude -p call ==="

# GitHub's REST API, as `gh api` serves it. `gh issue` is GraphQL, which a cloud
# session refuses with this 403 (2026-10-13), so the stub refuses it too. The
# issue lists hold a pull request, as REST's do. Each issue created is a line
# of its JSON in gh-created. An HTTP error reads as gh 2.97 prints one on
# stderr, exiting 1: "gh: Not Found (HTTP 404)" (2026-10-13).
GH = """#!/bin/bash
printf '%s\n' "$*" | tr '\n' ' ' >> "$STUB/gh-calls"; echo >> "$STUB/gh-calls"
if [ "$1" != api ]; then
  echo "HTTP 403: GitHub GraphQL is not available from Claude Code sessions; use the REST API" >&2
  exit 1
fi
input=; prev=
for a in "$@"; do [ "$prev" = --input ] && input=$a; prev=$a; done
case " $* " in
  *" POST "*)
    tr -d '\n' < "$input" >> "$STUB/gh-created"; echo >> "$STUB/gh-created"
    [ -n "$STUB_CREATE_FAILS" ] && { echo "gh: Validation Failed (HTTP 422)" >&2; exit 1; }
    [ -n "$STUB_CREATE_5XX" ] && { echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1; }
    if [ -n "$STUB_FAIL_FIRST_CREATE" ] && [ ! -e "$STUB/create-failed-once" ]; then
      touch "$STUB/create-failed-once"; echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1
    fi
    [ -n "$STUB_CREATE_HANGS" ] && sleep 5
    [ -n "$STUB_HANG_SECOND" ] && [ "$(grep -c . "$STUB/gh-created")" -ge 2 ] && sleep 20
    if [ -n "$STUB_NO_LABEL" ] && grep -q '"labels"' "$input"; then
      echo "gh: Validation Failed (HTTP 422)" >&2; exit 1
    fi
    n=$(( $(cat "$STUB/gh-number" 2>/dev/null || echo 76) + 1 )); echo "$n" > "$STUB/gh-number"
    echo "{\\"number\\": $n, \\"html_url\\": \\"https://github.com/o/r/issues/$n\\"}"; exit 0 ;;
  *"&page=1 "*)
    [ -n "$STUB_LIST_FAILS" ] && { echo "gh: Internal Server Error (HTTP 500)" >&2; exit 1; }
    pr='{"number": 9, "title": "A pull request", "state": "open", "pull_request": {}}'
    case " $* " in
      *"state=all"*) echo "[{\\"number\\": 5, \\"title\\": \\"Keep it\\", \\"state\\": \\"open\\"},
        {\\"number\\": 6, \\"title\\": \\"Old\\", \\"state\\": \\"closed\\"}, $pr]" ;;
      *) echo "[{\\"number\\": 5, \\"title\\": \\"Keep it\\", \\"state\\": \\"open\\"}, $pr]" ;;
    esac
    exit 0 ;;
  *) echo '[]'; exit 0 ;;
esac
"""


def entry(uuid, ts, kind, parent=None, **kw):
    return {"uuid": uuid, "timestamp": ts, "type": kind, "parentUuid": parent,
            "sessionId": SESSION, "gitBranch": kw.pop("branch", "feature-x"), **kw}


def user(uuid, ts, text, parent=None, **kw):
    return entry(uuid, ts, "user", parent, message={"role": "user", "content": text}, **kw)


def transcript_lines():
    """Two compactions; the stretch after the second is the live one."""
    t = "2026-10-13T10:00:"
    e = [
        user("u0", t + "00.000Z", "OLD MESSAGE before the first compaction"),
        entry("b1", t + "10.000Z", "system", subtype="compact_boundary",
              compactMetadata={"trigger": "manual"}, logicalParentUuid="u0"),
        entry("a1", t + "09.900Z", "attachment", "b1", attachment={
            "type": "queued_command", "prompt": "<task-notification>done</task-notification>",
            "origin": {"kind": "task-notification"}}),
        user("s1", t + "09.500Z", "SUMMARY ONE of what came before", "a1", isCompactSummary=True),
        user("u1", t + "11.000Z", "FIRST STRETCH request", "s1"),
        entry("b2", t + "20.000Z", "system", subtype="compact_boundary",
              compactMetadata={"trigger": "auto"}, logicalParentUuid="u1"),
        user("s2", t + "19.500Z", "SUMMARY TWO of what came before", "b2", isCompactSummary=True),
        user("u2", t + "21.000Z",
             "please add the probe <system-reminder>NOISE</system-reminder>", "s2"),
        entry("x2", t + "22.000Z", "assistant", "u2", message={"role": "assistant", "content": [
            {"type": "text", "text": "Adding it."},
            {"type": "tool_use", "name": "Bash", "input": {"command": "make test",
                                                           "description": "Run the tests"}}]}),
        entry("r2", t + "23.000Z", "user", "x2", message={"role": "user", "content": [
            {"type": "tool_result", "content": "RESULT-BLOB " * 50}]}),
        entry("q2", t + "24.000Z", "attachment", "r2", attachment={
            "type": "queued_command", "prompt": "a question while it ran",
            "origin": {"kind": "human"}}),
        entry("n2", t + "25.000Z", "attachment", "q2", attachment={
            "type": "queued_command", "prompt": "<ci-monitor-event>CI passed</ci-monitor-event>"}),
        user("side", t + "26.000Z", "SIDECHAIN talk", "n2", isSidechain=True),
    ]
    lines = [json.dumps(x) for x in e]
    # A resume appends copies, one re-parented, and the old boundary again.
    lines += [json.dumps({**e[7], "parentUuid": "x2"}), json.dumps(e[1]),
              json.dumps({"type": "queue-operation", "content": "bookkeeping"})]
    return lines


@pytest.fixture
def mem(tmp_path, monkeypatch):
    stub = tmp_path / "stub"
    stub.mkdir()
    for name, body in (("claude", CLAUDE), ("gh", GH)):
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    (stub / "summary").write_text(SUMMARY)
    # The whole path, pushed and filed, here and in a module load()ed in-process.
    for k in [k for k in os.environ if k.startswith(("SESSION_MEMORY_", "CLAUDE_PLUGIN_OPTION_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("SESSION_MEMORY_PUSH", "1")
    monkeypatch.setenv("SESSION_MEMORY_ISSUES", "1")
    # A machine's global git hooks (a core.hooksPath in ~/.gitconfig) stay out of
    # the throwaway repos: they run at every git command, and slowed two of these
    # tests nearly threefold.
    base = {**os.environ, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": os.devnull}
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True, env=base)
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True, env=base)
    ident = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
    (work / "README").write_text("x\n")
    for cmd in (["add", "README"], ["commit", "-q", "-m", "init"],
                ["push", "-q", "origin", "HEAD:main"]):
        subprocess.run(["git", "-C", str(work), *cmd], check=True, env={**base, **ident})
    tr = tmp_path / "transcript.jsonl"
    tr.write_text("\n".join(transcript_lines()) + "\n")
    env = {**base, "STUB": str(stub), "CALL_SEPARATOR": CALL_SEPARATOR,
           "SESSION_MEMORY_CLAUDE": str(stub / "claude"),
           "SESSION_MEMORY_GH": str(stub / "gh"), "CLAUDE_PROJECT_DIR": str(work)}
    env.pop("CLAUDE_CODE_REMOTE", None)

    class M:
        pass
    m = M()
    m.stub, m.remote, m.work, m.transcript, m.env = stub, remote, work, tr, env

    def hook(event, extra=None, **env_extra):
        payload = {"hook_event_name": event, "session_id": SESSION, "cwd": str(work),
                   "transcript_path": str(tr), **(extra or {})}
        return subprocess.run([sys.executable, str(HOOK), "hook"], input=json.dumps(payload),
                              capture_output=True, text=True, env={**env, **env_extra},
                              timeout=120, check=False)
    m.hook = hook

    def run(*args, **env_extra):
        return subprocess.run([sys.executable, str(HOOK), *args], capture_output=True, text=True,
                              cwd=work, env={**env, **env_extra}, timeout=120, check=False)
    m.run = run

    def on_branch(path=None):
        ls = subprocess.run(["git", "-C", str(remote), "ls-tree", "--name-only",
                             "memory:sessions"], capture_output=True, text=True, check=False)
        if path is None:
            return ls.stdout.split() if ls.returncode == 0 else []
        return subprocess.run(["git", "-C", str(remote), "show", f"memory:sessions/{path}"],
                              capture_output=True, text=True, check=False).stdout
    m.on_branch = on_branch
    m.read = lambda name: (stub / name).read_text() if (stub / name).exists() else ""
    return m


def load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("session_memory", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_digest_is_the_live_stretch_without_the_noise(mem):
    """session memory: the digest is the live stretch, its summary first, the noise left out"""
    out = mem.run("digest", str(mem.transcript)).stdout
    assert out.index("SUMMARY TWO") < out.index("please add the probe")
    for kept in ("USER: please add the probe", "ASSISTANT: Adding it.", "TOOL Bash: Run the tests",
                 "USER (while a turn ran): a question while it ran",
                 "NOTICE: <ci-monitor-event>CI passed"):
        assert kept in out, kept
    for dropped in ("OLD MESSAGE", "FIRST STRETCH", "SUMMARY ONE", "NOISE", "RESULT-BLOB",
                    "SIDECHAIN", "task-notification", "bookkeeping"):
        assert dropped not in out, dropped
    assert out.count("please add the probe") == 1


def test_a_long_digest_drops_its_oldest_items():
    """session memory: over the limit, the oldest items go, and the digest says how many"""
    mod = load()
    entries = [{"type": "user", "uuid": f"u{n}", "timestamp": f"2026-10-13T10:00:{n:02d}Z",
                "message": {"content": f"item {n} {'x' * 50}"}} for n in range(20)]
    text, meta = mod.digest_lines(entries, limit=400)
    assert meta["dropped"] > 0 and "earlier items left out" in text
    assert "item 19" in text and "item 0 " not in text


OWN = "The session found the failing test's cause itself, from the log it read."
# The block summarize() sends, which the prompt's own mention of it never matches.
OWN_BLOCK = "The session's own compaction summary:\n\n"


def saved_file(mem):
    return mem.on_branch(next(f for f in mem.on_branch() if f != "README.md"))


# The payloads of the first real compaction, 2026-10-13 (Claude Code 2.1.296):
# PreCompact has no compact_summary, and PostCompact has it.
PRE = {"trigger": "auto", "custom_instructions": None, "prompt_id": "p1",
       "scratchpad_dir": "scratchpad"}
POST = {"trigger": "auto", "prompt_id": "p1", "scratchpad_dir": "scratchpad"}


def records(mem):
    return [f for f in mem.on_branch() if f not in ("README.md", "own")]


def own_files(mem):
    """The session's own summaries, kept in .git/session-memory/own/ alone."""
    d = mem.work / ".git/session-memory/own"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def pushed_own(mem):
    ls = subprocess.run(["git", "-C", str(mem.remote), "ls-tree", "--name-only",
                         "memory:sessions/own"], capture_output=True, text=True, check=False)
    return ls.stdout.split() if ls.returncode == 0 else []


def pending_dir(mem):
    return mem.work / ".git/session-memory/pending"


def busy(mem):
    """A second pass's files: a summary not yet read, or a stretch it claimed."""
    d = pending_dir(mem)
    return [p.name for p in d.iterdir() if p.suffix == ".own"
            or re.search(r"\.json\.\d+$", p.name)] if d.is_dir() else []


def settle(mem, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and busy(mem):
        time.sleep(0.3)
    assert not busy(mem), f"a second pass never finished: {busy(mem)}"


def created(mem):
    return [line for line in mem.read("gh-created").splitlines() if line]


def logged(mem):
    return (mem.work / ".git/session-memory.log").read_text()


def handed(result):
    """What a SessionStart hook handed the session, or "" for nothing."""
    out = json.loads(result.stdout) if result.stdout.strip() else {}
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def compaction(mem, own=OWN, pre_env=None, post_env=None, **env):
    """A compaction in Claude Code 2.1.296's order: PreCompact, the summary,
    SessionStart (compact), then PostCompact, which returns at once and
    combines in a detached process. This waits for it, and keeps what
    SessionStart handed over in mem.handed."""
    pre = mem.hook("PreCompact", PRE, **{**env, **(pre_env or {})})
    mem.handed = handed(mem.hook("SessionStart", {"source": "compact"}, **env))
    start = time.monotonic()
    post = mem.hook("PostCompact", {**POST, "compact_summary": own}, STUB_SLEEP="2",
                    **{**env, **(post_env or {})})
    took = time.monotonic() - start
    settle(mem)
    return pre, post, took


def test_a_compaction_records_the_digest_then_combines_the_sessions_summary(mem):
    """session memory: PreCompact records the digest; PostCompact adds compact_summary"""
    pre, post, took = compaction(mem)
    assert pre.returncode == 0 and post.returncode == 0 and took < 1.5, took
    name, = records(mem)
    saved = mem.on_branch(name)
    assert f"own_summary: {len(OWN)} chars" in saved
    first, second = mem.read("claude-stdins").split(CALL_SEPARATOR)[:2]
    assert OWN_BLOCK not in first
    assert OWN_BLOCK + OWN in second and second.index(OWN) < second.index("The digest:")
    digest = lambda s: s[s.index("The digest:"):]  # noqa: E731
    assert digest(first) == digest(second)
    # Told by number and title, since GitHub's list can lag the first pass's create.
    assert "already filed these, which the list above may not show yet:\n#77 Decide the split" \
        in second
    assert "Built the probe" in mem.handed and OWN not in mem.handed
    # The stub's second pass proposes the first pass's issue again, by its title:
    # it takes #77, and nothing more is filed.
    assert "issues_filed: [77]" in saved and "(#77)" in saved
    assert len(created(mem)) == 1
    assert "combined with the session's own summary" in logged(mem)


def test_a_record_without_the_sessions_summary_says_so(mem):
    """session memory: PreCompact alone records from the digest, and the file says none"""
    mem.hook("PreCompact", PRE)
    assert OWN_BLOCK not in mem.read("claude-stdin")
    assert "own_summary: none" in saved_file(mem)
    assert own_files(mem) == []


def test_the_summarizer_is_told_whether_it_is_a_compaction(mem):
    """session memory: the code says whether it's a compaction, never the summarizer"""
    mem.hook("PreCompact", PRE)
    sent = mem.read("claude-stdin")
    assert "The trigger: compact:auto\nThis is a compaction." in sent
    assert "Only when the trigger says" not in sent and "This isn't a compaction." not in sent
    mem.run("record", "--transcript", str(mem.transcript), "--session", SESSION,
            "--trigger", "end:other")
    sent = mem.read("claude-stdin")
    assert "The trigger: end:other\nThis isn't a compaction." in sent


def test_a_compaction_whose_summary_gives_no_next_step_says_so(mem):
    """session memory: a compaction's record never hands on "Not a compaction." as its next step"""
    mod = load()
    (mem.stub / "summary").write_text(SUMMARY.replace("- Run the tests next.", "Not a compaction."))
    compaction(mem)
    for text in (saved_file(mem), mem.handed):
        step = text.split("## Continue from here", 1)[1]
        assert "Not a compaction" not in step and mod.NO_NEXT_STEP in step, step
    assert "gave no next step for a compaction" in logged(mem)


def test_an_end_record_keeps_its_not_a_compaction(mem):
    """session memory: outside a compaction, "Not a compaction." is the section's whole text"""
    (mem.stub / "summary").write_text(SUMMARY.replace("- Run the tests next.", "Not a compaction."))
    mem.run("record", "--transcript", str(mem.transcript), "--session", SESSION,
            "--trigger", "end:other")
    step = saved_file(mem).split("## Continue from here", 1)[1]
    assert step.strip().startswith("Not a compaction.")


def test_precompact_prints_nothing_since_its_output_joins_the_compaction_prompt(mem):
    """session memory: PreCompact's stdout is empty, whether the summarizer works or fails

    Claude Code 2.1.296 joins a successful PreCompact hook's stdout into the
    compaction's instructions (read from its code, 2026-10-13)."""
    for env in ({}, {"STUB_CLAUDE_RC": "1"}, {"STUB_CREATE_FAILS": "1"}):
        r = mem.hook("PreCompact", PRE, **env)
        assert r.returncode == 0 and r.stdout == "", (env, r.stdout)


def test_the_sessions_own_summary_is_kept_locally_as_written(mem):
    """session memory: compact_summary goes to .git/session-memory/own/, redacted, never pushed

    It quotes the user's messages in full, and the prompt's rules never touch
    it, so it stays on the machine."""
    token = f"ghp_{'A1b2C3d4E5' * 4}"
    compaction(mem, own=f"{OWN} It saw {token}.")
    raw, = own_files(mem)
    assert [raw] == records(mem) and pushed_own(mem) == []
    path = mem.work / ".git/session-memory/own" / raw
    kept = path.read_text()
    assert OWN in kept and token not in kept and "REDACTED" in kept
    assert "trigger: compact:auto" in kept and "# The session's own compaction summary" in kept
    assert path.stat().st_mode & 0o777 == 0o600
    # Handed on in Claude Code's order, the record arrives and the summary doesn't.
    assert "## Continue from here" in mem.handed and OWN not in mem.handed
    later = handed(mem.hook("SessionStart", {"source": "startup"}))
    assert "## Continue from here" in later and OWN not in later


def test_the_sessions_own_summary_survives_a_failed_summarizer(mem):
    """session memory: claude -p failing still keeps the session's own summary"""
    compaction(mem, STUB_CLAUDE_RC="1")
    raw, = own_files(mem)
    assert OWN in (mem.work / ".git/session-memory/own" / raw).read_text()
    assert records(mem) == []


def test_the_second_pass_records_when_the_first_failed(mem):
    """session memory: PreCompact keeps its digest before claude -p, so PostCompact still records"""
    compaction(mem, STUB_FAIL_FIRST="1")
    name, = records(mem)
    assert f"own_summary: {len(OWN)} chars" in mem.on_branch(name)
    assert own_files(mem) == [name]
    assert "issues_filed: [77]" in mem.on_branch(name) and len(created(mem)) == 1


def test_a_stretch_left_by_an_earlier_compaction_never_pairs_with_this_one(mem, tmp_path):
    """session memory: PreCompact clears a kept stretch first; PostCompact then keeps its own"""
    stale = pending_dir(mem) / f"{SESSION}.json"
    stale.parent.mkdir(parents=True)
    stale.write_text(json.dumps({"name": "2026-01-01T00-00-00Z-stale-compact.md",
                                 "text": "STALE", "meta": {"started": "x"}}))
    mem.hook("PreCompact", {**PRE, "transcript_path": str(tmp_path / "gone.jsonl")})
    assert not stale.exists()
    mem.hook("PostCompact", {**POST, "compact_summary": OWN})
    settle(mem)
    assert records(mem) == [] and len(own_files(mem)) == 1
    assert "stale" not in own_files(mem)[0] and "STALE" not in mem.read("claude-stdins")
    assert "no stretch kept by PreCompact" in logged(mem)


def test_a_second_pass_still_running_leaves_a_later_compactions_stretch(mem):
    """session memory: a second pass removes only the stretch it claimed, never a newer one"""
    mem.hook("PreCompact", PRE)
    first, = records(mem)
    mem.hook("PostCompact", {**POST, "compact_summary": OWN}, STUB_SLEEP="4")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(".json." in b for b in busy(mem)):
        time.sleep(0.1)
    assert any(".json." in b for b in busy(mem)), "the second pass never claimed its stretch"
    time.sleep(1.1)  # a later name
    mem.hook("PreCompact", PRE)  # the next compaction, while the second pass runs
    later = json.loads((pending_dir(mem) / f"{SESSION}.json").read_text())["name"]
    assert later != first
    settle(mem)
    kept = pending_dir(mem) / f"{SESSION}.json"
    assert kept.exists() and json.loads(kept.read_text())["name"] == later


def test_a_filing_that_times_out_is_marked_and_the_record_saved(mem):
    """session memory: a create past its timeout is not filed, not retried, and costs no record"""
    r = mem.hook("PreCompact", PRE, SESSION_MEMORY_GH_SECONDS="1", STUB_CREATE_HANGS="1")
    assert r.returncode == 0
    saved = saved_file(mem)
    assert "(new: 1, not filed: timed out after 1 s)" in saved and "issues_filed: []" in saved
    assert len(created(mem)) == 1  # no second, unlabelled try


def test_a_5xx_on_filing_is_not_retried_without_the_label(mem):
    """session memory: after a 5xx the issue may exist, so it's marked, never posted again"""
    mem.hook("PreCompact", PRE, STUB_CREATE_5XX="1")
    assert "(new: 1, not filed: gh: Bad Gateway (HTTP 502))" in saved_file(mem)
    assert len(created(mem)) == 1


def test_an_issue_entry_that_isnt_an_object_is_skipped(mem):
    """session memory: a malformed entry in new_issues is logged and skipped; the rest is filed"""
    (mem.stub / "summary").write_text(SUMMARY.replace(
        '"new_issues": [', '"new_issues": ["not an object", '))
    mem.hook("PreCompact", PRE)
    assert "(#77)" in saved_file(mem) and len(created(mem)) == 1
    assert "1 issue entries had no key or title, skipped" in logged(mem)


def test_unreadable_open_issues_file_nothing(mem):
    """session memory: without the open issues, new items may not be new, so none is filed"""
    mem.hook("PreCompact", PRE, STUB_LIST_FAILS="1")
    assert "(new: 1, not filed: the open issues couldn't be read)" in saved_file(mem)
    assert created(mem) == [] and "(none could be read)" in mem.read("claude-stdin")


def test_a_second_pass_missing_sections_leaves_the_first_record(mem):
    """session memory: an incomplete second pass files nothing and replaces nothing"""
    (mem.stub / "summary2").write_text("## Done\n- Only this.\n")
    compaction(mem)
    name, = records(mem)
    saved = mem.on_branch(name)
    assert "own_summary: none" in saved and "## Continue from here" in saved
    assert len(created(mem)) == 1 and own_files(mem) == [name]
    assert "the second pass lacked" in logged(mem)


def test_an_issue_list_that_doesnt_parse_is_said(mem):
    """session memory: a json block that isn't JSON files nothing, and the markers say why"""
    (mem.stub / "summary").write_text(SUMMARY.replace('"title": "Built the probe",', '"title": ,'))
    mem.hook("PreCompact", PRE)
    assert "(new: 1, not filed: the summary's issue list didn't parse)" in saved_file(mem)
    assert created(mem) == [] and "no issues read from the summary" in logged(mem)


def test_a_cut_session_summary_says_so(mem):
    """session memory: past 60,000 characters the summarizer and the record both say it's cut"""
    long_own = OWN + "x" * (60010 - len(OWN))
    compaction(mem, own=long_own)
    second = mem.read("claude-stdins").split(CALL_SEPARATOR)[1]
    assert "[the session's summary is cut here: 10 more characters left out]" in second
    assert "own_summary: 60010 chars, the first 60000 used" in mem.on_branch(records(mem)[0])


def test_a_retried_compaction_rewrites_its_record(mem):
    """session memory: Claude Code's retry of a failed compaction is another pass at one record"""
    mem.hook("PreCompact", PRE)
    time.sleep(1.1)
    mem.hook("PreCompact", PRE)  # no PostCompact came: the compaction failed, and is retried
    assert len(records(mem)) == 1 and len(created(mem)) == 1
    assert "a retry, so it's rewritten" in logged(mem)


def test_a_postcompact_without_a_summary_leaves_the_hand_off_its_record(mem):
    """session memory: on the replacement path SessionStart follows PostCompact, and still hands on

    Claude Code 2.1.296's replacement path runs PostCompact, with no summary,
    before SessionStart (compact). So the stretch stays for SessionStart."""
    mem.hook("PreCompact", PRE)
    mem.hook("PostCompact", {**POST, "compact_summary": ""})
    assert "Built the probe" in handed(mem.hook("SessionStart", {"source": "compact"}))
    assert len(records(mem)) == 1 and own_files(mem) == []
    assert "PostCompact brought no summary" in logged(mem)


def test_a_pass_killed_mid_filing_has_kept_what_it_filed(mem):
    """session memory: each create is kept before it goes and as it ends, so none is filed twice

    Claude Code kills a PreCompact hook at its 600 s timeout. This one is
    killed while its second create hangs, after its first went through."""
    (mem.stub / "summary").write_text(SUMMARY.replace(
        "- Whether to rename it", "- Whether to rename it (new: 2)").replace(
        '"body": "It is open."}]', '"body": "It is open."}, '
        '{"key": 2, "title": "Decide the name", "body": "Also open."}]'))
    payload = {"hook_event_name": "PreCompact", "session_id": SESSION, "cwd": str(mem.work),
               "transcript_path": str(mem.transcript), **PRE}
    hook = subprocess.Popen([sys.executable, str(HOOK), "hook"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                            env={**mem.env, "STUB_HANG_SECOND": "1"})
    hook.stdin.write(json.dumps(payload))
    hook.stdin.close()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(created(mem)) < 2:
        time.sleep(0.1)
    hook.kill()
    hook.wait()
    kept = json.loads((pending_dir(mem) / f"{SESSION}.json").read_text())
    # The create that was in flight is kept as one GitHub may have made.
    assert kept["filed"] == [{"number": 77, "title": "Decide the split"},
                             {"number": None, "title": "Decide the name"}], kept["filed"]
    assert records(mem) == []
    mem.hook("PostCompact", {**POST, "compact_summary": OWN})
    settle(mem)
    titles = [json.loads(c)["title"] for c in created(mem)]
    assert titles.count("Decide the split") == 1 and titles.count("Decide the name") == 1, titles


def test_a_create_that_may_have_worked_is_never_posted_again(mem):
    """session memory: after a 5xx the issue may exist, so a later pass marks it and never posts"""
    compaction(mem, pre_env={"STUB_CREATE_5XX": "1"})
    assert len(created(mem)) == 1
    saved = mem.on_branch(records(mem)[0])
    assert "(new: 1, not filed: an earlier try may have filed it, so check the tracker)" in saved
    second = mem.read("claude-stdins").split(CALL_SEPARATOR)[1]
    # Told to keep it with a marker, as the prompt's marker rule says, never to drop it.
    assert ("An earlier pass at this same stretch tried to file these too, and GitHub may have "
            "created them. One that's still undecided keeps its bullet, with a (new: K) marker "
            "and the same title") in second
    assert "never propose them again.\n- Decide the split" not in second


def test_an_end_record_after_a_failed_compaction_files_none_of_its_issues_again(mem):
    """session memory: a session end over a failed compaction's stretch knows what it filed"""
    mem.hook("PreCompact", PRE)  # files #77; no PostCompact comes: the compaction failed
    mem.hook("SessionEnd", {"reason": "prompt_input_exit"})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not any(f.endswith("-end.md") for f in records(mem)):
        time.sleep(0.3)
    end = next(f for f in records(mem) if f.endswith("-end.md"))
    assert "(#77)" in mem.on_branch(end) and len(created(mem)) == 1
    assert "whose compaction failed: its issues aren't filed again" in logged(mem)


def test_an_old_stretch_is_never_handed_on_as_this_compactions(mem):
    """session memory: a stretch kept over half an hour ago is another compaction's, not this"""
    mem.hook("PreCompact", PRE)
    kept = pending_dir(mem) / f"{SESSION}.json"
    stretch = json.loads(kept.read_text())
    stretch["kept_at"] = time.time() - 7200
    kept.write_text(json.dumps(stretch))
    assert handed(mem.hook("SessionStart", {"source": "compact"})) == ""
    assert "is over 30 minutes old, so not this compaction's; nothing handed on" in logged(mem)


def test_a_title_that_isnt_text_is_never_filed(mem):
    """session memory: a null title isn't filed as "None"; its marker says its entry has none"""
    (mem.stub / "summary").write_text(SUMMARY.replace('"title": "Decide the split"',
                                                      '"title": null'))
    mem.hook("PreCompact", PRE)
    assert created(mem) == []
    assert "(new: 1, not filed: its entry has no title)" in saved_file(mem)


def test_unreadable_issues_in_the_second_pass_keep_the_first_pass_numbers(mem):
    """session memory: what the first pass filed keeps its number when the list can't be read"""
    compaction(mem, post_env={"STUB_LIST_FAILS": "1"})
    saved = mem.on_branch(records(mem)[0])
    assert f"own_summary: {len(OWN)} chars" in saved
    assert "(#77)" in saved and "not filed" not in saved and len(created(mem)) == 1


def test_a_second_pass_whose_issue_list_isnt_a_list_leaves_the_first_record(mem):
    """session memory: a malformed new_issues is caught before the second pass replaces anything"""
    (mem.stub / "summary2").write_text(SUMMARY.replace(
        '"new_issues": [{"key": 1, "title": "Decide the split", "body": "It is open."}]',
        '"new_issues": {"key": 1}'))
    compaction(mem)
    saved = mem.on_branch(records(mem)[0])
    assert "own_summary: none" in saved and "(#77)" in saved
    assert "the second pass lacked its json block" in logged(mem)


def test_a_marker_with_no_entry_says_so(mem):
    """session memory: a (new: K) the issue list has no entry for is never left bare"""
    (mem.stub / "summary").write_text(SUMMARY.replace(
        "- Whether to rename it", "- Whether to rename it (new: 2)"))
    mem.hook("PreCompact", PRE)
    assert ("(new: 2, not filed: the summary's issue list has no entry for it)"
            in saved_file(mem))
    assert "(#77)" in saved_file(mem)


def test_an_incomplete_second_pass_is_kept_when_its_the_only_record(mem):
    """session memory: with no first record to protect, an incomplete second pass is saved"""
    (mem.stub / "summary2").write_text("## Done\n- Only this.\n")
    compaction(mem, STUB_FAIL_FIRST="1")
    name, = records(mem)
    assert "Only this." in mem.on_branch(name)


def test_a_second_own_summary_under_one_name_never_replaces_the_first(mem):
    """session memory: two passes at one name keep both of the session's summaries"""
    mod = load()
    for text in ("FIRST SUMMARY", "SECOND SUMMARY"):
        mod.save_own(mem.work, "2026-10-13T00-00-00Z-abcdef12-compact.md", text, SESSION, "c")
    own = mem.work / ".git/session-memory/own"
    assert sorted(p.name for p in own.iterdir()) == [
        "2026-10-13T00-00-00Z-abcdef12-compact-2.md", "2026-10-13T00-00-00Z-abcdef12-compact.md"]
    assert "FIRST" in (own / "2026-10-13T00-00-00Z-abcdef12-compact.md").read_text()


def later(text, by="9999"):
    """text with its recorded: time moved past any real one."""
    return re.sub(r"^recorded: \S+", f"recorded: {by}-01-01T00:00:00Z", text, flags=re.MULTILINE)


def test_a_rewrite_whose_push_failed_is_still_handed_on(mem):
    """session memory: a local copy recorded after the branch's wins: its push failed"""
    mem.hook("PreCompact", PRE)
    name, = records(mem)
    local = mem.work / ".git/session-memory" / name
    local.write_text(later(local.read_text().replace("Built the probe", "The rewrite")))
    assert "The rewrite" in handed(mem.hook("SessionStart", {"source": "startup"}))


def test_a_local_copy_that_lags_the_branch_loses_to_it(mem, monkeypatch):
    """session memory: the branch's copy recorded later wins over a stale local one

    Local copies of old records have lagged the branch, so a local copy can't
    simply win."""
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.setenv(k, mem.env[k])
    mem.hook("PreCompact", PRE)
    name, = records(mem)
    load().save(mem.work, name, later(mem.on_branch(name).replace("Built the probe",
                                                                  "Rewritten elsewhere")))
    assert "Rewritten elsewhere" in handed(mem.hook("SessionStart", {"source": "startup"}))


def test_another_writers_rewrite_wins_over_a_copy_taken_for_a_hand_off(mem, monkeypatch):
    """session memory: a record copied to hand on never hides its writer's later rewrite

    A record from another checkout or a cloud session, over the cap, is copied
    locally so the hand-off can name it. Its writer's second pass then rewrites
    it on the branch, and that rewrite is what's handed on next."""
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.setenv(k, mem.env[k])  # mod.save runs git here, outside mem.hook
    mod = load()
    done = "".join(f"- Finished step {n:03} {'of the work ' * 6}\n" for n in range(150))
    (mem.stub / "summary").write_text(SUMMARY.replace("- Built the probe (#3).\n", done))
    mem.hook("PreCompact", PRE)
    name, = records(mem)
    (mem.work / ".git/session-memory" / name).unlink()  # as if another machine wrote it
    first = handed(mem.hook("SessionStart", {"source": "startup"}))
    assert "Read the whole of it, at" in first and "/handed/" in first
    rewrite = mem.on_branch(name).replace("Built the probe", "The second pass")
    mod.save(mem.work, name, rewrite)  # the writer's rewrite, pushed
    assert "The second pass" in handed(mem.hook("SessionStart", {"source": "startup"}))


def test_a_tailscale_key_and_an_aws_key_id_are_redacted_whole():
    """session memory: a Tailscale key loses its secret half too, and a temporary AWS key id goes"""
    mod = load()
    # Both are made up, and assembled here so no secret scanner takes the source for a real key.
    tailscale = "-".join(["tskey", "auth", "kAbCdEfGh1CNTRL", "SeCrEtHaLf0123456789"])
    aws = "ASIA" + "ABCDEFGHIJKLMNOP"
    text, hidden = mod.redact(f"{tailscale} and {aws}")
    assert hidden == 2 and "SeCrEtHaLf" not in text and "ASIAABCD" not in text, text


def test_old_pending_files_are_swept_and_new_ones_are_private(mem):
    """session memory: a pending file a day old is removed; a new one is this account's alone"""
    old = pending_dir(mem) / "0000-old.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    two_days = time.time() - 2 * 86400
    os.utime(old, (two_days, two_days))
    mem.hook("PreCompact", PRE)
    assert not old.exists() and "over 1 day old, removed" in logged(mem)
    assert (pending_dir(mem) / f"{SESSION}.json").stat().st_mode & 0o777 == 0o600


def test_the_hook_uses_its_project_not_the_shells_cwd(mem, tmp_path):
    """session memory: a cwd in another repository never takes the records or the issues there"""
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, env=mem.env)
    r = mem.hook("PreCompact", {**PRE, "cwd": str(other)})
    assert r.returncode == 0 and len(records(mem)) == 1
    assert not (other / ".git/session-memory").exists()


def test_a_failed_first_pass_hands_on_a_note_not_another_record(mem):
    """session memory: with this compaction's record unwritten, no other is handed on as it"""
    (mem.stub / "summary").write_text(SUMMARY.replace("Built the probe", "Someone else's"))
    mem.hook("PreCompact", {**PRE, "session_id": "99999999-other"})
    time.sleep(1.1)
    mem.hook("PreCompact", PRE, STUB_CLAUDE_RC="1")
    text = json.loads(mem.hook("SessionStart", {"source": "compact"}).stdout)[
        "hookSpecificOutput"]["additionalContext"]
    assert "wasn't written" in text and "Someone else's" not in text


def test_each_compaction_logs_what_its_hooks_were_given(mem):
    """session memory: both compaction hooks log their fields and the summary's length only"""
    _, post, _ = compaction(mem)
    assert post.stdout == ""
    log = (mem.work / ".git/session-memory.log").read_text()
    assert re.search(r"PreCompact auto: fields \S*custom_instructions\S*; "
                     r"compact_summary absent", log), log
    assert re.search(rf"PostCompact auto: fields \S*compact_summary\S*; "
                     rf"compact_summary {len(OWN)} chars", log), log
    assert OWN not in log


def test_a_compaction_saves_its_summary_to_the_memory_branch(mem):
    """session memory: PreCompact summarizes, files the new issue, pushes the file and index"""
    r = mem.hook("PreCompact", {"trigger": "auto"})
    assert r.returncode == 0, r.stderr
    files = [f for f in mem.on_branch() if f != "README.md"]
    assert len(files) == 1 and files[0].endswith(f"-{SESSION[:8]}-compact.md"), files
    saved = mem.on_branch(files[0])
    assert "trigger: compact:auto" in saved and "branch: feature-x" in saved
    assert "(#77)" in saved and "(new: 1)" not in saved and "(#5)" in saved
    assert 'title: "Built the probe"' in saved
    assert "```markdown" not in saved and "## Continue from here" in saved
    index = mem.on_branch("README.md")
    assert files[0] in index and "Built the probe" in index
    assert '"title": "Decide the split"' in mem.read("gh-created")
    assert [c for c in mem.read("gh-calls").splitlines() if not c.startswith("api ")] == []
    stdin = mem.read("claude-stdin")
    issues, pulls = stdin.split("The open issues:\n")[1].split("The open pull requests:\n")
    assert "#5 Keep it" in issues and "A pull request" not in issues
    # So a merge waiting on a pull request is cited by it, not filed as an issue.
    assert pulls.startswith("#9 A pull request\n") and "Keep it" not in pulls


def test_a_failed_filing_says_so_in_the_record(mem):
    """session memory: an issue GitHub won't create is marked not filed, with why"""
    mem.hook("PreCompact", {"trigger": "auto"}, STUB_CREATE_FAILS="1")
    saved = mem.on_branch(next(f for f in mem.on_branch() if f != "README.md"))
    assert "(new: 1, not filed: gh: Validation Failed (HTTP 422))" in saved
    assert "issues_filed: []" in saved


def test_a_missing_transcript_records_nothing(mem, tmp_path):
    """session memory: a session end with no transcript logs that, saves nothing, never crashes"""
    r = mem.run("record", "--transcript", str(tmp_path / "gone.jsonl"), "--session", SESSION,
                "--trigger", "end:other")
    assert r.returncode == 0, r.stderr
    log = (mem.work / ".git/session-memory.log").read_text()
    assert "no transcript at" in log and "Error" not in log, log
    assert mem.on_branch() == []


def test_the_summarizer_is_bounded(mem):
    """session memory: claude -p runs outside the repo, restricted, toolless, as the child"""
    mem.hook("PreCompact", {"trigger": "manual"})
    argv = mem.read("claude-argv").splitlines()
    for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv, (flag, argv)
    assert argv[argv.index("--tools") + 1] == ""
    assert mem.read("claude-child").strip() == "1"
    assert os.path.realpath(mem.read("claude-cwd").strip()) != os.path.realpath(mem.work)
    stdin = mem.read("claude-stdin")
    assert "The trigger: compact:manual" in stdin and "#5 Keep it" in stdin
    assert stdin.startswith(PROMPT.read_text()[:200])


def test_the_working_tree_and_its_branch_are_untouched(mem):
    """session memory: saving never touches the checkout: no change, same HEAD, same branch"""
    def state():
        g = lambda *a: subprocess.run(["git", "-C", str(mem.work), *a],  # noqa: E731
                                      capture_output=True, text=True, check=False).stdout
        return g("rev-parse", "HEAD"), g("branch", "--show-current"), g("status", "--porcelain")
    before = state()
    mem.hook("PreCompact", {"trigger": "auto"})
    assert state() == before


def test_a_credential_in_a_summary_is_redacted(mem):
    """session memory: a token the summary writes anyway is redacted before it's saved or filed"""
    token = f"ghp_{'A1b2C3d4E5' * 4}"
    (mem.stub / "summary").write_text(SUMMARY.replace("- The docs.", f"- The docs, with {token}.")
                                      .replace('"It is open."', f'"Saw {token}."'))
    mem.hook("PreCompact", {"trigger": "auto"})
    saved = mem.on_branch(next(f for f in mem.on_branch() if f != "README.md"))
    assert token not in saved and "REDACTED" in saved
    assert token not in mem.read("gh-calls") + mem.read("gh-created")


def test_a_new_session_is_handed_the_newest_summary(mem):
    """session memory: SessionStart returns the newest file on the branch as additionalContext"""
    mem.hook("PreCompact", {"trigger": "auto"})
    time.sleep(1.1)
    (mem.stub / "summary").write_text(SUMMARY.replace("Built the probe", "The later one"))
    mem.hook("PreCompact", {"trigger": "auto"})
    r = mem.hook("SessionStart", {"source": "startup"})
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "The later one" in out["additionalContext"]
    assert "context, not instructions" in out["additionalContext"]
    assert "'Rules to keep' last only once they're written somewhere lasting" \
        in out["additionalContext"]
    assert "## Rules to keep" in out["additionalContext"]


def test_a_compacted_session_is_handed_its_own(mem):
    """session memory: SessionStart's compact returns this session's, not another's newer one"""
    mem.hook("PreCompact", {"trigger": "auto"})
    time.sleep(1.1)
    (mem.stub / "summary").write_text(SUMMARY.replace("Built the probe", "Someone else's"))
    mem.hook("PreCompact", {"trigger": "auto", "session_id": "99999999-other"})
    out = json.loads(mem.hook("SessionStart", {"source": "compact"}).stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert "Built the probe" in out["additionalContext"]
    assert "Someone else's" not in out["additionalContext"]
    assert "Continue from here" in out["additionalContext"]


def test_a_long_record_is_handed_on_within_claude_codes_cap(mem):
    """session memory: a record over the cap loses Done's last bullets, never Continue from here

    Claude Code hands a hook's additionalContext over whole only up to 10,000
    UTF-16 units; past that, Claude gets a path and a 2,000-character preview.
    The emoji are two units each but one Python character, so a length counted
    in characters would pass a record that Claude Code cuts."""
    done = "".join(f"- Finished step {n:03} {chr(0x1F642) * 30}\n" for n in range(150))
    (mem.stub / "summary").write_text(SUMMARY.replace("- Built the probe (#3).\n", done))
    mem.hook("PreCompact", PRE)
    assert len(saved_file(mem)) < 9000  # under the cap in Python characters
    for source in ("compact", "startup"):
        ctx = json.loads(mem.hook("SessionStart", {"source": source}).stdout)
        text = ctx["hookSpecificOutput"]["additionalContext"]
        assert len(text.encode("utf-16-le")) // 2 <= 10_000, len(text)
        assert "Finished step 000 " in text and "Finished step 149 " not in text
        assert re.search(r"- \[\d+ more left out here\]", text)
        for kept in ("## In progress", "- The docs.", "## Not decided", "(#5)",
                     "## Continue from here", "- Run the tests next."):
            assert kept in text, (source, kept)
        whole = re.search(r"Read the whole of it, at `([^`]+)`", text).group(1)
        assert "Finished step 149 " in Path(whole).read_text()


def test_a_short_record_is_handed_on_whole(mem):
    """session memory: a record within the cap is handed over as written, with no note"""
    mem.hook("PreCompact", PRE)
    text = json.loads(mem.hook("SessionStart", {"source": "compact"}).stdout)[
        "hookSpecificOutput"]["additionalContext"]
    assert saved_file(mem).split("\n---\n", 1)[1].strip() in text
    assert "left out" not in text and "Read the whole" not in text


def test_the_summarizers_own_session_does_nothing(mem):
    """session memory: with the child's variable set, the hook returns at once and calls nothing"""
    r = mem.hook("PreCompact", {"trigger": "auto"}, SESSION_MEMORY_CHILD="1")
    assert r.returncode == 0 and r.stdout == ""
    assert mem.read("claude-argv") == "" and mem.on_branch() == []


def test_a_failing_summarizer_saves_nothing_and_stops_nothing(mem):
    """session memory: claude -p failing: the hook exits 0, pushes nothing, logs why"""
    r = mem.hook("PreCompact", {"trigger": "auto"}, STUB_CLAUDE_RC="1")
    assert r.returncode == 0
    assert mem.on_branch() == []
    log = (mem.work / ".git/session-memory.log").read_text()
    assert "claude -p exited 1" in log


def test_a_session_end_returns_at_once_and_records_behind_it(mem):
    """session memory: SessionEnd returns at once; the detached child saves the file"""
    start = time.monotonic()
    r = mem.hook("SessionEnd", {"reason": "prompt_input_exit"}, STUB_SLEEP="3")
    assert r.returncode == 0 and time.monotonic() - start < 1.5
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not mem.on_branch():
        time.sleep(0.5)
    files = [f for f in mem.on_branch() if f != "README.md"]
    assert len(files) == 1 and files[0].endswith("-end.md"), files
    assert "trigger: end:prompt_input_exit" in mem.on_branch(files[0])


def test_new_issues_are_capped_and_a_missing_label_is_survived(mem):
    """session memory: ten issues at most, the rest marked; no label, filed without one"""
    items = [{"key": k, "title": f"Decide {k}", "body": "open"} for k in range(1, 13)]
    bullets = "\n".join(f"- Open {k} (new: {k})" for k in range(1, 13))
    text = SUMMARY.replace("- Whether to split it (new: 1)", bullets)
    (mem.stub / "summary").write_text(text.replace(
        '"new_issues": [{"key": 1, "title": "Decide the split", "body": "It is open."}]',
        '"new_issues": ' + json.dumps(items)))
    mem.hook("PreCompact", {"trigger": "auto"}, STUB_NO_LABEL="1")
    saved = mem.on_branch(next(f for f in mem.on_branch() if f != "README.md"))
    created = [c for c in mem.read("gh-created").splitlines() if '"labels"' not in c]
    assert len(created) == 10
    assert saved.count("not filed: over the 10") == 2


def test_the_merge_of_an_open_pull_request_is_never_filed(mem):
    """session memory: a new item only asking to merge open pull requests isn't filed"""
    items = [{"key": 1, "title": "Merge PR #9 (the probe)", "body": "Waits on the user."},
             {"key": 2, "title": "Merge PR #9 after #5", "body": "#5 is an issue."},
             {"key": 3, "title": "Decide what PR #9 should cover", "body": "Its scope."},
             {"key": 4, "title": "Merging PR #9", "body": "The records' own wording."},
             {"key": 5, "title": "Merge the two runbooks", "body": "No number at all."}]
    bullets = "\n".join(f"- Item {k} (new: {k})" for k in range(1, 6))
    text = SUMMARY.replace("- Whether to split it (new: 1)", bullets)
    (mem.stub / "summary").write_text(text.replace(
        '"new_issues": [{"key": 1, "title": "Decide the split", "body": "It is open."}]',
        '"new_issues": ' + json.dumps(items)))
    mem.hook("PreCompact", {"trigger": "auto"})
    saved = mem.on_branch(next(f for f in mem.on_branch() if f != "README.md"))
    assert "- Item 1 (#9)" in saved and "- Item 4 (#9)" in saved, saved
    # Naming an issue or no number, or asking something else of it, is filed as before.
    titles = [json.loads(c)["title"] for c in mem.read("gh-created").splitlines()]
    assert titles == ["Merge PR #9 after #5", "Decide what PR #9 should cover",
                      "Merge the two runbooks"], titles
    assert "isn't filed" in (mem.work / ".git/session-memory.log").read_text()


def test_backfill_writes_each_finished_stretch_and_files_nothing(mem):
    """session memory: backfill summarizes each finished stretch, by its time and trigger"""
    args = ("backfill", "--transcript", str(mem.transcript), "--session", SESSION)
    dry = mem.run(*args, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout.split()[:2] == ["2026-10-13T10-00-10Z-abcdef12-compact.md", "compact:manual"]
    assert "2026-10-13T10-00-20Z-abcdef12-compact.md  compact:auto" in dry.stdout
    r = mem.run(*args)
    assert r.returncode == 0, r.stderr
    files = [f for f in mem.on_branch() if f != "README.md"]
    assert files == ["2026-10-13T10-00-10Z-abcdef12-compact.md",
                     "2026-10-13T10-00-20Z-abcdef12-compact.md"]
    assert "(not filed: historical)" in mem.on_branch(files[1])
    assert mem.read("gh-created") == ""
    # Today's open pull request is never shown to an old stretch.
    stdins = mem.read("claude-stdins")
    assert "The open pull requests:\n(none)\n" in stdins and "#9 A pull request" not in stdins
    again = mem.run(*args, "--dry-run")
    assert again.stdout.strip() == ""


def prompt_for(mod, tmp_path):
    """The prompt as summarize() sends it, for a project with no rules of its own."""
    return mod.prompt_text(tmp_path)


def test_the_prompt_asks_for_what_the_parser_checks(tmp_path, monkeypatch):
    """session memory: either variant names every section the parser looks for, and the json block"""
    for on in ("1", ""):
        monkeypatch.setenv("SESSION_MEMORY_ISSUES", on)
        mod = load()
        prompt = prompt_for(mod, tmp_path)
        for section in mod.SECTIONS:
            assert section in prompt, section
        assert "```json" in prompt and mod.OWN_HEADING in prompt
        assert ('"new_issues"' in prompt) == bool(on)
        assert ("(new: K)" in prompt) == bool(on)
        assert "<!--" not in prompt, "a variant marker or a comment reached the prompt"
        # The plugin's own rules, since the project has none.
        assert 'say "a customer"' in prompt


def test_a_projects_own_rules_replace_the_plugins(mem):
    """session memory: .claude/session-memory-rules.md in the project is the summarizer's rules"""
    (mem.work / ".claude").mkdir()
    (mem.work / ".claude/session-memory-rules.md").write_text(
        "<!-- not for the prompt -->\n- Never write the word PINEAPPLE.\n")
    assert mem.hook("PreCompact", PRE).returncode == 0
    stdin = mem.read("claude-stdin")
    assert "- Never write the word PINEAPPLE." in stdin
    assert 'say "a customer"' not in stdin and "not for the prompt" not in stdin


def test_the_hooks_register_each_event():
    """session memory: PreCompact, PostCompact and SessionEnd write; SessionStart hands it on

    A summary reaches the model only as a SessionStart hook's additionalContext.
    PostCompact has no decision control and no additionalContext, so a summary
    handed there is dropped: the first real compaction showed it. PostCompact is
    where compact_summary arrives, so it writes, and prints nothing."""
    hooks = json.loads(HOOKS.read_text())["hooks"]

    def ours(event):
        return [m.get("matcher", "") for m in hooks.get(event, []) for h in m["hooks"]
                if h["command"] == '"${CLAUDE_PLUGIN_ROOT}"/scripts/session-memory.py hook']

    for event in ("PreCompact", "PostCompact", "SessionEnd"):
        assert ours(event), event
    assert hooks["PreCompact"][0]["hooks"][0]["timeout"] == 600
    sources = {s for m in ours("SessionStart") for s in m.split("|")}
    assert {"startup", "resume", "clear", "compact"} <= sources, sources
    assert os.access(HOOK, os.X_OK), "the hooks run the script directly, so it must be executable"


LOCAL = "refs/session-memory/memory"


def local_branch(mem, path=None):
    """The memory ref in the clone itself, where records go when they aren't pushed."""
    ls = subprocess.run(["git", "-C", str(mem.work), "ls-tree", "--name-only",
                         f"{LOCAL}:sessions"], capture_output=True, text=True, check=False)
    if path is None:
        return ls.stdout.split() if ls.returncode == 0 else []
    return subprocess.run(["git", "-C", str(mem.work), "show", f"{LOCAL}:sessions/{path}"],
                          capture_output=True, text=True, check=False).stdout


OFF = {"SESSION_MEMORY_PUSH": "", "SESSION_MEMORY_ISSUES": ""}


def test_by_default_records_stay_in_the_clone_and_nothing_calls_github(mem):
    """session memory: with both switches off, a local branch, no push, no gh, no issue lists"""
    r = mem.hook("PreCompact", PRE, **OFF)
    assert r.returncode == 0 and r.stdout == ""
    name, = [n for n in local_branch(mem) if n != "README.md"]
    saved = local_branch(mem, name)
    assert "Built the probe" in saved and "host: local" in saved
    assert mem.on_branch() == [], "nothing may reach the remote"
    assert mem.read("gh-calls") == "" and mem.read("gh-created") == ""
    stdin = mem.read("claude-stdin")
    assert "The open issues:" not in stdin and "(new: K)" not in stdin
    # The summary's stray marker says why it's unfiled, never that it was.
    assert "(not filed: issue filing is off)" in saved and "(new: 1)" not in saved
    assert "saved as" in logged(mem) and f"on {LOCAL}" in logged(mem)
    # Outside refs/heads/, so no `git push --all`, checkout or branch of the user's touches it.
    heads = subprocess.run(["git", "-C", str(mem.work), "for-each-ref", "refs/heads/"],
                           capture_output=True, text=True, check=True).stdout
    assert "memory" not in heads, heads
    pushed = subprocess.run(["git", "-C", str(mem.work), "push", "-q", "--all", "origin"],
                            capture_output=True, text=True, env=mem.env, check=False)
    assert pushed.returncode == 0 and mem.on_branch() == [], pushed.stderr
    # And a new session is handed it, from that branch.
    out = json.loads(mem.hook("SessionStart", {"source": "startup"}, **OFF).stdout)
    assert "Built the probe" in out["hookSpecificOutput"]["additionalContext"]


def test_a_stray_marker_with_filing_off_says_so(mem):
    """session memory: filing off, a (new: K) with no entry, or with no json block, says filing is off"""
    stray = SUMMARY.replace("- Whether to rename it", "- Whether to rename it (new: 2)")
    for reply in (stray, stray[:stray.index("```json")]):
        (mem.stub / "summary").write_text(reply)
        # What SessionEnd detaches, run in the foreground; each is then the newest.
        assert mem.run("record", "--transcript", str(mem.transcript), "--session", SESSION,
                       "--trigger", "end:other", **OFF).returncode == 0
        time.sleep(1.1)
        saved = mem.run("latest", **OFF).stdout
        assert "Built the probe" in saved and "trigger: end:other" in saved, saved
        assert "(new: 2, not filed: issue filing is off)" in saved, saved
        assert "didn't parse" not in saved and "has no entry" not in saved, saved


def test_each_save_adds_to_the_local_branch(mem, monkeypatch):
    """session memory: unpushed, each save is a commit on the local branch, and keeps the last"""
    for k, v in OFF.items():
        monkeypatch.setenv(k, v)
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
        monkeypatch.setenv(k, mem.env[k])
    mod = load()
    mod.save(mem.work, "2026-01-01T00-00-00Z-aaaaaaaa-end.md", "---\nrecorded: 1\n---\n# One\n")
    mod.save(mem.work, "2026-01-02T00-00-00Z-bbbbbbbb-end.md", "---\nrecorded: 2\n---\n# Two\n")
    assert {"2026-01-01T00-00-00Z-aaaaaaaa-end.md",
            "2026-01-02T00-00-00Z-bbbbbbbb-end.md"} <= set(local_branch(mem))
    assert mem.on_branch() == []


def test_the_plugins_own_options_switch_it_too(monkeypatch):
    """session memory: CLAUDE_PLUGIN_OPTION_PUSH and _ISSUES, as Claude Code exports them"""
    for k in [k for k in os.environ if k.startswith(("SESSION_MEMORY_", "CLAUDE_PLUGIN_OPTION_"))]:
        monkeypatch.delenv(k)
    mod = load()
    assert not mod.PUSH and not mod.ISSUES
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_PUSH", "true")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_ISSUES", "false")
    mod = load()
    assert mod.PUSH and not mod.ISSUES
    monkeypatch.setenv("SESSION_MEMORY_PUSH", "0")  # the environment's own wins
    assert not load().PUSH


def test_a_named_datadog_key_is_redacted_and_a_commit_hash_is_not():
    """session memory: a Datadog key goes wherever it's named; a bare 40-hex hash stays"""
    mod = load()
    key = "0123456789abcdef0123456789abcdef"
    sha = "0123456789abcdef0123456789abcdef01234567"
    for named in (f"DD_API_KEY={key}", f"dd-api-key: {key}", f"export DD_APP_KEY='{sha}'",
                  f'"DD_API_KEY": "{key}"', f"DATADOG_APP_KEY={key}", f"TF_VAR_DD_API_KEY={key}",
                  f"--dd-api-key {key}", f"DD-APPLICATION-KEY: {sha}"):
        text, n = mod.redact(f"{named}, then commit {sha}")
        assert n == 1 and text.count(sha) == 1 and key not in text.replace(sha, ""), text
    for left in (f"commit {sha}", "DD_API_KEY is set", "DD_APP_KEY=${DD_APP_KEY}"):
        assert mod.redact(left) == (left, 0), left


def test_a_rules_file_of_comments_alone_keeps_the_plugins_rules(mem):
    """session memory: an empty project rules file never leaves a record without rules"""
    (mem.work / ".claude").mkdir()
    (mem.work / ".claude/session-memory-rules.md").write_text("<!-- to do: our own rules\n")
    assert mem.hook("PreCompact", PRE).returncode == 0
    stdin = mem.read("claude-stdin")
    assert 'say "a customer"' in stdin and "to do: our own rules" not in stdin
