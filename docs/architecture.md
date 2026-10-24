# Architecture

stairway is a single process (`stairway.py`) built on Python's `http.server`. Everything long-lived runs in
detached GNU `screen` sessions, so restarting stairway doesn't interrupt running experiments.

## Processes

| screen name | runs | started by |
|---|---|---|
| `stairway.<name>` | `claude --remote-control <name> …` (session mode) or `claude remote-control …` (server mode) | `create_experiment`, `start_experiment`, an approval, Telegram `/new` and `/start` |
| `stairway-code.<name>` | `code-server --socket state/code/<name>/code.sock …` | the first visit to `/code/<name>/`, or `open_editor` |

To look at a session from a shell, run `screen -x stairway.<name>`; press Ctrl-a d to detach. With the herdr
runtime (below), each experiment is instead a herdr workspace labelled `stairway.<name>` in the herdr session
`stairway`: run `herdr --session stairway` to see them all, or `herdr --session stairway agent attach <pane>`.

stairway answers Claude Code's one-time folder-trust question for registered folders. It handles both the
`[y/N]` prompt of `claude remote-control` and the menu of interactive sessions. A folder is registered only if
stairway created it, or you approved it.

## Files

```
state/
  experiments.json        name -> {path, origin, approved, mode, permission_mode, spawn, url, initial_prompt}
  prompts/<name>.jsonl    every prompt sent to the session (start, send), with a timestamp
  telegram.json           update offset, chats: {chat_id: {title, approved, experiment, reply_to}}
  code/<name>/            VS Code user data and terminal.sh (the terminal profile script)
  code-extensions/        extensions shared by all editors
```

Experiments themselves are plain folders, `git init`ed by default. stairway never deletes them.

## HTTP endpoints

| path | auth | purpose |
|---|---|---|
| `POST /mcp` | OAuth access token, or `Bearer <STAIRWAY_TOKEN>` | MCP (Streamable HTTP, stateless, JSON responses). Unauthenticated requests get a 401 with `WWW-Authenticate: Bearer resource_metadata=…` |
| `POST /mcp/<token>` | the token in the path | MCP for clients without OAuth |
| `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server` | none | OAuth discovery |
| `POST /register`, `GET+POST /authorize`, `POST /token` | none, the login cookie or the token on the consent page, PKCE | OAuth 2.1 with dynamic client registration |
| `/login`, `/logout` | the token | browser login (an HttpOnly, SameSite=Strict cookie) |
| `/status` | browser login | monitoring page and actions |
| `/code/<name>/…` | browser login | byte-level proxy to that experiment's code-server unix socket, websockets included |
| `/herdr/`, `/herdr/ws` | browser login; the websocket also requires a same-origin `Origin` | herdr's UI in the browser: an xterm.js page, and a websocket running `herdr --session <session>` in a pseudo-terminal |
| `POST /hooks/<name>/{stop,notify,file}?k=<key>` | per-experiment HMAC key | called by the session's Claude Code hooks |

### OAuth without storage

Registered clients, access tokens (24 hours) and refresh tokens (90 days) are HMAC-signed with
`STAIRWAY_TOKEN`; nothing is stored. Authorization codes live in memory for five minutes. Changing the token
invalidates every grant at once.

## Session mode in detail

The session is started as:

```
claude --remote-control <name> --permission-mode <mode> \
       --settings '{"theme": "dark-ansi", "hooks": {"Notification": [...]}}' \
       --append-system-prompt '<how to send files to the bound Telegram chat>' \
       '<first prompt>'
```

- **Prompts** are typed in as one bracketed paste: stairway runs `screen readbuf` and `paste` with
  `ESC[200~ … ESC[201~` around the text, then presses Enter. Multi-line text and special characters arrive
  intact, and Claude Code queues the prompt if it's busy.
- **The `Notification` hook** POSTs to `/hooks/<name>/notify`, and stairway relays permission requests. Idle notifications are dropped.
- **Files:** the system prompt tells Claude to `curl` `/hooks/<name>/file` with a path inside the experiment.
  stairway sends that file to the bound chats.
- **The ANSI theme** is there because `screen` 4.x misreads 24-bit color escape codes as other attributes,
  which shows up as red bars.

## Reply relay

A background thread polls every 3 seconds. For each running session-mode experiment bound to an approved
Telegram chat, it reads newly finished answers from the agent's own conversation store:

| agent | store | a finished answer is |
|---|---|---|
| claude | the newest `~/.claude/projects/<folder, with non-alphanumerics as ->/*.jsonl` | `assistant` entries with `stop_reason: end_turn`, posted once the file has been quiet for one poll |
| opencode | `~/.local/share/opencode/opencode.db` (sessions matched on `directory`) | an assistant message with `time.completed` that isn't a tool-call step; errors are posted as warnings |
| crush | `<folder>/.crush/crush.db` | an assistant message with `finished_at` whose finish reason isn't `tool_use`; errors are posted as warnings |
| openclaude | `state/agents/<name>/openclaude/projects/<folder, as for claude>/*.jsonl` | the same as claude: it writes Claude Code's transcript format |

On first sight of an experiment the watcher starts at the end, so history isn't replayed. A new Claude
transcript, meaning a restarted session, is read from its start. Because nothing depends on hooks, this works
for sessions started before the chat was bound.

## Other agents (opencode, crush, openclaude)

`agent=opencode` or `agent=crush` replaces Claude Code in the experiment's screen. stairway writes a
per-experiment config to `state/agents/<name>/`, which points the agent at `STAIRWAY_PROVIDER_BASE_URL` with
the chosen model:

- **opencode:** `OPENCODE_CONFIG=state/agents/<name>/opencode.json`. On OpenRouter it uses opencode's
  built-in `openrouter` provider; anything else gets an `@ai-sdk/openai-compatible` provider. The first prompt
  goes in with `--prompt`. `permission_mode` maps to opencode's `permission` settings for `edit`, `bash` and
  `webfetch`, and `plan` maps to `--agent plan`.
- **crush:** `CRUSH_GLOBAL_CONFIG` and `CRUSH_GLOBAL_DATA` point into `state/agents/<name>/`, with an
  `openai-compat` provider. crush has no prompt flag, so stairway types the first prompt in once the UI is up.
  `acceptEdits` pre-allows the file tools, and `auto`, `dontAsk` and `bypassPermissions` become `--yolo`.
- **openclaude** is a Claude Code fork, so it runs like a Claude session, with the same `--settings` (theme and
  `Notification` hook), `--append-system-prompt` and permission modes, but no remote control.
  `OPENCLAUDE_CONFIG_DIR=state/agents/<name>/openclaude` keeps its config, history and transcripts per
  experiment. stairway pre-sets the theme, marks onboarding as done and trusts the folder there, so no first-run
  dialogs appear. The provider goes in through `CLAUDE_CODE_USE_OPENAI=1`, `OPENAI_BASE_URL`, `OPENAI_API_KEY` and
  `OPENAI_MODEL`. openclaude needs Node 22 or later; stairway puts the binary's folder first on `PATH`, so a
  `node` installed next to it wins. On glibc older than 2.28, use an
  [unofficial `glibc-217` Node build](https://unofficial-builds.nodejs.org/download/release/).
- **opencode always draws in 24-bit color**, so it runs through `stairway.py tty256`. That small
  pseudo-terminal filter rewrites `ESC[38;2;r;g;bm` codes to the nearest xterm-256 color, because screen 4.x
  can't render 24-bit color. crush already respects `TERM=screen-256color`.
- **Agent processes get the provider key, but not stairway's own secrets** (`STAIRWAY_TOKEN` and the
  Telegram token). Claude sessions don't get the provider key either.

## The herdr runtime

`STAIRWAY_RUNTIME=herdr` runs agents in [herdr](https://herdr.dev), a terminal multiplexer that recognizes
coding agents and tracks their state (`working`, `blocked`, `done`, `idle`). stairway still talks to it only
through its CLI, so stairway itself stays dependency-free.

- **One headless server** in a named session (`STAIRWAY_HERDR_SESSION`, default `stairway`), started by
  stairway when needed. Like screen, it outlives stairway restarts.
- **One workspace per experiment**, labelled `stairway.<name>`. Its root pane runs
  `exec state/agents/<name>/run.sh`: a launcher (mode 700) that sets the agent's environment and `exec`s it. API
  keys therefore never appear in typed input or shell history. When the agent exits, its workspace closes.
- **Handles:** internally a running experiment is a screen session id or `herdr:<pane id>`. Reading the
  screen (`pane read --source visible`), typing prompts (`agent prompt`, which pastes the way each agent
  expects), sending keys and stopping (`workspace close`) go to whichever runtime started it. You can switch
  `STAIRWAY_RUNTIME` at any time, and experiments already running under the other runtime keep working.
- **States:** one `herdr api snapshot` per poll gives every pane and its agent state. The status page shows it
  as a chip.
  - opencode and crush have no `Notification` hook, so when herdr sees them turn `blocked`, stairway posts the
    "waiting" event to callbacks and Telegram itself. Claude Code and openclaude keep using their hook.
- **`send_prompt(…, wait_seconds=N)`** uses `agent prompt --wait`, which returns once the agent is idle,
  done or blocked, and then returns the terminal. If the agent is blocked, prompts are refused until the
  question is answered, instead of being typed into the dialog.
- **Agent hints:** openclaude runs with `HERDR_AGENT=claude`, because it draws Claude Code's UI. opencode runs
  with `HERDR_AGENT=opencode`, because it still runs inside `tty256`: opencode 1.4 draws nothing until it gets a
  fresh terminal and a first resize, which `tty256` provides.
- **Claude Code's renderer:** Claude Code picks its fullscreen renderer for herdr's terminal (libghostty), and
  that renderer hangs in a pane with no client attached. Sessions under herdr therefore get `"tui": "default"`
  in `--settings`.
- **On the web and phones:** `/herdr/` shows herdr's own interface. It is an xterm.js page (loaded from jsDelivr
  with subresource integrity) whose websocket runs a herdr client in a pseudo-terminal on the server. herdr
  switches to its single-column layout on narrow screens, and a key bar adds Esc, Tab, a sticky Ctrl, Ctrl-b
  (herdr's prefix), Ctrl-c and the arrows. Closing the page ends only that client; the agents keep running.
- **VS Code:** the "auto" terminal runs `herdr agent attach <pane>` (Ctrl-b q detaches) if no other terminal is
  attached; otherwise it opens a shell.
- herdr fetches updated agent-detection rules from herdr.dev. Set `[update] manifest_check = false` in
  `~/.config/herdr/config.toml` if you don't want that.

## VS Code terminals

`state/code/<name>/terminal.sh` is the default terminal profile ("auto"). In session mode, if no one is
attached to the Claude screen, it attaches with `screen -x`. Otherwise it starts a login shell in the folder.
The "claude session" profile always attaches, and the "shell" profile never does. stairway rewrites the
profiles each time the editor starts, and keeps any other settings.
