# stairway

**Spin up Claude Code experiments on your own server, from a chat.**

stairway is a small MCP server: one Python file, standard library only. With it, Claude on claude.ai (or any
MCP client) can:

- create an experiment folder on your machine, or use an existing folder once you approve it,
- start a Claude Code session there with remote control on, briefed with what you just discussed,
- send it more instructions later.

Every experiment shows up in [claude.ai/code](https://claude.ai/code), so you can continue on the web, on your
phone, in a browser VS Code that stairway starts on demand, or from a Telegram group.

```
 you, in claude.ai  ──MCP──▶  stairway  ──▶  /srv/<experiment>/   (git repo)
   "let's try X"              (this repo)       └─ claude --remote-control  ◀── claude.ai/code, phone
                                  │                  ▲
                                  ├─ /status         │ prompts in, replies out
                                  ├─ /code/<name>/ ──┘ (VS Code, terminal joins the session)
                                  └─ Telegram bot ───▶ group chat about the experiment
```

## Features

- **MCP server for claude.ai.** It uses Streamable HTTP and has a built-in OAuth 2.1 server (discovery, dynamic
  client registration, PKCE), so it works as a claude.ai custom connector. Simpler clients can use a token
  instead.
- **Experiments from a chat.** `create_experiment(name, prompt=…)` creates `<root>/<name>`, runs `git init`, and
  starts Claude on your brief. `send_prompt` passes it further instructions.
- **Existing folders.** `attach_folder(path)` queues a request, and nothing runs until you approve it on the
  status page.
- **Status page.** It shows system load, memory and disk, every experiment and its terminal, pending approvals,
  forms to create or attach experiments, a prompt box, and copy-paste connection settings.
- **VS Code in the browser, on demand.** stairway proxies code-server per experiment, websockets included.
  The first terminal joins the Claude session; every other terminal is a shell in the folder.
  You can upload files by dragging them in.
- **Telegram mode.** Add the bot to a group and approve the group. Then talk to the experiment's Claude by
  mentioning the bot. Its replies, permission requests and files come back to the group.
- **Small and dependency-free.** About 1,700 lines of Python 3.8+ with no third-party packages. Claude sessions
  run inside GNU `screen`, so they survive stairway restarts.

## How it works

Each experiment is a folder plus a detached `screen` named `stairway.<name>` that runs Claude Code:

| run mode | what runs | receives prompts |
|---|---|---|
| `session` (default) | `claude --remote-control <name> [prompt]`: one interactive session | yes: `send_prompt`, `/status`, Telegram, VS Code terminal |
| `server` | `claude remote-control`: spawns sessions on demand from claude.ai/code | no |

stairway answers Claude Code's one-time "trust this folder?" question itself. It only does so for folders it
created, or that you approved. In session mode, Claude Code hooks report each finished reply and each
permission request back to stairway, which relays them to bound Telegram chats.

More detail: [docs/architecture.md](docs/architecture.md).

## Requirements

- Linux, Python 3.8 or later, GNU `screen`, `git` and `curl`
- [Claude Code](https://claude.com/claude-code), logged in with a claude.ai subscription. Remote control
  needs one.
- HTTPS in front of stairway (a reverse proxy or a tunnel), because claude.ai connectors require it
- Optional: [code-server](https://github.com/coder/code-server) 4.x for the browser VS Code
- Optional: a Telegram bot token from [@BotFather](https://t.me/BotFather)

## Quick start

```bash
git clone https://github.com/sirmmo/stairway.git /srv/stairway && cd /srv/stairway
cp .env.example .env
sed -i "s|^STAIRWAY_TOKEN=.*|STAIRWAY_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')|" .env
$EDITOR .env          # set STAIRWAY_PUBLIC_URL, roots, optional code-server and Telegram
chmod 600 .env

# try it in the foreground
set -a; . ./.env; set +a; python3 stairway.py

# or run it as a service
cp stairway.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now stairway
```

Put it behind HTTPS (see [docs/deployment.md](docs/deployment.md)). Then:

1. Open `https://<host>/status` and log in with the token.
2. In claude.ai, go to **Settings → Connectors → Add custom connector**, enter `https://<host>/mcp`, and
   press **Connect**. stairway's authorization page opens; press **Allow**.
3. In a chat, say something like *"create an experiment called `rss-digest` and brief it: build a script that
   …"*. A link to the new session comes back in the tool result.

## MCP tools

| tool | what it does |
|---|---|
| `create_experiment(name, prompt?, mode?, permission_mode?, spawn?, git_init?, start?)` | create `<root>/<name>` and start Claude, working on `prompt` right away |
| `attach_folder(path, name?, prompt?, mode?, permission_mode?, spawn?)` | ask to use an existing folder. It waits for your approval on `/status` |
| `send_prompt(name, prompt)` | type a prompt into a running session (it is queued if Claude is busy) |
| `start_experiment(name, prompt?, mode?, …)` / `stop_experiment(name)` | start or stop Claude. A restart starts a new conversation |
| `experiment_status(name)` / `list_experiments()` | state, links, recent prompts, the current terminal |
| `open_editor(name)` / `stop_editor(name)` | the experiment's browser VS Code |
| `forget_experiment(name)` | remove it from stairway; the folder is never deleted |

## Configuration

All settings are environment variables, usually set in `.env`:

| variable | default | meaning |
|---|---|---|
| `STAIRWAY_TOKEN` | required | the secret (16+ characters). It logs you in to the web pages, and signs OAuth grants and hook keys |
| `STAIRWAY_PUBLIC_URL` | none | public base URL, used in links and OAuth metadata |
| `STAIRWAY_EXPERIMENTS_ROOT` | `./experiments` | where new experiments are created |
| `STAIRWAY_ALLOW` | `/srv` | colon-separated roots that existing folders may be attached from |
| `STAIRWAY_STATE` | `./state` | registry, prompt logs, Telegram state, VS Code profiles |
| `STAIRWAY_HOST` / `STAIRWAY_PORT` | `127.0.0.1` / `8765` | listen address |
| `STAIRWAY_CLAUDE` | `claude` on PATH | Claude Code binary |
| `STAIRWAY_CLAUDE_THEME` | `dark-ansi` | theme for sessions. Keep an ANSI theme: screen 4 can't render 24-bit color |
| `STAIRWAY_CODE_SERVER` | newest `~/.local/lib/code-server-*`, else PATH | code-server binary |
| `STAIRWAY_TELEGRAM_TOKEN` | none | enables the Telegram bot |
| `STAIRWAY_TELEGRAM_PERMISSION_MODE` | `acceptEdits` | permission mode for experiments created from Telegram |
| `STAIRWAY_TELEGRAM_API` | `https://api.telegram.org` | Bot API base, mainly useful for testing |

## Security

stairway gives whoever holds the token, or an approved OAuth client, or an approved Telegram group, the ability
to run an AI agent that executes commands on your machine. Read [docs/security.md](docs/security.md) before you
expose it. In short:

- one strong token; changing it revokes every OAuth grant and hook key
- existing folders and Telegram chats need your explicit approval on `/status`
- choose the experiments' `permission_mode` deliberately; `default` makes Claude ask before it acts
- run it on a machine, or as a user, where that level of access is acceptable

## Documentation

- [docs/architecture.md](docs/architecture.md): processes, files, endpoints, the hook flow
- [docs/deployment.md](docs/deployment.md): systemd, reverse proxies (nginx, Traefik), code-server
- [docs/telegram.md](docs/telegram.md): setting up and using the bot
- [docs/security.md](docs/security.md): threat model and recommendations

## License

[MIT](LICENSE)
