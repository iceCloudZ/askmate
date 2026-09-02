---
name: answer-partner
description: Answer your partner's questions through askmate — pull the inbox with a CLI, export question + attachment materials for your AI agent to read screenshots and logs, and every reply auto-settles into your knowledge base so your avatar answers the next one for you. Answer once, never again; multi-turn follow-ups merge into the final entry and the feedback loop calibrates quality automatically.
version: 1.3.0
tags: [answer, knowledge-base, partner, cli]
---

# Answer & grow your knowledge base (askmate)

When your partner asks a question, one CLI command pulls the inbox; questions with screenshots/logs can **export a materials folder for your AI agent** (read images, parse logs) to draft an answer; replying **auto-settles the answer into your knowledge base** so your avatar answers the next similar question. Multi-turn follow-ups stay in one thread — merge the final version back into the original entry.

## Setup (once)

Copy `askme.py` and `askme_gh.py` from the repo's `cli/` next to this SKILL.md. Then pick a backend:

**GitHub backend (recommended — no server)**:

```bash
askme login --backend github --gh-token <PAT> --gh-repo <owner>/askmate-data
```

(PAT = fine-grained token, scoped to the data repo, Contents: Read and write. Username = your GitHub login.)

**Self-hosted server backend** (login once, auto-relogin afterwards):

```bash
askme login --user <my-username> --password <password>
```

## Seeing incoming questions

```bash
askme inbox                 # OPEN = waiting for you (includes follow-ups escalated from avatar answers)
```

## Watching the inbox on a schedule (strongly recommended, one-time setup)

Run `askme inbox --notify` from a **scheduled task**: silent normally, a native desktop notification (Windows toast / macOS / Linux notify-send) only when something new arrives; already-seen items never re-notify.

**Windows** (admin CMD, every 15 min; adjust the path):

```bat
schtasks /Create /TN "askmate inbox" /SC MINUTE /MO 15 /TR "python C:\tools\askme.py inbox --notify"
```

**macOS / Linux** (crontab -e):

```
*/15 * * * * /usr/bin/python3 ~/tools/askme.py inbox --notify >/dev/null 2>&1
```

**Advanced (auto-drafting)**: feed new questions straight to a local agent for a draft, review before sending:

```bat
schtasks /Create /TN "askmate inbox agent" /SC MINUTE /MO 15 /TR "cmd /c python C:\tools\askme.py inbox --notify && zcode -p ""Check askmate inbox, for new items run inbox show --save-attachments, draft answers for my review, do not auto-reply"""
```

## Questions with attachments: export materials for your AI agent

```bash
askme inbox show 42 --save-attachments ./q42-materials
```

The export folder holds the asker's context.md / screenshots / raw logs (packed under the self-contained-context discipline, so it should be actionable as-is). Hand the folder to your local agent:

> This is a technical question; the materials folder has screenshots and logs. Read them and produce a reusable troubleshooting runbook (commands, order, conclusions) — I'll store it in the knowledge base for the avatar to reuse.

Review the draft, then reply — **a reply is knowledge settled**. Write it reusable, and the avatar answers verbatim next time.

## Replying (the core action)

```bash
askme reply 42 "the answer (agent-generated markdown works as-is)" --img arch.png
```

One command, three effects: thread → RESOLVED, **first reply auto-settles** into your knowledge base, and the avatar starts answering similar questions.

**Answering a question that already has an entry (auto-dedup)**: if the question already has an entry (e.g. the avatar answered it once and a follow-up sent it to review), the reply does not create a duplicate — the CLI tells you to merge:

```bash
askme kb edit <kbId> -a "the merged, complete answer" --status ACTIVE   # reviewed items must be re-ACTIVATED
```

Later replies in a multi-turn thread never duplicate either — merge the **final version** into the original entry with `kb edit`.

## The avatar and its quality signals

- Next similar question → avatar answers automatically (hits+1) or returns candidates
- **The asker must give feedback on every answer** (the CLI keeps reminding; `sent` lists pending ones):
  - `helpful` → knowledge confirmed
  - `not` / follow-up / escalate → the hit entry flips to **NEEDS_REVIEW** — your most important calibration signal

```bash
askme kb list --status NEEDS_REVIEW    # review queue; fix then `edit --status ACTIVE` to re-enable
```

Note: items under review are **disabled for the avatar** (no auto-answering, hidden from search) — clear the queue promptly.

## Managing your knowledge base

```bash
askme kb                                  # mine (hits = times the avatar answered for you)
askme kb search "keywords"                # verify search
askme kb push -q "question" -a "answer" [--tags redis,networking] [--alts "variant 1|variant 2"]
askme kb show <id>                        # full entry
askme kb show <id> --save out.md          # export as markdown
askme kb edit <id> -a "new answer"        # fix; --status ACTIVE re-enables after review
askme kb rm <id>                          # drop an outdated entry
```

## Upgrading the CLI

```bash
askme upgrade           # pull the latest zip and atomically replace askme.py (+ SKILL.md / askme_gh.py)
askme upgrade --check   # check only, shows the changelog
```

Auto-checks daily; see `~/.askme/config.json` for `upgrade_check` / `auto_upgrade`.

## Rhythm that works

1. Clear the inbox daily; use the materials folder + local agent for attachment-heavy questions
2. Write **reusable** answers, not context-bound fragments
3. Walk the NEEDS_REVIEW queue weekly — knowledge-base quality *is* avatar quality
