---
name: ask-partner
description: Ask your partner's AI avatar a question and get an instant answer from their knowledge base; unmatched questions land in their inbox for a human reply, with multi-turn follow-ups. Your agent packs self-contained context (error transcripts, code, logs, screenshots) so the other side never has to ask follow-up questions just to understand the problem.
version: 1.3.0
tags: [ask, partner, cli, human-in-the-loop]
---

# Ask your partner (askmate)

Throw a question at your partner's **AI avatar**: if their knowledge base has a strong match you get the answer **instantly**; a near match returns **candidates**; otherwise it lands in their inbox for a human reply — and you can **follow up in the same thread**.

Your AI agent (ZCode / Claude Code / etc.) drives the whole flow: collecting context, packing material, asking, searching, following up, and giving feedback.

## Setup (once)

This directory ships with `askme.py` (single file, pure Python stdlib, Win/Mac/Linux). Copy `askme.py` and `askme_gh.py` from the repo's `cli/` next to this SKILL.md. Below we use the alias `askme`; if you don't have one, replace `askme` with `python askme.py`.

Pick one data backend:

**GitHub backend (recommended — no server)**: your data is a **private GitHub repo** shared by both of you; every command is one commit — question history *is* the git log. Create a fine-grained PAT (GitHub → Settings → Developer settings → Fine-grained tokens) scoped to that repo with **Contents: Read and write**:

```bash
askme login --backend github --gh-token <PAT> --gh-repo <owner>/askmate-data
```

Your username is your GitHub login. Your partner logs into the same repo the same way. Route questions by GitHub login: `askme ask <partner-login> "…"`.

**Self-hosted server backend**:

```bash
askme login --user <my-username> --password <password>   # once; auto-relogin afterwards
```

Default server is `http://127.0.0.1:8730`; point `--server` (or env `ASKME_SERVER`) at your deployment.

## Before asking: self-contained context (the core discipline)

**Send everything in one shot; never rely on back-and-forth.** The other side should be able to start working immediately instead of asking "what's the error? which file? which version?". Your agent collects, before every question:

1. **Goal**: what you're trying to do; expected vs actual behavior
2. **Error transcripts**: stack traces / error messages **verbatim**, never paraphrased; last 50–100 lines of logs
3. **Relevant code**: file path + key snippet; read the files yourself and extract what actually matters
4. **Environment**: OS / language version / framework / branch or commit
5. **Already tried**: what was changed and what happened

**Packing action** (agent): write the above into `context.md` (clean markdown, fenced code blocks with language tags). Screenshots and raw logs travel as attachments, not inline:

```bash
askme ask <partner> "thread pool submit keeps timing out" \
    --file context.md \
    --file app.log \
    --img error.png
```

- Body: one-line topic + the single most telling line (e.g. the typical error). Details go into context.md
- `--img`: images (png/jpg/webp/gif, ≤5MB); `--file`: text (log/txt/md/json/py/java etc., ≤2MB); both repeatable
- Same discipline when following up: bring the **new** errors/code/logs, never just "still broken"

## Three outcomes and what to do

| `ask` returns | Meaning | What you / your agent do |
|---|---|---|
| `AUTO_ANSWERED` | avatar strong-hit the knowledge base | Verify the answer → **always feedback** `askme feedback <id> helpful\|not`; want a human answer → follow up in-thread `askme reply <id> "…"` |
| `CANDIDATES` | near match, top-5 returned | Evaluate each; if one fits use it and give feedback; otherwise run the search loop |
| `OPEN` | no match, sitting in partner's inbox | Run the search loop yourself; if nothing, wait for the human reply (they get the material) |

## Agent search loop (reword and retry; searching has no side effects)

```bash
askme kb search "index error" --owner <partner> --json
askme kb search "ES returns no data" --owner <partner>     # retry with different keywords
```

- `--owner` searches the partner's entire knowledge base (same surface the avatar answers from; items under review are not exposed)
- Nothing found → stay OPEN and wait for the human reply

## Multi-turn follow-ups

After they reply (or if the avatar's answer missed), append to the **same thread** — context is preserved:

```bash
askme reply <id> "tried your step 2, now it throws this: <verbatim>" --file new.log
```

- Your follow-up puts the thread back into their inbox
- Following up on an auto-answer automatically flags it for human review (the hit entry goes to NEEDS_REVIEW); no separate escalate step needed
- `askme sent` lists all your questions and their states

## Feedback (the mandatory last step)

**Every time you get an answer — avatar, search hit, or human — verify it and give feedback. Never end silently:**

```bash
askme feedback <id> helpful                      # answer was right: knowledge confirmed, keeps auto-answering
askme feedback <id> not --comment "index name was wrong"   # answer wrong: entry goes to NEEDS_REVIEW for the partner to fix
```

`askme sent` keeps listing unanswered-feedback items until you close them.

## Upgrading the CLI

```bash
askme upgrade           # pull the latest zip and atomically replace askme.py (+ SKILL.md / askme_gh.py)
askme upgrade --check   # check only, shows the changelog
```

The CLI auto-checks once a day and nudges on new versions. Disable: `"upgrade_check": false` in `~/.askme/config.json`; auto-apply: `"auto_upgrade": true`.

## Troubleshooting

- **Stays OPEN**: partner hasn't replied yet (they may run a scheduled inbox watcher)
- **Avatar/candidates all wrong**: follow up in-thread (auto-escalates to human) or `feedback not` so the entry gets reviewed
- **Unsupported attachment type**: images png/jpg/webp/gif; text log/txt/md/json/yaml/py/java/sql etc.
