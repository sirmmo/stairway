# Security

stairway exists to let an AI agent run on your machine on request. Anyone who can drive it can make Claude Code
read, write and execute things, with the permissions of the user stairway runs as. Treat it like SSH access.

## Who can drive it

| channel | credential | can do |
|---|---|---|
| MCP via OAuth (claude.ai, Claude Code) | you allowed the client on `/authorize`, which needs the token or a logged-in browser | everything the MCP tools offer |
| MCP via `/mcp/<token>` or `Bearer <token>` | the token | the same |
| Web pages (`/status`, `/code/…`, `/herdr/`) | a login cookie, obtained with the token | everything, plus a full VS Code with shells and herdr's UI (which can open shells too) |
| Telegram | membership in a chat you approved on `/status` | prompts, `/approve`, `/deny`, starting and stopping the bound experiment, reading and writing files inside it |
| Hooks (`/hooks/<name>/…`) | a per-experiment HMAC key, known to that experiment's Claude | post to that experiment's bound chats; send files from inside that experiment |

`STAIRWAY_TOKEN` is the root secret. Changing it logs out browsers and invalidates OAuth clients, access and
refresh tokens, and hook keys. Running sessions keep their old hook keys until you restart them.

## Built-in safeguards

- **Existing folders need approval.** `attach_folder` only queues a request, and only paths inside
  `STAIRWAY_ALLOW` are accepted, never an allowed root itself. Approving trusts the folder, including its
  `.claude` settings, hooks and MCP servers.
- **Telegram chats need approval** before the bot does anything there.
- **The trust question is answered only for registered folders**, that is, ones stairway created or you
  approved.
- **File access from Telegram** is confined to the experiment folder; paths are checked after resolving
  symlinks.
- **Login and OAuth hardening:** wrong tokens are delayed on login and on the consent page, and a wrong path
  token on `/mcp/<x>` gets a 404. OAuth requires PKCE (S256), exact redirect URI matching, and HTTPS
  redirect URIs (or localhost).
- **Logs redact** path tokens and hook keys.
- **Agent secrets stay out of typed input.** With the herdr runtime, the provider key reaches the agent through
  `state/agents/<name>/run.sh` (mode 700, inside the 700 `state/` folder), never through text typed into a
  pane. herdr's socket lives in `~/.config/herdr/sessions/<session>/`: anyone who can use it can drive the
  agents, just like attaching to their screen sessions.

## Recommendations

- **Use a long random token** (`secrets.token_urlsafe(32)`) and keep `.env` at mode 600.
- **Run stairway as a dedicated user** with access only to what experiments need, ideally on a dedicated
  machine or VM. The example unit runs as root for simplicity; don't keep that on a shared server.
- **Choose the permission mode per experiment.** `default` makes Claude ask before it edits files or runs
  commands. `acceptEdits` allows edits. `auto` and `bypassPermissions` let it act alone.
- **Only add the Telegram bot to groups whose members you'd give a shell.** `/approve` answers permission
  prompts.
- **Keep stairway on a private address** behind an HTTPS proxy.
- **Rotate the token** if it ever appears in a log, a chat or a screenshot.

## Reporting

Please report vulnerabilities privately through GitHub's
[security advisories](https://github.com/sirmmo/stairway/security/advisories/new) rather than in public issues.
