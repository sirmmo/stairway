# Architecture

stairway is a single process (`stairway.py`) built on Python's `http.server`. Everything long-lived runs in
detached GNU `screen` sessions, so restarting stairway doesn't interrupt running experiments.

## Processes

| screen name | runs | started by |
|---|---|---|
| `stairway.<name>` | `claude --remote-control <name> …` (session mode) or `claude remote-control …` (server mode) | `create_experiment`, `start_experiment`, an approval, Telegram `/new` and `/start` |
| `stairway-code.<name>` | `code-server --socket state/code/<name>/code.sock …` | the first visit to `/code/<name>/`, or `open_editor` |

To look at a session from a shell, run `screen -x stairway.<name>`; press Ctrl-a d to detach.

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
| `POST /hooks/<name>/{stop,notify,file}?k=<key>` | per-experiment HMAC key | called by the session's Claude Code hooks |

### OAuth without storage

Registered clients, access tokens (24 hours) and refresh tokens (90 days) are HMAC-signed with
`STAIRWAY_TOKEN`; nothing is stored. Authorization codes live in memory for five minutes. Changing the token
invalidates every grant at once.

## Session mode in detail

The session is started as:

```
claude --remote-control <name> --permission-mode <mode> \
       --settings '{"theme": "dark-ansi", "hooks": {"Stop": [...], "Notification": [...]}}' \
       --append-system-prompt '<how to send files to the bound Telegram chat>' \
       '<first prompt>'
```

- **Prompts** are typed in as one bracketed paste: stairway runs `screen readbuf` and `paste` with
  `ESC[200~ … ESC[201~` around the text, then presses Enter. Multi-line text and special characters arrive
  intact, and Claude Code queues the prompt if it's busy.
- **The `Stop` hook** POSTs the hook payload, which includes `last_assistant_message`, to
  `/hooks/<name>/stop`. stairway relays the message to bound Telegram chats.
- **The `Notification` hook** relays permission requests. Idle notifications are dropped.
- **Files:** the system prompt tells Claude to `curl` `/hooks/<name>/file` with a path inside the experiment.
  stairway sends that file to the bound chats.
- **The ANSI theme** is there because `screen` 4.x misreads 24-bit color escape codes as other attributes,
  which shows up as red bars.

## Other agents (opencode, crush)

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
- **opencode always draws in 24-bit color**, so it runs through `stairway.py tty256`. That small
  pseudo-terminal filter rewrites `ESC[38;2;r;g;bm` codes to the nearest xterm-256 color, because screen 4.x
  can't render 24-bit color. crush already respects `TERM=screen-256color`.
- **Agent processes get the provider key, but not stairway's own secrets** (`STAIRWAY_TOKEN` and the
  Telegram token). Claude sessions don't get the provider key either.

## VS Code terminals

`state/code/<name>/terminal.sh` is the default terminal profile ("auto"). In session mode, if no one is
attached to the Claude screen, it attaches with `screen -x`. Otherwise it starts a login shell in the folder.
The "claude session" profile always attaches, and the "shell" profile never does. stairway rewrites the
profiles each time the editor starts, and keeps any other settings.
