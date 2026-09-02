# askmate

**Answer once — your avatar answers forever.**

askmate is a two-person Q&A system where **AI agents do the asking and answering**, and humans only step in when the knowledge base can't help. Built for two engineers who kept interrupting each other with questions — my wife and I.

```
partner's agent ── ask (packs self-contained context) ──▶  the data store
                                                           │ strong KB hit  → avatar answers instantly
partner's agent ◀── multi-turn follow-ups / feedback ────  │ near miss      → candidates
your agent      ◀── inbox (export materials for agent) ──  │ no match       → human inbox
                                                           └ every human reply auto-settles into the KB
```

[中文说明](README.zh-CN.md)

## Why this exists

Three ideas, none of them rocket science, which is exactly why no existing tool combines them:

**1. Agents ask, humans answer.** Existing human-in-the-loop tooling (e.g. [LangChain Agent Inbox](https://github.com/langchain-ai/agent-inbox)) handles *your own agent pausing to ask you*. askmate is the other direction: my agent asks **your** agent, your avatar answers from what you've taught it, and *you* are only paged when the knowledge base misses. Async by design — nobody blocks on anybody.

**2. Self-contained context, enforced by the skill.** The ask-side SKILL.md hard-codes a discipline the agent must follow before asking: goal, verbatim error transcripts, relevant code, environment, already-tried — packed into `context.md` plus raw screenshots/logs as attachments. One shot, no interrogative ping-pong. This turned out to be the single biggest quality lever of the whole system.

**3. A reply is knowledge settled.** The first human reply to any question is automatically distilled into a knowledge-base entry. The next person (or agent) asking the same thing gets an instant **avatar answer** with zero human involvement. "Not helpful" feedback, follow-ups, and escalations flip the entry to `NEEDS_REVIEW`, which **disables the avatar** for that entry until a human fixes and re-activates it. The KB grows exactly along the lines of what actually gets asked — no corpus bootstrapping, no RAG pipeline.

## Features

- **Multi-turn threads** — follow-ups reopen a resolved thread; context is never lost
- **Auto-dedup** — answering a question that already has an entry prompts a merge instead of spawning duplicates
- **Attachments** — screenshots ≤5MB, logs/code/text ≤2MB, embedded as capability links, stripped before KB indexing
- **Feedback loop** — mandatory (the CLI nags until you close it); calibrates the avatar
- **Two backends, one CLI**:
  - **GitHub-native** — the data store is a *private GitHub repo*; every command is one commit, so question history *is* the git log. Zero servers, zero databases, backup = `git clone`. Auth = fine-grained PAT, no password system at all
  - **Self-hosted server** — a single-file, pure-stdlib Python server + SQLite (`server/server.py`), systemd + reverse proxy for TLS
- **Self-upgrading CLI** — `askme upgrade` checks daily, shows the changelog, and atomically replaces itself (served by the optional skill-dist channel on the self-hosted backend)

## Quickstart — GitHub backend (5 minutes, no server)

1. Create a **private** repo, e.g. `askmate-data`; add your partner as a collaborator
2. Each of you creates a fine-grained PAT: GitHub → Settings → Developer settings → **Fine-grained tokens** → scope it to `askmate-data`, permission **Contents: Read and write**
3. Each of you installs the CLI next to a SKILL.md (see [skills/](skills/)) and logs in:

   ```bash
   askme login --backend github --gh-token <PAT> --gh-repo <owner>/askmate-data
   ```

4. Ask across the wire:

   ```bash
   askme ask <partner-github-login> "why does my thread pool keep timing out" \
       --file context.md --file app.log --img error.png
   ```

The repo initializes itself on first login. That's it — your agents now have a shared brain.

## Quickstart — self-hosted server

```bash
git clone https://github.com/<you>/askmate && cd askmate
./server/deploy.sh <your-server> <ssh-user>     # probe → upload → systemd → health check
# follow the printed checklist: adduser ×2, DNS, TLS (see server/reverse-proxy.md)
```

Then on both machines: `askme login --user <name> --password <pw>` (server defaults to `http://127.0.0.1:8730`; override with `--server` / `ASKME_SERVER`).

## CLI cheatsheet

```
askme login            # --backend github --gh-token … --gh-repo …  |  --user … --password …
askme whoami
askme ask <user> "q" [--img …] [--file …] [--json]   # avatar/candidates/inbox routing
askme inbox [--notify]                # --notify: desktop notification on new items (cron-friendly)
askme inbox show <id> [--save-attachments DIR]       # export materials for your local agent
askme reply <id> "…"                  # answer (addressee) or follow-up (asker) — auto-detected
askme sent                            # my questions + pending-feedback nagging
askme feedback <id> helpful|not       # the mandatory last step
askme kb list|search|show|push|edit|rm
askme kb search "kw" --owner <user>   # search the partner's KB (same surface the avatar uses)
askme upgrade [--check]
```

## How a question flows (state machine)

```
ask ──▶ strong KB hit? ── yes ─▶ AUTO_ANSWERED (avatar replies, hits+1)
              │                        │ follow-up / escalate / feedback:not
              no                       ▼
              └──▶ OPEN (human inbox) ─ reply ─▶ RESOLVED (first reply settles into KB)
                        ▲                        │ asker follows up
                        └────────────────────────┘
NEEDS_REVIEW: entry disabled for the avatar until kb edit --status ACTIVE
```

## Repo layout

```
skills/ask-partner/SKILL.md      the asker's agent instructions (context-packing discipline)
skills/answer-partner/SKILL.md   the answerer's agent instructions (inbox → agent → settle)
cli/askme.py                     single-file CLI, pure stdlib, Python 3.8+
cli/askme_gh.py                  GitHub backend (Contents API, optimistic locking, client-side state machine)
server/server.py                 optional single-file server (stdlib + SQLite, ~800 lines)
server/deploy.sh                 one-command deploy (systemd + health check)
scripts/publish.py               builds the self-upgrade zips for the optional dist channel
```

Install for an agent (ZCode / Claude Code / etc.): create a folder in your agent's skills directory with three files — `SKILL.md` (pick your side from [skills/](skills/)) plus `askme.py` and `askme_gh.py` from [cli/](cli/).

## How it compares

| | askmate | [Agent Inbox](https://github.com/langchain-ai/agent-inbox) | digital-twin projects | [A2A](https://github.com/a2aproject/a2a) |
|---|---|---|---|---|
| Direction | my agent → *your* avatar → (rarely) you | my agent → me | humans → twin, then twin answers | agent ↔ agent protocol |
| Knowledge growth | every human reply auto-settles | n/a (approval UX) | corpus fed up-front | n/a (transport) |
| Infrastructure | a git repo, or one stdlib file | LangGraph stack | varies | protocol + runtimes |
| Cold start | day one: all human, KB grows from use | — | heavy prep | — |

## Honest limitations

- The GitHub backend executes the state machine client-side — fine because both sides are collaborators on the same private repo and the git history is the audit log, but it's cooperation, not adversarial security
- Search is intentionally keyword/prefix-based (grep-style, no embeddings) — agentic search with rewording loops works better than vector recall for this scale, but don't expect semantic fuzz at 10k entries
- UI is terminal + CLI; the web is only mentioned in the roadmap
- CLI/server user-facing strings are currently Chinese (the origin users are); the SKILL.md docs and READMEs are English. i18n is the top of the roadmap — PRs very welcome

## Roadmap

- [ ] English UI strings (i18n)
- [ ] Expose ask/inbox/reply as an **MCP server** so any MCP client can use it without installing a skill
- [ ] A2A transport for the ask/reply primitives
- [ ] Minimal web inbox (read-only first)

## License

[MIT](LICENSE)
