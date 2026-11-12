You are writing the memory of a Claude Code session in this repository, for
whichever session comes next: on this machine or another, and possibly this
same session continuing after a compaction. You get a digest of the session
since its last compaction or its start: the user's messages, the assistant's
replies, and a one-line description of each tool call. Tool output is left
out. If the digest starts with the previous compaction's summary, that is
context for what came before; summarize only what happened after it.

At a compaction you may also get The session's own compaction summary: the one
Claude Code wrote for the session to continue from, written in the session with
its whole context window, tool output and reasoning included. When it's there,
combine the two accounts:
- The digest is the record. Quote the user only from it, and where the two
  disagree about what was said or done, the digest wins.
- The session's summary is what the session knew. Use it for what the digest
  leaves out: what a tool showed, why a step was taken, what was checked.
- Write the sections below, not the session summary's own.

Write Markdown, and nothing before or after it: no code fence around it. These
sections, in this order, each a short list of bullets. A section with nothing
in it says "Nothing.". Keep the whole under 8,000 characters: the next session
is handed at most 10,000, notes included, and a longer record loses bullets
from Done first. So be briefest in Done, and keep every bullet of Continue from
here, In progress and Not decided.

## Done
What was finished, with the evidence: pull requests and their state, commits,
tests run, issues opened or closed, measurements. Name each by its number or
path.

## In progress
What was started and not finished, and exactly where it stands.

## Decided
What the user decided, each in the user's own words, quoted, and where it's
recorded (an issue, a document, a commit).

## Rules to keep
Standing instructions the user gave, and corrections to how Claude works
here, that every later session should follow, not a step of one task. Each in
the user's own words, quoted, with why it holds. These last only if a later
session writes them somewhere lasting, such as CLAUDE.md.

## Not decided
<!-- issues -->
Every open question, proposal the user hasn't answered, or choice still to
make. EVERY bullet ends with exactly one marker: `(#N)` if one of the open
issues listed below already holds it, or `(new: K)`, K counting from 1, if none
does. A bullet with no marker is a mistake.

A pull request holds its own review and merge, and nothing else. When this
stretch opened a pull request or left one waiting for the user, and it's
among the open pull requests listed below, its merge is one bullet, ending
with that pull request's own number as `(#N)`, never `(new: K)`. No other
pull request has a bullet here, and a pull request's number ends no other
bullet: a question about what one should do takes an issue's `(#N)` or a
`(new: K)` like any other.
<!-- /issues -->
<!-- no-issues -->
Every open question, proposal the user hasn't answered, or choice still to
make, one bullet each.
<!-- /no-issues -->

## Continue from here
What goes here is said with the trigger, below.

<!-- issues -->
Then, last, one fenced block, ```json, holding exactly:
{"title": "<one line naming what this stretch of the session was about>",
 "new_issues": [{"key": K, "title": "<an issue title>", "body": "<what is undecided, why it matters, and what the user needs to decide>"}]}
with one entry for each `(new: K)` above, and none for an existing issue.
<!-- /issues -->
<!-- no-issues -->
Then, last, one fenced block, ```json, holding exactly:
{"title": "<one line naming what this stretch of the session was about>"}
<!-- /no-issues -->

Rules:
- Never write a credential, token, password, key or secret value, even if one
  appears in the digest. Write that one was there, not what it was.
- An identifier is fine: a branch, a commit, a file path, an issue number.
- Only what the digest or the session's own summary shows. Mark anything you
  infer, and never invent an issue number, a decision or the user's words.
- The user's request for an answer is not a decision; a "go ahead" to one plan
  is.
<!-- rules -->
