---
name: stairway
description: Use the stairway MCP tools to turn a conversation into a running coding-agent experiment on the user's server (a folder plus a Claude Code, opencode, crush or openclaude session in it), brief it, pass it follow-up instructions, watch its progress, answer its permission questions and fetch what it built. Use when the user wants to "try it", "build it on the server", "spin up an experiment", or asks about, steers or collects results from an existing stairway experiment.
---

# stairway

stairway is an MCP server on the user's own machine. Each **experiment** is a folder (a git repo by default)
plus a coding agent running in it, inside a detached `screen`. The agent keeps working after this conversation
ends. The user can follow it in claude.ai/code (Claude Code only), in a browser VS Code, or in a Telegram group.

You are the **organizer**: you turn what was discussed into a brief, start the experiment, and pass on
instructions. The experiment's agent does the actual work in its own context. It cannot see this conversation,
so everything it needs must be in the prompts you send.

## The tools

| tool | use it to |
|---|---|
| `create_experiment(name, prompt?, agent?, model?, mode?, permission_mode?, …)` | make `<root>/<name>`, `git init` it, start the agent working on `prompt` |
| `attach_folder(path, name?, prompt?, …)` | use an existing folder. Nothing runs until the user approves it on the status page |
| `send_prompt(name, prompt)` | type a follow-up instruction into a running session (queued if the agent is busy) |
| `experiment_status(name)` | state, links, recent prompts, and what the agent's terminal shows right now |
| `list_experiments()` | every experiment: state, agent, mode, editor, folder |
| `answer_permission(name, allow)` | answer the Yes/No permission question the agent is waiting on |
| `start_experiment(name, prompt?, …)` / `stop_experiment(name)` | restart or stop the agent. A restart begins a **new conversation** |
| `open_editor(name)` / `stop_editor(name)` | a browser VS Code on the folder, for the user |
| `experiment_files(name, path?, max_mb?)` | the folder's files as a base64 zip (no `.git`, dependencies, or files over 5 MB) |
| `experiment_info(name)` | the same state as JSON, for programs |
| `forget_experiment(name)` | remove it from stairway. The folder is never deleted |

## Starting an experiment

1. **Agree on the brief first.** Before creating anything, make sure the goal is clear enough to act on. If
   the user just said "let's try it", summarize what you'll send and go; don't interrogate them.
2. **Pick a name:** lowercase letters, digits, `-` or `_`, at most 63 characters, short and descriptive
   (`rss-digest`, `trail-planner`). Call `list_experiments()` first if you're not sure it's free.
3. **Write the prompt as a standalone brief.** The agent starts from zero, so include:
   - the goal and what "done" looks like,
   - the decisions already made (language, libraries, data sources, constraints, things ruled out),
   - concrete first steps, and what to report back,
   - any URLs, sample data or snippets from this conversation that it needs.
4. **Call `create_experiment(name, prompt=…)`.** Keep the defaults unless there's a reason:
   - `agent`: `claude` (Claude Code). Use `opencode`, `crush` or `openclaude` only when the user asks for
     another agent or model; they run through the server's OpenAI-compatible provider, and `model` picks the
     model there.
   - `mode`: `session`, which is one interactive session that can receive `send_prompt`. `server` mode
     (`claude remote-control`) only spawns sessions from claude.ai/code. It can't take a prompt or follow-ups.
   - `permission_mode`: `default` makes the agent ask before edits and commands. Suggest `acceptEdits` when the
     user wants it to get on with things, and use `auto`, `dontAsk` or `bypassPermissions` only when the user
     explicitly asks for that.
5. **Give the user the links from the result:** the claude.ai/code session URL (Claude only) and, if they're
   at a computer, offer `open_editor`.

For an **existing project**, use `attach_folder(path, prompt=…)` instead. It only queues a request. Tell the
user to approve it on the status page (the result has the link). Once approved, the agent starts with your
prompt. Only folders under the server's allowed roots can be attached.

## Steering a running experiment

- **`send_prompt` passes on new instructions**, as if the user typed them. Like the first brief, make each
  prompt self-contained: say what changed in the discussion and what to do now. It only works in session mode
  while the experiment is running. If it's stopped, use `start_experiment(name, prompt=…)`, keeping in mind
  that the agent won't remember the earlier conversation.
- **`send_prompt` returns the screen a moment later, not the answer.** To see progress, call
  `experiment_status(name)` again after a while and read the terminal tail. Don't poll in a tight loop: a
  couple of checks, then tell the user it's working and where to watch it.
- **Long results** are better fetched as files than read off the terminal: ask the agent to write a report
  or deliverables into the folder (e.g. `deliverables/`), then use `experiment_files(name, path="deliverables")`.

## Permission questions

In `default` or `acceptEdits` mode the agent stops and waits when it wants to run a command (or, in
`default`, edit a file). `experiment_status` then shows a "Do you want to proceed?" prompt with the action.

- **Tell the user exactly what it wants to do** (the command or edit shown) and ask whether to allow it.
- Call `answer_permission(name, allow=true)` only after the user agrees, or when they've told you up front to
  approve this kind of action. Use `allow=false` to refuse; then say why with `send_prompt` if useful.
- Never approve destructive or outward-facing actions (deleting data, pushing, publishing, spending money,
  sending messages) on your own judgment.
- If the questions keep coming for harmless edits, suggest restarting with `permission_mode="acceptEdits"`.

## Things to keep in mind

- **Experiments run on a real machine** with the permissions of the stairway user. Treat prompts you send
  like commands you'd type on someone's server.
- **Folders are never deleted by stairway**, and `forget_experiment` only unregisters. Don't promise cleanup
  that didn't happen.
- **Telegram:** if a group is bound to the experiment, the agent's final reply for each turn is posted there,
  and its prompts may start with `[Telegram, <sender> in <group>]`. Your prompts arrive without that prefix.
- **Non-Claude agents** (`opencode`, `crush`, `openclaude`) need a provider API key on the server. If starting
  one fails with a key error, tell the user; don't retry with another agent unasked.
- **Errors are plain text** and say what to do (for example "already exists; use attach_folder", "is not
  running; use start_experiment"). Follow them rather than guessing.

## Example

User: *"OK, let's try the RSS digest idea on the server."*

```
create_experiment(
  name="rss-digest",
  prompt="""Build a small Python 3 script that reads the RSS feeds listed in feeds.txt, keeps items from the
last 24 hours, and writes digest.md grouped by feed with title, link and a one-line summary.
Decisions so far: standard library plus feedparser only; no database; dedupe by link.
Start with feeds.txt containing https://hnrss.org/frontpage and https://www.theverge.com/rss/index.xml.
When it works, run it once, commit, and reply with a short summary and the first lines of digest.md."""
)
```

Then share the claude.ai/code link from the result, and later check with `experiment_status("rss-digest")`
or collect the output with `experiment_files("rss-digest")`.
