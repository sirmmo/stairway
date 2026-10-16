# Telegram mode

Bind a Telegram group to an experiment, and the group can talk to the experiment's Claude while discussing what
it builds.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Set `STAIRWAY_TELEGRAM_TOKEN=<token>` in `.env` and restart stairway. The log should show
   `telegram bot @<name> is polling`. stairway long-polls the Bot API, so no webhook or inbound port is needed.
3. Add the bot to a group. It replies that the chat must be approved.
4. Open `/status` and press **Approve** in the Telegram section.

Keep the bot's default privacy mode. The bot then only sees commands, mentions and replies to its own
messages, which is all stairway needs.

## Using it

| in the group | what happens |
|---|---|
| `/new <name> [claude\|opencode\|crush\|openclaude] [brief]` | creates a session-mode experiment (Claude Code unless you name another agent), binds it to this chat, and starts it on the brief |
| `/bind <name>` / `/unbind` | binds or unbinds an existing session-mode experiment; `/status` can do this too |
| `@bot …`, a reply to the bot, or `/ask …` | typed into the session as `[Telegram, <sender> in <group>] …`. If you reply to someone else's message, the quoted text goes along |
| a document or photo with a mention, or as a reply to the bot | saved to `telegram-uploads/` in the experiment, and Claude is told the path |
| `/status`, `/screen` | experiment state and links; the current terminal |
| `/approve`, `/deny` | answer the permission prompt Claude is showing (Enter or Esc) |
| `/ls [path]`, `/get <path>` | browse the experiment folder and download files (only inside the folder) |
| `/start [brief]`, `/stop` | start or stop the session |

Coming back from Claude:

- **Its final reply for each turn** is posted to every chat bound to the experiment, as a reply to the message
  that triggered it. Turns started from claude.ai/code or VS Code are posted too.
- **Permission requests** ("Claude needs your permission …") are posted with a hint to `/approve` or `/deny`.
- **Files and images** that Claude decides to share arrive as documents or photos.

Experiments created with `/new` use `STAIRWAY_TELEGRAM_PERMISSION_MODE`, which defaults to `acceptEdits`:
file edits happen on their own, and shell commands ask for approval.

## Notes

- Only session-mode experiments can be bound, because server mode has no single session to type into.
- Replies are read from the agent's own conversation store, so they're relayed for Claude Code, opencode,
  crush and openclaude alike, including sessions that were already running before you bound the chat.
- Permission-request notices need a Claude Code or openclaude session started by stairway 0.2 or later (it has
  the `Notification` hook).
- When a group becomes a supergroup, its chat id changes. stairway moves the binding automatically.
- Telegram limits bots to sending files of at most 50 MB, and stairway posts long replies in 4,000-character
  pieces.
