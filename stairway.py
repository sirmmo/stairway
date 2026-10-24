#!/usr/bin/env python3
"""stairway - a tiny, dependency-free MCP server (Streamable HTTP, JSON responses).

It lets Claude on claude.ai create experiment folders on this machine (or attach existing
ones, after you approve them) and run a `claude remote-control` instance in each one, inside
a detached GNU screen, so the experiment shows up in claude.ai/code.

It also serves, behind a browser login:
  /status          a monitoring page: system load, experiments, approvals, actions
  /code/<name>/    an on-demand VS Code (code-server) on the experiment folder, whose
                   default terminal is attached to the remote-control screen

Config (env):
  STAIRWAY_TOKEN        required; secret. MCP endpoint is /mcp/<token> (or /mcp + Bearer header);
                        the same token logs you in to the web pages
  STAIRWAY_EXPERIMENTS_ROOT  the experiments root: every new experiment is a folder in here
                        (default: ./experiments, next to this file)
  STAIRWAY_STATE        registry and editor data (default: ./state)
  STAIRWAY_ALLOW        colon-separated folders inside which existing folders may be attached (default: /srv)
  STAIRWAY_PUBLIC_URL   public base URL, used in links returned to Claude (e.g. https://stairway.example.org)
  STAIRWAY_HOST         bind address (default: 127.0.0.1)
  STAIRWAY_PORT         bind port (default: 8765)
  STAIRWAY_CLAUDE       claude binary (default: claude on PATH)
  STAIRWAY_CLAUDE_THEME Claude theme for sessions; must be an ANSI one for screen (default: dark-ansi)
  STAIRWAY_TELEGRAM_TOKEN  optional Telegram bot token: chats approved on /status can drive session experiments
  STAIRWAY_TELEGRAM_PERMISSION_MODE  permission mode for experiments made with /new (default: acceptEdits)
  STAIRWAY_PROVIDER_BASE_URL  OpenAI-compatible API for opencode/crush/openclaude (default: https://openrouter.ai/api/v1)
  STAIRWAY_PROVIDER_API_KEY   its API key (OPENROUTER_API_KEY also works)
  STAIRWAY_PROVIDER_MODEL     default model for the other agents (default: deepseek/deepseek-v4.1-flash)
  STAIRWAY_OPENCODE / STAIRWAY_CRUSH / STAIRWAY_OPENCLAUDE  agent binaries (default: on PATH, or
                        ~/.opencode/bin/opencode, or the newest ~/.local/lib/node-*/bin/openclaude)
  STAIRWAY_RUNTIME      where agents run: screen (default) or herdr (https://herdr.dev: agent states for
                        every agent, prompt-and-wait); STAIRWAY_HERDR / STAIRWAY_HERDR_SESSION (default: stairway)
  STAIRWAY_CODE_SERVER  code-server binary (default: newest ~/.local/lib/code-server-*/bin/code-server,
                        else code-server on PATH)
"""
import base64
import glob
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shlex
import select
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlencode, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = os.environ.get("STAIRWAY_TOKEN", "")
ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("STAIRWAY_EXPERIMENTS_ROOT") or os.path.join(HERE, "experiments")))
STATE = os.path.abspath(os.environ.get("STAIRWAY_STATE", os.path.join(HERE, "state")))
ALLOW = [os.path.realpath(p) for p in os.environ.get("STAIRWAY_ALLOW", "/srv").split(":") if p]
PUBLIC_URL = os.environ.get("STAIRWAY_PUBLIC_URL", "").rstrip("/")
HOST = os.environ.get("STAIRWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("STAIRWAY_PORT", "8765"))
CLAUDE = os.environ.get("STAIRWAY_CLAUDE") or shutil.which("claude") or "claude"
# screen 4 can't parse 24-bit colors (they turn into red bars), so sessions use an ANSI-only theme
CLAUDE_THEME = os.environ.get("STAIRWAY_CLAUDE_THEME", "dark-ansi")
# Other coding agents, driven through an OpenAI-compatible provider (OpenRouter by default)
OPENCODE = (os.environ.get("STAIRWAY_OPENCODE") or shutil.which("opencode")
            or next((p for p in [os.path.expanduser("~/.opencode/bin/opencode")] if os.path.exists(p)), None))
CRUSH = os.environ.get("STAIRWAY_CRUSH") or shutil.which("crush")
# openclaude (a Claude Code fork for OpenAI-compatible APIs) needs Node 22+: its bin folder goes first on PATH
OPENCLAUDE = (os.environ.get("STAIRWAY_OPENCLAUDE") or shutil.which("openclaude")
              or (sorted(glob.glob(os.path.expanduser("~/.local/lib/node-*/bin/openclaude"))) or [None])[-1])
PROVIDER_BASE_URL = os.environ.get("STAIRWAY_PROVIDER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
PROVIDER_API_KEY = os.environ.get("STAIRWAY_PROVIDER_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
PROVIDER_MODEL = os.environ.get("STAIRWAY_PROVIDER_MODEL", "deepseek/deepseek-v4.1-flash")
RUNTIME = os.environ.get("STAIRWAY_RUNTIME", "screen")
HERDR = os.environ.get("STAIRWAY_HERDR") or shutil.which("herdr") or next(
    (p for p in [os.path.expanduser("~/.local/bin/herdr")] if os.path.exists(p)), None)
HERDR_SESSION = os.environ.get("STAIRWAY_HERDR_SESSION", "stairway")
CODE_SERVER = (os.environ.get("STAIRWAY_CODE_SERVER")
               or (sorted(glob.glob(os.path.expanduser("~/.local/lib/code-server-*/bin/code-server"))) or [None])[-1]
               or shutil.which("code-server"))

SERVER_INFO = {"name": "stairway", "version": "0.5.0"}
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
PERMISSION_MODES = ["default", "acceptEdits", "auto", "plan", "dontAsk", "bypassPermissions"]
SPAWN_MODES = ["same-dir", "worktree", "session"]
RUN_MODES = ["session", "server"]
AGENTS = ["claude", "opencode", "crush", "openclaude"]
URL_RE = re.compile(r"https://claude\.ai/code[^\s'\")\]]*")
MAX_PROMPT = 100000
RC_PREFIX = "stairway."
CODE_PREFIX = "stairway-code."
REGISTRY = os.path.join(STATE, "experiments.json")
STARTED = time.time()

_lock = threading.RLock()


def _link(path):
    return (PUBLIC_URL or "<stairway host>") + path


# ---------------------------------------------------------------- registry
# experiments.json: name -> {path, origin: created|attached, approved, mode: session|server,
#                            permission_mode, spawn, url, initial_prompt}
# mode "session": one interactive `claude --remote-control` session; prompts can be typed into it.
# mode "server":  `claude remote-control`, which spawns sessions on demand from claude.ai/code.

def _load():
    try:
        with open(REGISTRY) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(reg):
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w") as f:
        json.dump(reg, f, indent=2, sort_keys=True)
    os.replace(tmp, REGISTRY)


def _get(name, approved=True):
    if not NAME_RE.match(name or ""):
        raise ValueError("name must match %s" % NAME_RE.pattern)
    entry = _load().get(name)
    if not entry:
        raise ValueError("no experiment named %r (see list_experiments)" % name)
    if approved and not entry.get("approved"):
        raise ValueError("%r is waiting for approval on %s" % (name, _link("/status")))
    return entry


def _update(name, **fields):
    with _lock:
        reg = _load()
        if name in reg:
            reg[name].update(fields)
            _save(reg)


# ---------------------------------------------------------------- screen helpers

def _screens(prefix):
    """Map name -> screen session id ("pid.prefixname") for live screens with this prefix."""
    out = subprocess.run(["screen", "-ls"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         universal_newlines=True).stdout
    return {m.group(2): m.group(1)
            for m in re.finditer(r"^\s*(\d+\.%s(\S+))\s" % re.escape(prefix), out, re.M)}


_GLYPHS = {"\x14": "—", "\x19": "'", "\x18": "'", "\x1c": '"', "\x1d": '"'}


def _tidy(text):
    """Screen text for people: hardcopy turns box rules into "LLLL…" and some punctuation into control bytes."""
    out = []
    for line in text.splitlines():
        line = "".join(_GLYPHS.get(c, "" if ord(c) < 32 and c != "\t" else c) for c in line).rstrip()
        if len(line) >= 20 and len(set(line.strip())) == 1:
            continue
        out.append(line)
    return "\n".join(out).strip()


def _screen_text(session):
    if session.startswith("herdr:"):
        # the visible screen: agents draw on the alternate screen, which never reaches scrollback
        text = _herdr_cli("pane", "read", session[6:], "--source", "visible", raw=True)
        lines = [l.rstrip() for l in text.splitlines()]
        return "\n".join(l for i, l in enumerate(lines) if l or (i and lines[i - 1]))[-4000:].strip()
    fd, path = tempfile.mkstemp(prefix="stairway-")
    os.close(fd)
    try:
        subprocess.run(["screen", "-S", session, "-X", "hardcopy", "-h", path], timeout=5)
        with open(path, errors="replace") as f:
            raw = f.read().replace("\x00", "").replace("\ufffd", "·")  # hardcopy pads and mangles glyphs
        lines = [l.rstrip() for l in raw.splitlines()]
        return "\n".join(l for i, l in enumerate(lines) if l or (i and lines[i - 1]))[-4000:].strip()
    finally:
        os.unlink(path)


def _session_url(text):
    m = URL_RE.search(re.sub(r"(?m)^(.{80})\n", r"\1", text))  # rejoin lines wrapped at 80 columns
    return m.group(0) if m else None


def _quit(session):
    if session.startswith("herdr:"):
        _herdr_cli("workspace", "close", session[6:].split(":")[0])
        return
    subprocess.run(["screen", "-S", session, "-X", "quit"])


def _sessions():
    """Map name -> handle of every running experiment, whichever runtime it was started in."""
    found = _screens(RC_PREFIX)
    found.update(_herdr_panes())
    return found


# ---------------------------------------------------------------- herdr runtime (STAIRWAY_RUNTIME=herdr)

_herdr_status = {}  # name -> agent state herdr last reported (idle, working, blocked, done, unknown)


def _herdr_cli(*args, raw=False, timeout=30):
    """Run a herdr command against stairway's own session; its JSON result, or the raw output."""
    res = subprocess.run([HERDR, "--session", HERDR_SESSION] + list(args), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True, timeout=timeout, env=_child_env())
    if res.returncode:
        try:
            err = json.loads(res.stderr)["error"]
            msg = "%s: %s" % (err.get("code"), err.get("message"))
        except (ValueError, KeyError, TypeError):
            msg = res.stderr.strip() or "exit status %d" % res.returncode
        raise ValueError("herdr %s failed: %s" % (" ".join(args[:2]), msg))
    return res.stdout if raw else (json.loads(res.stdout).get("result", {}) if res.stdout.strip() else {})


def _herdr_socket():
    return os.path.expanduser("~/.config/herdr/sessions/%s/herdr.sock" % HERDR_SESSION)


def _herdr_panes():
    """name -> "herdr:<pane>" for experiments running in herdr (one workspace each, labelled stairway.<name>)."""
    if not HERDR or not _socket_alive(_herdr_socket()):
        return {}
    try:
        snap = _herdr_cli("api", "snapshot", timeout=10)["snapshot"]
    except Exception as e:
        sys.stderr.write("stairway: herdr snapshot: %s\n" % e)
        return {}
    labels = {w["workspace_id"]: w.get("label", "")[len(RC_PREFIX):] for w in snap.get("workspaces") or []
              if w.get("label", "").startswith(RC_PREFIX)}
    out = {}
    for pane in snap.get("panes") or []:
        name = labels.get(pane.get("workspace_id"))
        if name and name not in out:
            out[name] = "herdr:" + pane["pane_id"]
            _herdr_status[name] = pane.get("agent_status") if pane.get("agent") else None
    return out


def _herdr_server():
    """Start stairway's headless herdr server unless it is up. It outlives stairway, like screen does."""
    if not HERDR:
        raise ValueError("STAIRWAY_RUNTIME=herdr, but herdr is not installed; set STAIRWAY_HERDR")
    if _socket_alive(_herdr_socket()):
        return
    subprocess.Popen([HERDR, "--session", HERDR_SESSION, "server"], env=_child_env(), cwd=STATE,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    deadline = time.time() + 15
    while time.time() < deadline:
        time.sleep(0.3)
        if _socket_alive(_herdr_socket()):
            return
    raise ValueError("the herdr server did not come up within 15s")


def _herdr_start(name, path, cmd, extra_env, agent):
    """Run cmd in a new herdr workspace. A private launcher script carries the environment, so secrets never
    appear in typed input or shell history; the workspace closes when the agent exits."""
    _herdr_server()
    # herdr can't see the agent behind a wrapper (opencode runs inside tty256, whose fresh terminal and
    # initial resize opencode 1.4 also needs to draw at all) or know that openclaude looks like Claude Code
    hint = {"openclaude": "claude", "opencode": "opencode"}.get(agent)
    if hint:
        extra_env = dict(extra_env, HERDR_AGENT=hint)
    os.makedirs(_agent_dir(name), exist_ok=True)
    script = os.path.join(_agent_dir(name), "run.sh")
    fd = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
    with os.fdopen(fd, "w") as f:
        f.write("#!/bin/sh\n# written by stairway for %s\ncd %s || exit 1\n" % (name, shlex.quote(path)))
        f.write("".join("export %s=%s\n" % (k, shlex.quote(v)) for k, v in sorted(extra_env.items())))
        f.write("exec %s\n" % " ".join(shlex.quote(c) for c in cmd))
    os.chmod(script, 0o700)
    ws = _herdr_cli("workspace", "create", "--cwd", path, "--label", RC_PREFIX + name, "--no-focus")
    pane = ws["root_pane"]["pane_id"]
    _herdr_cli("pane", "run", pane, "exec " + shlex.quote(script))
    return pane


# ---------------------------------------------------------------- remote-control instances

def _prompt_log(name):
    return os.path.join(STATE, "prompts", name + ".jsonl")


def _log_prompt(name, source, text):
    os.makedirs(os.path.dirname(_prompt_log(name)), exist_ok=True)
    with open(_prompt_log(name), "a") as f:
        f.write(json.dumps({"at": int(time.time()), "source": source, "text": text}) + "\n")


def _prompts(name, last=10):
    try:
        with open(_prompt_log(name)) as f:
            return [json.loads(l) for l in f.read().splitlines()[-last:]]
    except FileNotFoundError:
        return []


def _check_prompt(prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT:
        raise ValueError("prompt is longer than %d characters" % MAX_PROMPT)
    return prompt


def _check_agent(agent, mode):
    if agent not in AGENTS:
        raise ValueError("agent must be one of %s" % AGENTS)
    if agent != "claude" and mode == "server":
        raise ValueError("server mode is Claude-only (claude remote-control); %s runs in session mode" % agent)


def _check_run(mode, prompt):
    if mode not in RUN_MODES:
        raise ValueError("mode must be one of %s" % RUN_MODES)
    if prompt:
        _check_prompt(prompt)
        if mode != "session":
            raise ValueError("prompts need mode=session; server mode starts sessions from claude.ai/code")


def _child_env():
    """Environment for agent processes: without markers of a Claude session that started stairway, and
    without stairway's own secrets."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "STAIRWAY_TOKEN", "STAIRWAY_TELEGRAM_TOKEN",
                        "STAIRWAY_PROVIDER_API_KEY", "OPENROUTER_API_KEY") and not k.startswith("CLAUDE_CODE_")}
    return env


def _agent_dir(name):
    return os.path.join(STATE, "agents", name)


def _agent_command(agent, name, path, permission_mode, model, prompt):
    """Return (argv, extra env, prompt still to type) for a non-Claude agent; configs live in state/agents/<name>."""
    if not PROVIDER_API_KEY:
        raise ValueError("%s needs an API key: set STAIRWAY_PROVIDER_API_KEY (provider %s)" % (agent, PROVIDER_BASE_URL))
    d = _agent_dir(name)
    os.makedirs(d, exist_ok=True)
    env = {"STAIRWAY_PROVIDER_API_KEY": PROVIDER_API_KEY}
    trusting = permission_mode in ("auto", "dontAsk", "bypassPermissions")
    if agent == "opencode":
        if not OPENCODE:
            raise ValueError("opencode is not installed; set STAIRWAY_OPENCODE")
        if "openrouter.ai" in PROVIDER_BASE_URL:
            pid, provider = "openrouter", {"options": {"apiKey": "{env:STAIRWAY_PROVIDER_API_KEY}"}, "models": {model: {}}}
            env["OPENROUTER_API_KEY"] = PROVIDER_API_KEY
        else:
            pid, provider = "stairway", {"npm": "@ai-sdk/openai-compatible", "name": "stairway provider",
                                         "options": {"baseURL": PROVIDER_BASE_URL,
                                                     "apiKey": "{env:STAIRWAY_PROVIDER_API_KEY}"},
                                         "models": {model: {"name": model}}}
        ask = "allow" if trusting else "ask"
        config = {"$schema": "https://opencode.ai/config.json", "autoupdate": False, "share": "disabled",
                  "provider": {pid: provider}, "model": "%s/%s" % (pid, model),
                  "permission": {"edit": "allow" if trusting or permission_mode == "acceptEdits" else "ask",
                                 "bash": ask, "webfetch": ask}}
        cfg = os.path.join(d, "opencode.json")
        with open(cfg, "w") as f:
            json.dump(config, f, indent=2)
        env.update(OPENCODE_CONFIG=cfg, OPENCODE_DISABLE_AUTOUPDATE="1")
        # opencode always draws in 24-bit color, which screen 4 can't show: run it through the tty256 filter
        argv = [sys.executable, os.path.abspath(__file__), "tty256", OPENCODE, path, "--model", "%s/%s" % (pid, model)]
        if permission_mode == "plan":
            argv += ["--agent", "plan"]
        return argv + (["--prompt", prompt] if prompt else []), env, None
    if agent == "crush":
        if not CRUSH:
            raise ValueError("crush is not installed; set STAIRWAY_CRUSH")
        selected = {"model": model, "provider": "stairway"}
        config = {"$schema": "https://charm.land/crush.json",
                  "providers": {"stairway": {"name": urlsplit(PROVIDER_BASE_URL).hostname, "type": "openai-compat",
                                             "base_url": PROVIDER_BASE_URL, "api_key": "$STAIRWAY_PROVIDER_API_KEY",
                                             "discover_models": True,
                                             "models": [{"id": model, "name": model, "context_window": 128000,
                                                         "default_max_tokens": 16000}]}},
                  "models": {"large": selected, "small": selected},
                  "options": {"disable_metrics": True, "disable_provider_auto_update": True}}
        if permission_mode == "acceptEdits":
            config["permissions"] = {"allowed_tools": ["view", "ls", "grep", "glob", "edit", "multiedit", "write"]}
        os.makedirs(os.path.join(d, "crush-config"), exist_ok=True)
        with open(os.path.join(d, "crush-config", "crush.json"), "w") as f:
            json.dump(config, f, indent=2)
        env.update(CRUSH_GLOBAL_CONFIG=os.path.join(d, "crush-config"), CRUSH_GLOBAL_DATA=os.path.join(d, "crush-data"))
        return [CRUSH, "--cwd", path] + (["--yolo"] if trusting else []), env, prompt  # no prompt flag: type it
    if agent == "openclaude":
        if not OPENCLAUDE:
            raise ValueError("openclaude is not installed; set STAIRWAY_OPENCLAUDE")
        home = os.path.join(d, "openclaude")
        os.makedirs(home, exist_ok=True)
        # its own config home: no first-run theme picker, and the folder is trusted (stairway created or you approved it)
        cfg = os.path.join(home, ".openclaude.json")
        try:
            with open(cfg) as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
        config.update(hasCompletedOnboarding=True, theme=CLAUDE_THEME)
        config.setdefault("projects", {}).setdefault(path, {})["hasTrustDialogAccepted"] = True
        with open(cfg, "w") as f:
            json.dump(config, f, indent=2)
        env.update(OPENCLAUDE_CONFIG_DIR=home, CLAUDE_CODE_USE_OPENAI="1", OPENAI_API_KEY=PROVIDER_API_KEY,
                   OPENAI_BASE_URL=PROVIDER_BASE_URL, OPENAI_MODEL=model, DISABLE_AUTOUPDATER="1",
                   PATH=os.path.dirname(OPENCLAUDE) + os.pathsep + os.environ.get("PATH", ""))
        return ([OPENCLAUDE, "--permission-mode", permission_mode, "--settings", json.dumps(_session_settings(name)),
                 "--append-system-prompt", _session_system_prompt(name)] + ([prompt] if prompt else [])), env, None
    raise ValueError("agent must be one of %s" % AGENTS)


_HERDR_KEYS = {"\r": "enter", "\033": "esc", "\033[B": "down", "\033[A": "up"}


def _stuff(session, keys):
    if session.startswith("herdr:"):
        for part in re.findall(r"\033\[[AB]|\033|\r|[^\033\r]+", keys):
            if part in _HERDR_KEYS:
                _herdr_cli("pane", "send-keys", session[6:], _HERDR_KEYS[part])
            else:
                _herdr_cli("pane", "send-text", session[6:], part)
        return
    subprocess.run(["screen", "-S", session, "-X", "stuff", keys], check=True)


def _answer_trust(session, text):
    """Accept the one-time folder trust question; True if one was on screen.

    Every registered folder was either created by stairway or approved by you on /status."""
    if "[y/N]" in text and "Trust " in text:  # `claude remote-control`
        _stuff(session, "y\r")
        return True
    yes = [l for l in text.splitlines() if "Yes, I trust this folder" in l]
    if yes:  # interactive session: a menu whose default is "No, exit"
        if yes[-1].strip().startswith("Yes"):  # not the highlighted entry
            _stuff(session, "\033[B")
            time.sleep(0.5)
        _stuff(session, "\r")
        return True
    return False


def _type_prompt(session, prompt):
    """Type a prompt into an interactive session as one bracketed paste, then press Enter."""
    if session.startswith("herdr:"):
        try:
            _herdr_cli("agent", "prompt", session[6:], prompt)  # herdr pastes it the way the agent expects
        except ValueError as e:
            if "agent_blocked" in str(e):
                raise ValueError("it is waiting on a permission question: answer it first (answer_permission)")
            _herdr_cli("pane", "send-text", session[6:], "\033[200~" + prompt + "\033[201~")
            time.sleep(0.7)
            _herdr_cli("pane", "send-keys", session[6:], "enter")
        return
    fd, path = tempfile.mkstemp(prefix="stairway-prompt-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\033[200~" + prompt + "\033[201~")
        subprocess.run(["screen", "-S", session, "-X", "readbuf", "-e", "utf8", path], check=True)
        subprocess.run(["screen", "-S", session, "-X", "paste", "."], check=True)
        time.sleep(0.7)
        _stuff(session, "\r")
    finally:
        os.unlink(path)


def _tail(text, n=15):
    return "\n".join(text.splitlines()[-n:])


def start_experiment(name, permission_mode=None, spawn=None, mode=None, prompt=None, agent=None, model=None):
    entry = _get(name)
    path = entry["path"]
    permission_mode = permission_mode or entry.get("permission_mode", "default")
    spawn = spawn or entry.get("spawn", "same-dir")
    agent = agent or entry.get("agent", "claude")
    model = model or entry.get("model") or PROVIDER_MODEL
    mode = "session" if agent != "claude" else (mode or entry.get("mode", "server"))
    prompt = prompt or entry.get("initial_prompt")
    _check_agent(agent, mode)
    if not os.path.isdir(path):
        raise ValueError("folder %s no longer exists" % path)
    if permission_mode not in PERMISSION_MODES:
        raise ValueError("permission_mode must be one of %s" % PERMISSION_MODES)
    if spawn not in SPAWN_MODES:
        raise ValueError("spawn must be one of %s" % SPAWN_MODES)
    _check_run(mode, prompt)
    if mode == "server" and spawn == "worktree" and not os.path.isdir(os.path.join(path, ".git")):
        raise ValueError("spawn=worktree needs a git repository in the experiment folder")
    if name in _sessions():
        hint = " Use send_prompt to give it instructions." if prompt and mode == "session" else ""
        return "Experiment %r is already running.%s" % (name, hint)
    _update(name, permission_mode=permission_mode, spawn=spawn, mode=mode, initial_prompt=None, agent=agent,
            model=model if agent != "claude" else None)
    env, to_type, extra = _child_env(), None, {}
    if agent != "claude":
        cmd, extra, to_type = _agent_command(agent, name, path, permission_mode, model, prompt)
        env.update(extra)
    elif mode == "session":
        cmd = [CLAUDE, "--remote-control", name, "--permission-mode", permission_mode,
               "--settings", json.dumps(_session_settings(name)),
               "--append-system-prompt", _session_system_prompt(name)] + ([prompt] if prompt else [])
    else:
        cmd = [CLAUDE, "remote-control", "--name", name, "--permission-mode", permission_mode, "--spawn", spawn]
    if RUNTIME == "herdr":
        _herdr_start(name, path, cmd, extra if agent != "claude" else {}, agent)
    else:
        subprocess.run(["screen", "-U", "-T", "screen-256color", "-dmS", RC_PREFIX + name] + cmd, cwd=path, env=env,
                       check=True)
    if prompt:
        _log_prompt(name, "start", prompt)

    text, url, settled = "", None, 0
    deadline = time.time() + 40
    while time.time() < deadline:
        time.sleep(1)
        session = _sessions().get(name)
        if not session and RUNTIME == "herdr":
            time.sleep(1)  # look twice: a busy herdr server can miss one snapshot
            session = _sessions().get(name)
        if not session:
            return "%s for %r exited right away. Last output:\n\n%s" % (agent, name, text)
        text = _screen_text(session)
        if _answer_trust(session, text):
            settled = 0
            continue
        url = _session_url(text) or url
        if url:
            _update(name, url=url)
        if mode == "server" and "Connected" in text:
            break
        settled += 1 if text.strip() else 0
        if mode == "session" and (url or settled >= (8 if agent in ("claude", "openclaude") else 5)):
            break
    if to_type and name in _sessions():
        _type_prompt(_sessions()[name], to_type)
        time.sleep(2)
        text = _screen_text(_sessions()[name])
    if agent != "claude":
        what = "%s with %s via %s%s" % (agent, model, urlsplit(PROVIDER_BASE_URL).hostname,
                                        ", working on the prompt" if prompt else "")
    elif mode == "session":
        what = "an interactive Claude session (remote control on)%s" % (", working on the prompt" if prompt else "")
    else:
        what = "`claude remote-control` (spawn: %s)" % spawn
    return ("Started %s for %r in %s (permission mode: %s).%s Current screen:\n\n%s"
            % (what, name, path, permission_mode, " claude.ai/code: %s." % url if url else "", _tail(text)))


def send_prompt(name, prompt, wait_seconds=0):
    entry = _get(name)
    _check_prompt(prompt)
    wait_seconds = max(0, min(int(wait_seconds or 0), 600))
    if entry.get("mode", "server") != "session":
        raise ValueError("%r runs in server mode, which has no terminal session to type into; stop it and use "
                         "start_experiment(name, mode='session', prompt=...)" % name)
    session = _sessions().get(name)
    if not session:
        raise ValueError("%r is not running; use start_experiment(name, prompt=...) to start it with this prompt"
                         % name)
    if wait_seconds and session.startswith("herdr:"):
        # submit and wait in one request: herdr returns once the agent is idle, done or blocked again
        try:
            res = _herdr_cli("agent", "prompt", session[6:], prompt, "--wait", "--timeout", str(wait_seconds * 1000),
                             timeout=wait_seconds + 15)
            state = res.get("agent", {}).get("agent_status")
        except ValueError as e:
            if "agent_blocked" in str(e):
                raise ValueError("%r is waiting on a permission question: answer it first (answer_permission)" % name)
            if "timeout" not in str(e) and "stalled" not in str(e):
                raise
            state = "still working after %ds" % wait_seconds
        _log_prompt(name, "send", prompt)
        hint = {"blocked": " It is waiting on a permission question (answer_permission)."}.get(state, "")
        return "Sent the prompt to %r; it is now %s.%s Its terminal:\n\n%s" % (
            name, state, hint, _tail(_tidy(_screen_text(session)), 40))
    _type_prompt(session, prompt)
    _log_prompt(name, "send", prompt)
    time.sleep(2)
    note = (" (wait_seconds needs STAIRWAY_RUNTIME=herdr; it was ignored)" if wait_seconds else "")
    return "Sent the prompt to %r%s. Current screen:\n\n%s" % (name, note, _tail(_screen_text(session)))


def create_experiment(name, permission_mode="default", spawn="same-dir", git_init=True, start=True,
                      mode="session", prompt=None, agent="claude", model=None, callback_url=None, callback_secret=None):
    if not NAME_RE.match(name or ""):
        raise ValueError("name must match %s" % NAME_RE.pattern)
    callback = _check_callback(callback_url, callback_secret)
    path = os.path.join(ROOT, name)
    if agent != "claude":
        mode = "session"
    _check_agent(agent, mode)
    _check_run(mode, prompt)
    with _lock:
        reg = _load()
        if name in reg:
            raise ValueError("an experiment named %r already exists at %s" % (name, reg[name]["path"]))
        if os.path.exists(path):
            raise ValueError("%s already exists; use attach_folder for existing folders" % path)
        os.makedirs(path)
        reg[name] = {"path": path, "origin": "created", "approved": True, "mode": mode, "agent": agent,
                     "model": model if agent != "claude" else None,
                     "permission_mode": permission_mode, "spawn": spawn, "initial_prompt": prompt or None}
        if callback:
            reg[name]["callback"] = callback
        _save(reg)
    msg = ["Created %s." % path]
    if git_init:
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        msg.append("Initialized a git repository.")
    if start:
        msg.append(start_experiment(name, permission_mode, spawn, mode, agent=agent, model=model))
    return " ".join(msg)


def _check_callback(url, secret):
    """A program (e.g. a task planner) that wants each finished reply POSTed to it. None when not asked for."""
    if not url:
        return None
    if not re.match(r"^https://[^\s/]+(/\S*)?$", url) and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?(/\S*)?$", url):
        raise ValueError("callback_url must be an https:// URL")
    if not secret or len(secret) < 16:
        raise ValueError("callback_secret must be at least 16 characters (it signs every callback)")
    return {"url": url, "secret": secret}


def _callback_post(name, entry, text, event="reply"):
    """POST {"experiment", "event", "text", "at", "url"} with X-Stairway-Signature: sha256=HMAC(secret, body).
    event: "reply" (a finished answer) or "waiting" (it asks for permission; text = what it wants to do).
    A few retries with backoff; failures are logged, never raised."""
    cb = entry.get("callback") or {}
    body = json.dumps({"experiment": name, "event": event, "text": text, "at": int(time.time()),
                       "url": entry.get("url")}).encode()
    sig = "sha256=" + hmac.new(cb["secret"].encode(), body, hashlib.sha256).hexdigest()
    for attempt in range(4):
        try:
            req = urllib.request.Request(cb["url"], body, method="POST", headers={
                "Content-Type": "application/json", "X-Stairway-Signature": sig, "User-Agent": "stairway"})
            with urllib.request.urlopen(req, timeout=15) as res:
                if res.status < 300:
                    return
        except Exception as e:
            sys.stderr.write("stairway: callback for %s failed (attempt %d): %s\n" % (name, attempt + 1, e))
        time.sleep(5 * (attempt + 1))


def answer_permission(name, allow):
    """Answer the permission question a session is showing: allow = Yes (Enter), otherwise No (Esc)."""
    _get(name, approved=False)
    session = _sessions().get(name)
    if not session:
        raise ValueError("%s is not running" % name)
    _stuff(session, "\r" if allow else "\033")
    time.sleep(2)
    return _tail(_tidy(_screen_text(session)), 12)


def experiment_info(name):
    """The state of an experiment as JSON, for programs (experiment_status is for people)."""
    entry = _get(name, approved=False)
    rc = _sessions()
    return json.dumps({"name": name, "state": _state(name, entry, rc), "running": name in rc,
                       "agent": entry.get("agent", "claude"), "mode": entry.get("mode", "server"),
                       "session_url": entry.get("url"), "folder": entry["path"],
                       "editor_url": _link("/code/%s/" % name) if name in _screens(CODE_PREFIX) else None,
                       "callback": bool(entry.get("callback"))})


#: Never exported: version control, dependencies, caches.
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", ".next", "dist-newstyle", "target"}


def experiment_files(name, path="", max_mb=15):
    """A zip (base64) of the experiment folder, or of one of its subfolders, as JSON:
    {"files": [{path, size}], "skipped": [...], "zip_base64": ...}. Files over 5 MB and anything past max_mb are left out."""
    import io
    import zipfile

    entry = _get(name, approved=False)
    root = entry["path"]
    base = os.path.realpath(os.path.join(root, path)) if path else root
    if base != root and not _inside(base, root):
        raise ValueError("path must stay inside the experiment folder")
    if not os.path.isdir(base):
        return json.dumps({"files": [], "skipped": [], "zip_base64": None})
    budget = max(1, min(int(max_mb), 50)) * 1024 * 1024
    files, skipped, buf = [], [], io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base)
                if os.path.islink(full):
                    continue
                size = os.path.getsize(full)
                if size > 5 * 1024 * 1024 or size > budget:
                    skipped.append(rel)
                    continue
                zf.write(full, rel)
                budget -= size
                files.append({"path": rel, "size": size})
    return json.dumps({"files": files, "skipped": skipped,
                       "zip_base64": base64.b64encode(buf.getvalue()).decode() if files else None})


def _inside(path, root):
    return path != root and path.startswith(root.rstrip(os.sep) + os.sep)


def _register_folder(path, name, permission_mode, spawn, approved, mode="session", prompt=None, agent="claude",
                     model=None):
    """Register an existing folder; return (name, message). Only the status page passes approved=True."""
    real = os.path.realpath(os.path.expanduser(path))
    if not os.path.isdir(real):
        raise ValueError("%s is not a folder" % real)
    if not any(_inside(real, root) for root in ALLOW):
        raise ValueError("only folders inside %s can be attached" % ", ".join(ALLOW))
    if permission_mode not in PERMISSION_MODES or spawn not in SPAWN_MODES:
        raise ValueError("permission_mode must be one of %s, spawn one of %s" % (PERMISSION_MODES, SPAWN_MODES))
    if agent != "claude":
        mode = "session"
    _check_agent(agent, mode)
    _check_run(mode, prompt)
    with _lock:
        reg = _load()
        for other, entry in reg.items():
            if entry["path"] == real:
                return other, "%s is already registered as %r (%s)." % (
                    real, other, "approved" if entry.get("approved") else "waiting for approval")
        if name:
            if not NAME_RE.match(name):
                raise ValueError("name must match %s" % NAME_RE.pattern)
            if name in reg:
                raise ValueError("an experiment named %r already exists" % name)
        else:
            base = re.sub(r"[^a-z0-9_-]+", "-", os.path.basename(real).lower()).strip("-_")[:56] or "folder"
            name, n = base, 2
            while name in reg:
                name, n = "%s-%d" % (base, n), n + 1
        reg[name] = {"path": real, "origin": "attached", "approved": approved, "requested_at": int(time.time()),
                     "mode": mode, "agent": agent, "model": model if agent != "claude" else None,
                     "permission_mode": permission_mode, "spawn": spawn, "initial_prompt": prompt or None}
        _save(reg)
    return name, None


def attach_folder(path, name=None, permission_mode="default", spawn="same-dir", mode="session", prompt=None,
                  agent="claude", model=None):
    name, msg = _register_folder(path, name, permission_mode, spawn, False, mode, prompt, agent, model)
    if msg:
        return msg
    real = _load()[name]["path"]
    return ("Approval needed: ask the user to open %s and approve %r (%s). Once approved, Claude starts there "
            "on its own%s; check with experiment_status." % (_link("/status"), name, real,
                                                             " with your prompt" if prompt else ""))


def _approve(name):
    _get(name, approved=False)
    _update(name, approved=True)

    def run():
        try:
            sys.stderr.write("stairway: %s\n" % start_experiment(name).splitlines()[0])
        except Exception as e:
            sys.stderr.write("stairway: starting %s failed: %s\n" % (name, e))
    threading.Thread(target=run, daemon=True).start()


def stop_experiment(name):
    _get(name, approved=False)
    session = _sessions().get(name)
    if not session:
        return "Experiment %r is not running." % name
    _quit(session)
    return "Stopped %r (folder kept)." % name


def forget_experiment(name):
    entry = _get(name, approved=False)
    stop_experiment(name)
    stop_editor(name)
    with _lock:
        reg = _load()
        reg.pop(name, None)
        _save(reg)
    shutil.rmtree(_editor_dir(name), ignore_errors=True)
    for cid in _tg_bound(name):
        _tg_update_chat(cid, experiment=None, reply_to=None)
    return "Forgot %r. Its folder %s was left untouched." % (name, entry["path"])


def _agent_label(entry):
    agent = entry.get("agent", "claude")
    return agent if agent == "claude" else "%s (%s)" % (agent, entry.get("model") or PROVIDER_MODEL)


def _state(name, entry, rc):
    if not entry.get("approved"):
        return "awaiting approval"
    return "running" if name in rc else "stopped"


def list_experiments():
    reg, rc, code = _load(), _sessions(), _screens(CODE_PREFIX)
    if not reg:
        return "No experiments yet. New ones go in %s." % ROOT
    return "\n".join("%s\t%s\t%s\t%s mode\teditor %s\t%s\t%s" % (
        n, _state(n, e, rc), _agent_label(e), e.get("mode", "server"), "open" if n in code else "closed",
        e["origin"], e["path"])
        for n, e in sorted(reg.items()))


def experiment_status(name):
    entry = _get(name, approved=False)
    rc = _sessions()
    lines = ["%r: %s, %s, %s mode, %s folder %s" % (name, _state(name, entry, rc), _agent_label(entry),
                                                    entry.get("mode", "server"), entry["origin"], entry["path"])]
    for p in _prompts(name, 3):
        lines.append("prompt (%s, %s): %s" % (p["source"], time.strftime("%Y-%m-%d %H:%M", time.localtime(p["at"])),
                                              p["text"][:200] + ("…" if len(p["text"]) > 200 else "")))
    if entry.get("url"):
        lines.append("claude.ai/code: %s" % entry["url"])
    if name in _screens(CODE_PREFIX):
        lines.append("VS Code: %s" % _link("/code/%s/" % name))
    chats = [(_tg_chat(c) or {}).get("title", str(c)) for c in _tg_bound(name)]
    if chats:
        lines.append("Telegram: replies are posted to %s" % ", ".join(chats))
    if name in rc:
        lines += ["", "Current screen:", "", _screen_text(rc[name])]
    return "\n".join(lines)


# ---------------------------------------------------------------- code-server editors

def _editor_dir(name):
    return os.path.join(STATE, "code", name)


def _editor_socket(name):
    return os.path.join(_editor_dir(name), "code.sock")


def _socket_alive(path):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


TERMINAL_SH = """#!/bin/bash
# stairway terminal for %(name)s. Default profile ("auto"): in session mode the first terminal joins the
# agent session (detach with Ctrl-a d in screen, Ctrl-b q in herdr); every other terminal is a shell in the folder.
want=$1
mode=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]].get("mode", "server"))' \\
       %(registry)s %(name)s 2>/dev/null)
pane=$(%(herdr)s api snapshot 2>/dev/null | python3 -c '
import json, sys
snap = json.load(sys.stdin)["result"]["snapshot"]
ws = [w["workspace_id"] for w in snap["workspaces"] or [] if w.get("label") == sys.argv[1]]
print(next((p["pane_id"] for p in snap["panes"] or [] if p["workspace_id"] in ws), ""))' %(label)s 2>/dev/null)
if [ -n "$pane" ] && { [ "$want" = claude ] || { [ "$want" = auto ] && [ "$mode" = session ] &&
     ! pgrep -f "agent attach $pane" >/dev/null; }; }; then
  %(herdr)s agent attach "$pane" || echo "Could not attach to herdr pane $pane."
  exec bash -l
fi
s=$(screen -ls | awk '$1 ~ /^[0-9]+\\.%(screen)s$/ && /Detached/ {print $1; exit}')
[ "$want" = claude ] && s=$(screen -ls | awk '$1 ~ /^[0-9]+\\.%(screen)s$/ {print $1; exit}')
if [ "$want" = claude ] || { [ "$want" = auto ] && [ "$mode" = session ]; }; then
  if [ -n "$s" ]; then
    screen -x "$s"
  elif [ "$want" = claude ]; then
    echo "The Claude session of %(name)s is not running (or this is server mode, which has no session)."
  fi
fi
exec bash -l
"""


def _write_editor_settings(name, data):
    """(Re)write stairway's terminal profiles, keeping any other settings made in VS Code."""
    os.makedirs(_editor_dir(name), exist_ok=True)
    script = os.path.join(_editor_dir(name), "terminal.sh")
    with open(script, "w") as f:
        f.write(TERMINAL_SH % {"name": name, "registry": REGISTRY, "screen": re.escape(RC_PREFIX + name),
                               "label": shlex.quote(RC_PREFIX + name),
                               "herdr": "%s --session %s" % (shlex.quote(HERDR or "herdr"), shlex.quote(HERDR_SESSION))})
    os.chmod(script, 0o755)
    path = os.path.join(data, "User", "settings.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {"security.workspace.trust.enabled": False, "workbench.startupEditor": "none",
                    "telemetry.telemetryLevel": "off"}
    except ValueError:
        return  # hand-edited with comments: leave it alone
    settings["terminal.integrated.profiles.linux"] = {
        "auto": {"path": script, "args": ["auto"], "overrideName": True},
        "claude session": {"path": script, "args": ["claude"], "icon": "sparkle", "overrideName": True},
        "shell": {"path": "bash", "args": ["-l"]}}
    settings["terminal.integrated.defaultProfile.linux"] = "auto"
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)


def _ensure_editor(name):
    """Start code-server for this experiment unless it is already up; return its socket path."""
    sock = _editor_socket(name)
    if _socket_alive(sock):
        return sock
    entry = _get(name)
    if not CODE_SERVER:
        raise ValueError("code-server is not installed; set STAIRWAY_CODE_SERVER")
    with _lock:
        if _socket_alive(sock):
            return sock
        stale = _screens(CODE_PREFIX).get(name)
        if stale:
            _quit(stale)
        data = os.path.join(_editor_dir(name), "data")
        _write_editor_settings(name, data)
        if os.path.exists(sock):
            os.unlink(sock)
        cmd = [CODE_SERVER, "--auth", "none", "--socket", sock, "--user-data-dir", data,
               "--extensions-dir", os.path.join(STATE, "code-extensions"),
               "--disable-telemetry", "--disable-update-check", "--disable-workspace-trust",
               "--disable-getting-started-override", entry["path"]]
        subprocess.run(["screen", "-dmS", CODE_PREFIX + name] + cmd, cwd=entry["path"], check=True)
        deadline = time.time() + 30
        while time.time() < deadline:
            time.sleep(0.5)
            if _socket_alive(sock):
                return sock
    raise ValueError("code-server for %r did not come up within 30s" % name)


def open_editor(name):
    _ensure_editor(name)
    return ("VS Code for %r is running at %s (log in with the stairway token if asked). "
            "Its default terminal is attached to the remote-control screen." % (name, _link("/code/%s/" % name)))


def stop_editor(name):
    session = _screens(CODE_PREFIX).get(name)
    if not session:
        return "VS Code for %r is not running." % name
    _quit(session)
    return "Stopped VS Code for %r." % name


# ---------------------------------------------------------------- MCP tools

NAME = {"type": "string", "description": "Experiment name: lowercase letters, digits, '-' or '_' (max 63 chars)."}
MODE = {"type": "string", "enum": PERMISSION_MODES, "default": "default",
        "description": "Permission mode for the sessions spawned by remote-control."}
SPAWN = {"type": "string", "enum": SPAWN_MODES, "default": "same-dir",
         "description": "same-dir: sessions share the folder; worktree: each session gets its own git worktree; "
                        "session: a single session, the instance exits when it ends."}
RUN_MODE = {"type": "string", "enum": RUN_MODES, "default": "session",
            "description": "session: one interactive Claude session with remote control, which can receive prompts "
                           "(send_prompt); server: `claude remote-control`, which spawns sessions from claude.ai/code "
                           "and cannot receive prompts."}
PROMPT = {"type": "string",
          "description": "First prompt for the experiment's Claude (session mode): the brief agreed in this "
                         "conversation, e.g. goal, constraints, first steps. It starts working on it right away."}
AGENT = {"type": "string", "enum": AGENTS, "default": "claude",
         "description": "Coding agent to run: claude (Claude Code, the default, with remote control), or opencode / "
                        "crush / openclaude (a Claude Code fork) using the server's OpenAI-compatible provider (%s). "
                        "Non-Claude agents run in session mode and have no claude.ai/code link." % PROVIDER_BASE_URL}
MODEL = {"type": "string",
         "description": "Model for opencode/crush/openclaude, as the provider names it (default %s)." % PROVIDER_MODEL}
NAME_ONLY = {"type": "object", "required": ["name"], "properties": {"name": NAME}}
READ_ONLY = {"readOnlyHint": True, "openWorldHint": False}

TOOLS = {
    "create_experiment": (create_experiment, {
        "description": "Create a new experiment folder <experiments root>/<name> (the root is %s) and start "
                       "Claude in it with remote control, so it shows up in claude.ai/code. Pass `prompt` to brief it "
                       "with what was decided in this conversation; it starts working right away." % ROOT,
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": NAME, "prompt": PROMPT, "agent": AGENT, "model": MODEL, "mode": RUN_MODE,
            "permission_mode": MODE, "spawn": SPAWN,
            "git_init": {"type": "boolean", "default": True, "description": "Run `git init` in the new folder."},
            "start": {"type": "boolean", "default": True, "description": "Start remote-control right away."},
            "callback_url": {"type": "string", "description": "For programs: POST each finished reply here as JSON "
                             "(session mode), signed with callback_secret (header X-Stairway-Signature: sha256=HMAC)."},
            "callback_secret": {"type": "string", "description": "At least 16 characters; signs the callbacks."}}},
    }),
    "attach_folder": (attach_folder, {
        "description": "Ask to use an existing folder on the server as an experiment. The user must approve it "
                       "on the stairway status page; then remote-control starts there. Tell the user to approve it.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Absolute path of the existing folder."},
            "name": dict(NAME, description="Optional experiment name; defaults to the folder name."),
            "prompt": PROMPT, "agent": AGENT, "model": MODEL, "mode": RUN_MODE, "permission_mode": MODE,
            "spawn": SPAWN}},
    }),
    "start_experiment": (start_experiment, {
        "description": "(Re)start Claude for an experiment, optionally with a first prompt. Without options it "
                       "reuses the last mode and permission settings. A restarted session starts a new conversation.",
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": NAME, "prompt": PROMPT, "agent": AGENT, "model": MODEL, "mode": RUN_MODE,
            "permission_mode": MODE, "spawn": SPAWN}},
    }),
    "send_prompt": (send_prompt, {
        "description": "Type a prompt into a running session-mode experiment, as if the user typed it: use it to "
                       "pass on instructions or context from this conversation. If Claude is busy, the prompt is "
                       "queued. Returns what its terminal shows a moment later.",
        "inputSchema": {"type": "object", "required": ["name", "prompt"], "properties": {
            "name": NAME, "prompt": {"type": "string", "description": "The text to send (max %d chars)." % MAX_PROMPT},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 600, "default": 0,
                             "description": "With the herdr runtime: wait up to this long for the agent to finish "
                                            "(or ask for permission) and return its terminal then. 0 returns at once."}}},
    }),
    "stop_experiment": (stop_experiment, {
        "description": "Stop the remote-control instance of an experiment. The folder is kept.",
        "inputSchema": NAME_ONLY,
    }),
    "forget_experiment": (forget_experiment, {
        "description": "Stop an experiment and remove it from stairway. Never deletes the folder or its files.",
        "inputSchema": NAME_ONLY,
    }),
    "list_experiments": (list_experiments, {
        "description": "List experiments: state, whether VS Code is open, origin (created/attached) and folder.",
        "inputSchema": {"type": "object", "properties": {}}, "annotations": READ_ONLY,
    }),
    "answer_permission": (answer_permission, {
        "description": "Answer the permission question a session-mode experiment is waiting on: allow=true answers "
                       "Yes, allow=false answers No. Returns the end of its terminal.",
        "inputSchema": {"type": "object", "required": ["name", "allow"], "properties": {
            "name": NAME, "allow": {"type": "boolean"}}},
    }),
    "experiment_info": (experiment_info, {
        "description": "An experiment's state as JSON (state, running, agent, mode, session_url, folder, editor_url), "
                       "for programs; experiment_status is the human-readable version.",
        "inputSchema": NAME_ONLY, "annotations": READ_ONLY,
    }),
    "experiment_files": (experiment_files, {
        "description": "The experiment's files as a base64 zip in JSON (files over 5 MB, .git and dependency folders "
                       "are left out). Pass `path` for one subfolder, e.g. deliverables.",
        "inputSchema": {"type": "object", "required": ["name"], "properties": {
            "name": NAME, "path": {"type": "string", "description": "Subfolder inside the experiment (default: all)."},
            "max_mb": {"type": "integer", "default": 15, "description": "Cap on the zipped files, in MB (max 50)."}}},
        "annotations": READ_ONLY,
    }),
    "experiment_status": (experiment_status, {
        "description": "Show an experiment's state, its claude.ai/code and VS Code links, "
                       "and what its remote-control terminal currently shows.",
        "inputSchema": NAME_ONLY, "annotations": READ_ONLY,
    }),
    "open_editor": (open_editor, {
        "description": "Start (if needed) a browser VS Code on the experiment folder and return its link. "
                       "Its terminal is attached to the remote-control screen; files can be uploaded by drag and drop.",
        "inputSchema": NAME_ONLY,
    }),
    "stop_editor": (stop_editor, {
        "description": "Stop the browser VS Code of an experiment.",
        "inputSchema": NAME_ONLY,
    }),
}


def handle(msg):
    """Handle one JSON-RPC message; return a response dict, or None for notifications."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO,
                   "instructions": "Create experiment folders in the experiments root " + ROOT + " (or attach "
                                   "existing folders elsewhere after the user "
                                   "approves them at %s) and run remote-control Claude instances in them. Each "
                                   "experiment can also be opened in a browser VS Code." % _link("/status")})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [dict(name=n, **spec) for n, (_, spec) in TOOLS.items()]})
    if method == "tools/call":
        tool = TOOLS.get(params.get("name"))
        if not tool:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        try:
            text, is_error = tool[0](**(params.get("arguments") or {})), False
        except Exception as e:  # report tool failures to the model, not as protocol errors
            text, is_error = "Error: %s" % e, True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_error})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}


# ---------------------------------------------------------------- web pages

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>@TITLE@</title><style>
:root{--bg:#f5f5f2;--fg:#1c1c1a;--muted:#686862;--card:#fff;--line:#deded7;--accent:#2b5d8c;
--ok:#2a7a4b;--warn:#9a6200;--off:#85857e;--pre:#efefea;--btn:#ecece6}
@media (prefers-color-scheme:dark){:root{--bg:#151514;--fg:#e6e6e0;--muted:#9a9a92;--card:#1e1e1c;
--line:#33332f;--accent:#82b3e3;--ok:#6cc690;--warn:#e2ab4f;--off:#7a7a72;--pre:#111110;--btn:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 48px}h1{font-size:22px;margin:0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:28px 0 10px}
a{color:var(--accent)}header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}
.muted{color:var(--muted)}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat,.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.stat b{display:block;font-size:20px;font-variant-numeric:tabular-nums}.stat span{font-size:12px;color:var(--muted)}
.card{margin-bottom:10px}.top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.top strong{font-size:16px}
.path{font:13px ui-monospace,monospace;color:var(--muted);word-break:break-all;margin:4px 0 8px}
.badge{font-size:12px;border-radius:99px;padding:1px 9px;border:1px solid currentColor}
.running{color:var(--ok)}.stopped{color:var(--off)}.pending{color:var(--warn)}
.actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}form{margin:0}
button,.btn{font:inherit;font-size:13px;padding:5px 11px;border-radius:7px;border:1px solid var(--line);
background:var(--btn);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
.primary{background:var(--accent);border-color:var(--accent);color:var(--bg)}.danger{color:#c4413a}
details{margin-top:8px}summary{cursor:pointer;font-size:13px;color:var(--muted)}
pre{background:var(--pre);border-radius:8px;padding:10px;overflow-x:auto;font-size:12px;line-height:1.35;margin:6px 0 0}
.flash{background:var(--card);border-left:3px solid var(--accent);padding:8px 12px;margin:14px 0;border-radius:4px}
input{font:inherit;padding:7px 10px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);width:100%}
select{font:inherit;padding:6px 8px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);width:100%}
.connect{margin-top:12px}.connect summary{font-size:15px;color:var(--fg);font-weight:600}
.connect h3{font-size:13px;margin:16px 0 4px}.connect p{margin:6px 0}.tools{padding-left:18px;font-size:13px}
.tools li{margin:3px 0}.copyrow{display:flex;gap:6px;align-items:flex-start}.copyrow input,.copyrow pre{flex:1;min-width:0;margin:0}
code{font:12.5px ui-monospace,monospace}
.newgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}
.newform{display:grid;gap:8px;align-content:start;margin:0}.newform p{margin:0;font-size:13px}
.newform label{display:grid;gap:3px;font-size:13px;color:var(--muted)}
.newform label.check{display:flex;gap:6px;align-items:center}.newform label.check input{width:auto}
textarea{font:13px/1.4 ui-monospace,monospace;padding:7px 10px;border-radius:7px;border:1px solid var(--line);
background:var(--card);color:var(--fg);width:100%;resize:vertical}
.promptform{display:flex;gap:6px;align-items:flex-end;margin-top:8px}.prompt{margin-top:8px;font-size:12px}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.brand{display:flex;align-items:baseline;gap:10px}nav{display:flex;gap:14px;flex-wrap:wrap;font-size:14px}
nav a{text-decoration:none}.sysbar{display:flex;gap:6px 16px;flex-wrap:wrap;font-size:12px;color:var(--muted);
margin:8px 0 4px;font-variant-numeric:tabular-nums}.sysbar span+span:before{content:"·";margin-right:16px}
.sysbar b{font-weight:500;color:var(--fg)}.meta{font-size:12px;color:var(--muted);margin:-4px 0 8px}
.sectionhead{display:flex;justify-content:space-between;align-items:baseline}h2 .muted{text-transform:none;letter-spacing:0}
.chip{font-size:12px;border-radius:99px;padding:1px 9px;background:var(--btn);color:var(--muted)}
.chip.agent{color:var(--accent)}.chip.ok{color:var(--ok)}.chip.tg{color:var(--fg)}.chip.warn{color:var(--warn)}
.exp .top strong{font-size:16px}.dot{width:9px;height:9px;border-radius:50%;background:var(--off);flex:none}
.exp.is-running .dot{background:var(--ok);box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 25%,transparent)}
.card.attention{border-left:3px solid var(--warn)}.card p.muted{margin:4px 0 8px;font-size:13px}
details.more{display:inline-block;margin:0}details.more summary{list-style:none;font-size:13px;padding:5px 8px}
details.more[open]{display:block;margin-top:6px}.newpanel summary{font-weight:600;color:var(--accent)}
.newpanel[open] summary{margin-bottom:10px}.newpanel .newform{border:1px solid var(--line)}
.promptform textarea{min-height:34px}.foot{margin-top:28px;font-size:12px}
.login{max-width:360px;margin:12vh auto}.login form{display:grid;gap:10px;margin-top:16px}
</style></head><body><main>@BODY@</main></body></html>"""


def _page(title, body):
    return PAGE.replace("@TITLE@", html.escape(title)).replace("@BODY@", body)


def _human(kb):
    for unit in ("KB", "MB", "GB", "TB"):
        if kb < 1024 or unit == "TB":
            return "%.1f %s" % (kb, unit)
        kb /= 1024.0


def _system_stats():
    stats = []
    try:
        stats.append(("load (1/5/15 min)", "%.2f / %.2f / %.2f" % os.getloadavg()))
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            mem = {l.split(":")[0]: int(l.split()[1]) for l in f}
        total, avail = mem["MemTotal"], mem.get("MemAvailable", mem["MemFree"])
        stats.append(("memory used", "%s of %s" % (_human(total - avail), _human(total))))
    except (OSError, KeyError, ValueError):
        pass
    du = shutil.disk_usage(ROOT)
    stats.append(("disk free (experiments root)", _human(du.free / 1024.0)))
    up = int(time.time() - STARTED)
    stats.append(("stairway uptime", "%dd %dh %dm" % (up // 86400, up // 3600 % 24, up // 60 % 60)))
    return stats


def _button(action, name, label, cls="", confirm=None):
    onsubmit = ' onsubmit="return confirm(%s)"' % html.escape(json.dumps(confirm)) if confirm else ""
    return ('<form method="post" action="/status"%s><input type="hidden" name="action" value="%s">'
            '<input type="hidden" name="name" value="%s"><button class="%s">%s</button></form>'
            % (onsubmit, action, html.escape(name), cls, label))


def _select(field, options, default):
    return '<select name="%s">%s</select>' % (field, "".join(
        '<option%s>%s</option>' % (" selected" if o == default else "", o) for o in options))


def _connect_section(base):
    e = html.escape
    oauth_url = "%s/mcp" % base
    url = "%s/mcp/%s" % (base, TOKEN)
    blocks = [
        ("claude.ai (web, desktop, mobile)",
         "Settings → Connectors → Add custom connector, paste the URL above, then press Connect: a stairway page "
         "opens where you allow access (with the token if you are not logged in here).", None),
        ("Claude Code CLI (OAuth: run /mcp in Claude Code to log in)", None,
         "claude mcp add --transport http stairway %s" % oauth_url),
        ("URL with the token in it, for clients without OAuth", None, url),
        ("Claude Code CLI, token in the URL", None, "claude mcp add --transport http stairway %s" % url),
        ("Claude Code CLI, token in a header", None,
         'claude mcp add --transport http stairway %s/mcp --header "Authorization: Bearer %s"' % (base, TOKEN)),
        (".mcp.json", None, json.dumps({"mcpServers": {"stairway": {"type": "http", "url": url}}}, indent=2)),
    ]
    out = ['<details class="card connect"><summary>Connect Claude to stairway (MCP)</summary>'
           '<p class="muted">Streamable HTTP, stateless, JSON responses, OAuth 2.1 with dynamic client registration. '
           'Changing STAIRWAY_TOKEN revokes every OAuth grant.</p><div class="copyrow"><input id="mcp-url" readonly '
           'value="%s"><button type="button" data-copy="mcp-url">Copy</button></div>' % e(oauth_url)]
    for i, (title, text, code) in enumerate(blocks):
        out.append("<h3>%s</h3>" % e(title))
        if text:
            out.append('<p class="muted">%s</p>' % e(text))
        if code:
            out.append('<div class="copyrow"><pre id="cfg-%d">%s</pre><button type="button" data-copy="cfg-%d">Copy'
                       '</button></div>' % (i, e(code), i))
    out.append("<h3>Tools</h3><ul class=\"tools\">%s</ul></details>" % "".join(
        "<li><code>%s</code> %s</li>" % (e(n), e(spec["description"])) for n, (_, spec) in TOOLS.items()))
    return "".join(out)


def _new_section():
    modes = ("<label>Agent%s</label><label>Model (other agents)<input name=\"model\" placeholder=\"%s\"></label>"
             "<label>Run mode (Claude)%s</label><label>Permission mode%s</label><label>Spawn (server mode)%s</label>"
             "<label>First prompt (session mode, optional)<textarea name=\"prompt\" rows=\"4\" "
             "placeholder=\"What should Claude do first?\"></textarea></label>") % (
        _select("agent", AGENTS, "claude"), html.escape(PROVIDER_MODEL),
        _select("mode", RUN_MODES, "session"), _select("permission_mode", PERMISSION_MODES, "default"),
        _select("spawn", SPAWN_MODES, "same-dir"))
    return (
        '<div class="newgrid">'
        '<form method="post" action="/status" class="card newform"><input type="hidden" name="action" value="create">'
        '<strong>New experiment</strong><p class="muted">Creates <code>%s/&lt;name&gt;</code> and starts '
        'the chosen agent in it.</p><label>Name<input name="name" required pattern="[a-z0-9][a-z0-9_-]{0,62}" '
        'placeholder="my-experiment" title="lowercase letters, digits, - and _"></label>%s'
        '<label class="check"><input type="checkbox" name="git_init" checked> git init</label>'
        '<button class="primary">Create and start</button></form>'
        '<form method="post" action="/status" class="card newform"><input type="hidden" name="action" value="attach">'
        '<strong>Attach existing folder</strong><p class="muted">Any folder inside %s. Attaching trusts it: its '
        '.claude settings, hooks and MCP servers apply.</p><label>Path<input name="path" required placeholder="%s/…">'
        '</label><label>Name (optional)<input name="name" pattern="[a-z0-9][a-z0-9_-]{0,62}" '
        'placeholder="defaults to the folder name"></label>%s<button class="primary">Attach and start</button></form>'
        '</div>' % (html.escape(ROOT), modes, html.escape(", ".join(ALLOW)), html.escape(ALLOW[0] if ALLOW else ""),
                    modes))


COPY_JS = """<script>document.querySelectorAll('[data-copy]').forEach(function(b){b.onclick=function(){
var t=document.getElementById(b.dataset.copy);navigator.clipboard.writeText(t.value||t.textContent).then(function(){
b.textContent='Copied';setTimeout(function(){b.textContent='Copy'},1500)})}})</script>"""


def _chip(text, cls=""):
    return '<span class="chip %s">%s</span>' % (cls, html.escape(text))


def _experiment_card(n, x, rc, code, chats):
    e = html.escape
    state = "running" if n in rc else "stopped"
    mode = x.get("mode", "server")
    chips = [_chip(_agent_label(x), "agent")]
    if state == "running" and rc[n].startswith("herdr:") and _herdr_status.get(n):
        st = _herdr_status[n]
        chips.append(_chip({"blocked": "⏳ needs permission"}.get(st, st), "warn" if st == "blocked" else ""))
    if n in code:
        chips.append(_chip("VS Code open", "ok"))
    chips += [_chip("✈ " + t, "tg") for t in chats]
    main = []
    if x.get("url"):
        main.append('<a class="btn primary" href="%s" target="_blank" rel="noopener">Open in claude.ai</a>' % e(x["url"]))
    main.append('<a class="btn" href="/code/%s/" target="_blank">VS Code</a>' % e(n))
    main.append(_button("stop", n, "Stop") if state == "running" else _button("start", n, "Start", "primary"))
    more = ([_button("editor-stop", n, "Close VS Code")] if n in code else []) + [
        _button("forget", n, "Forget", "danger", "Stop %s and remove it from stairway? The folder is kept." % n)]
    body = []
    if state == "running" and mode == "session":
        body.append('<form method="post" action="/status" class="promptform"><input type="hidden" name="action" '
                    'value="prompt"><input type="hidden" name="name" value="%s"><textarea name="prompt" rows="1" '
                    'required placeholder="Send a prompt…"></textarea><button class="primary">Send</button></form>'
                    % e(n))
    if state == "running":
        body.append("<details><summary>Terminal</summary><pre>%s</pre></details>" % e(_screen_text(rc[n])))
    history = _prompts(n)
    if history:
        body.append("<details><summary>Prompts (%d)</summary>%s</details>" % (len(history), "".join(
            '<div class="prompt"><span class="muted">%s &middot; %s</span><pre>%s</pre></div>'
            % (time.strftime("%d %b %H:%M", time.localtime(p["at"])), e(p["source"]), e(p["text"]))
            for p in reversed(history))))
    return ('<div class="card exp is-%s"><div class="top"><span class="dot"></span><strong>%s</strong>%s</div>'
            '<div class="path">%s</div><div class="meta">%s mode &middot; %s permissions &middot; %s</div>'
            '<div class="actions">%s<details class="more"><summary>more</summary><div class="actions">%s</div>'
            '</details></div>%s</div>'
            % (state, e(n), "".join(chips), e(x["path"]), e(mode), e(x.get("permission_mode", "default")),
               e(x["origin"]), "".join(main), "".join(more), "".join(body)))


def status_page(flash="", base=""):
    reg, rc, code = _load(), _sessions(), _screens(CODE_PREFIX)
    e = html.escape
    short = {"load (1/5/15 min)": "load", "memory used": "memory", "disk free (experiments root)": "disk free",
             "stairway uptime": "up"}
    stats = "".join('<span title="%s"><b>%s</b> %s</span>' % (e(k), e(short.get(k, k)), e(v)) for k, v in _system_stats())
    out = ['<header class="topbar"><div class="brand"><h1>stairway</h1><span class="muted">%s</span></div>'
           '<nav><a href="#experiments">Experiments</a><a href="#telegram">Telegram</a><a href="#connect">Connect</a>'
           '%s<a href="/status">Refresh</a><a href="/logout">Log out</a></nav></header>'
           '<div class="sysbar" title="load (1/5/15 min) · memory · free disk in the experiments root · uptime">%s'
           '</div>' % (e(socket.gethostname()), '<a href="/herdr/">herdr</a>' if HERDR else "", stats)]
    if flash:
        out.append('<div class="flash">%s</div>' % e(flash))

    attention = []
    for n, x in sorted((n, x) for n, x in reg.items() if not x.get("approved")):
        attention.append(
            '<div class="card attention"><div class="top"><strong>%s</strong><span class="badge pending">folder</span>'
            '%s</div><div class="path">%s</div><p class="muted">Claude asked to run %s here (%s, %s mode). Approving '
            'trusts this folder: its project settings, hooks and MCP servers will apply.</p><div class="actions">%s%s'
            '</div></div>'
            % (e(n), _chip(_agent_label(x), "agent"), e(x["path"]), e(x.get("agent", "claude")),
               e(x.get("permission_mode", "default")), e(x.get("mode", "session")),
               _button("approve", n, "Approve and start", "primary"), _button("reject", n, "Reject", "danger")))
    attention += _tg_pending_cards()
    if attention:
        out.append('<h2>Needs your approval</h2>' + "".join(attention))

    active = [(n, x) for n, x in reg.items() if x.get("approved")]
    active.sort(key=lambda nx: (nx[0] not in rc, nx[0]))
    chats = {}
    for c in _tg_load()["chats"].values():
        if c.get("approved") and c.get("experiment"):
            chats.setdefault(c["experiment"], []).append(c.get("title") or "chat")
    out.append('<div class="sectionhead" id="experiments"><h2>Experiments <span class="muted">%d running &middot; '
               '%d total</span></h2></div>' % (sum(1 for n, _ in active if n in rc), len(active)))
    out.append('<details class="card newpanel"%s><summary>+ New experiment or attach a folder</summary>%s</details>'
               % ("" if active else " open", _new_section()))
    if not active:
        out.append('<p class="muted">None yet. Create one above, or ask Claude (MCP) or the Telegram bot.</p>')
    out += [_experiment_card(n, x, rc, code, chats.get(n, [])) for n, x in active]
    out.append(_tg_section())
    out.append('<div id="connect">%s</div>' % _connect_section(base or PUBLIC_URL))
    out.append('<p class="muted foot">stairway %s &middot; experiments root <code>%s</code> &middot; '
               '<a href="https://github.com/sirmmo/stairway">github.com/sirmmo/stairway</a></p>'
               % (SERVER_INFO["version"], e(ROOT)))
    return _page("stairway", "".join(out) + COPY_JS)


def login_page(next_path, error=""):
    return _page("stairway login", (
        '<div class="login"><h1>stairway</h1><p class="muted">Enter the stairway token.</p>%s'
        '<form method="post" action="/login"><input type="hidden" name="next" value="%s">'
        '<input type="password" name="token" autofocus autocomplete="current-password" placeholder="token">'
        '<button class="primary">Log in</button></form></div>'
        % ('<div class="flash">%s</div>' % html.escape(error) if error else "", html.escape(next_path))))


def _form_create(f):
    return create_experiment(f.get("name", "").strip(), f.get("permission_mode", "default"),
                             f.get("spawn", "same-dir"), git_init=f.get("git_init") == "on",
                             mode=f.get("mode", "session"), prompt=f.get("prompt", "").strip() or None,
                             agent=f.get("agent", "claude"), model=f.get("model", "").strip() or None)


def _form_attach(f):
    name, msg = _register_folder(f.get("path", "").strip(), f.get("name", "").strip() or None,
                                 f.get("permission_mode", "default"), f.get("spawn", "same-dir"), True,
                                 f.get("mode", "session"), f.get("prompt", "").strip() or None,
                                 f.get("agent", "claude"), f.get("model", "").strip() or None)
    if msg:
        return msg
    _approve(name)
    return "Attached %s as %r; remote-control is starting." % (_load()[name]["path"], name)


def _form_prompt(f):
    return send_prompt(f.get("name", ""), f.get("prompt", "")).split(" Current screen:")[0]


FORM_ACTIONS = {"create": _form_create, "attach": _form_attach, "prompt": _form_prompt}

ACTIONS = {
    "start": lambda n: start_experiment(n).split(". ")[0] + ".",
    "stop": stop_experiment,
    "approve": lambda n: (_approve(n), "Approved %r; remote-control is starting." % n)[1],
    "reject": forget_experiment,
    "forget": forget_experiment,
    "editor-stop": stop_editor,
}


# ---------------------------------------------------------------- Telegram
# A bot (STAIRWAY_TELEGRAM_TOKEN) polls Telegram. Each chat must be approved on /status, then it can be bound
# to one session-mode experiment: messages addressed to the bot become prompts, and the session's Stop and
# Notification hooks post its replies back. telegram.json: {offset, chats: {chat_id: {title, type, approved,
# experiment, reply_to}}}

TG_TOKEN = os.environ.get("STAIRWAY_TELEGRAM_TOKEN", "")
TG_API = os.environ.get("STAIRWAY_TELEGRAM_API", "https://api.telegram.org").rstrip("/")
TG_STATE = os.path.join(STATE, "telegram.json")
TG_PERMISSION_MODE = os.environ.get("STAIRWAY_TELEGRAM_PERMISSION_MODE", "acceptEdits")
TG_MAX_FILE = 50 * 1024 * 1024
TG_UPLOADS = "telegram-uploads"
PHOTO_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_tg_me = {}
TG_COMMANDS = [
    ("ask", "Send a message to the experiment's Claude"),
    ("new", "<name> [claude|opencode|crush|openclaude] [brief]: create an experiment and bind it here"),
    ("bind", "<name>: bind an existing session-mode experiment here"),
    ("unbind", "Unbind the experiment from this chat"),
    ("status", "Experiment state and links"),
    ("screen", "What Claude's terminal shows now"),
    ("approve", "Answer Yes to the permission Claude is asking for"),
    ("deny", "Answer No (Esc) to the permission Claude is asking for"),
    ("ls", "[path]: list files in the experiment"),
    ("get", "<path>: send a file from the experiment"),
    ("start", "[brief]: start the experiment's Claude"),
    ("stop", "Stop the experiment's Claude"),
    ("help", "What this bot does"),
]


def _local_url():
    return "http://%s:%d" % ("127.0.0.1" if HOST in ("", "0.0.0.0") else HOST, PORT)


def _hook_key(name):
    return hmac.new(TOKEN.encode(), b"hook:" + name.encode(), hashlib.sha256).hexdigest()[:32]


def _hook_url(name, kind):
    return "%s/hooks/%s/%s?k=%s" % (_local_url(), name, kind, _hook_key(name))


def _session_settings(name):
    """--settings for session mode: an ANSI theme, and hooks that relay replies to bound Telegram chats."""
    def hook(kind):
        return [{"hooks": [{"type": "command", "timeout": 15,
                            "command": "curl -s -m 10 -X POST --data-binary @- '%s' >/dev/null" % _hook_url(name, kind)}]}]
    settings = {"theme": CLAUDE_THEME, "hooks": {"Notification": hook("notify")}}
    if RUNTIME == "herdr":
        settings["tui"] = "default"  # the fullscreen renderer, chosen for herdr's libghostty, hangs in a headless pane
    return settings


def _session_system_prompt(name):
    return ("This Claude session runs on a stairway server as experiment %r. Your final reply of each turn may be "
            "relayed to a Telegram group bound to this experiment, so keep it self-contained. Messages that start "
            "with [Telegram ...] come from people in that group. To send a file you made (an image, a chart, a "
            "document) to the group, run: curl -s -d '{\"path\": \"<path inside this folder>\", \"caption\": "
            "\"<short text>\"}' '%s'" % (name, _hook_url(name, "file")))


def _tg_load():
    try:
        with open(TG_STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"offset": 0, "chats": {}}


def _tg_save(st):
    tmp = TG_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, TG_STATE)


def _tg_chat(chat_id):
    return _tg_load()["chats"].get(str(chat_id))


def _tg_update_chat(chat_id, **fields):
    with _lock:
        st = _tg_load()
        chat = st["chats"].setdefault(str(chat_id), {})
        chat.update(fields)
        _tg_save(st)
        return chat


def _tg_drop_chat(chat_id):
    with _lock:
        st = _tg_load()
        chat = st["chats"].pop(str(chat_id), None)
        _tg_save(st)
        return chat


def _tg_bound(name):
    return [int(cid) for cid, c in _tg_load()["chats"].items() if c.get("approved") and c.get("experiment") == name]


def _multipart(fields, files):
    boundary = secrets.token_hex(16)
    parts = []
    for k, v in fields.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode())
    for k, (filename, data) in files.items():
        parts.append(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                      'Content-Type: application/octet-stream\r\n\r\n' % (boundary, k, filename.replace('"', ""))).encode()
                     + data + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def _tg(method, params=None, files=None, timeout=30):
    if files:
        body, ctype = _multipart({k: v for k, v in (params or {}).items() if v is not None}, files)
    else:
        body, ctype = json.dumps(params or {}).encode(), "application/json"
    req = urllib.request.Request("%s/bot%s/%s" % (TG_API, TG_TOKEN, method), body,
                                 {"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            res = json.load(e)
        except ValueError:
            raise ValueError("Telegram %s: HTTP %s" % (method, e.code))
    if not res.get("ok"):
        raise ValueError("Telegram %s: %s" % (method, res.get("description")))
    return res["result"]


def _tg_say(chat_id, text, reply_to=None):
    text = text.strip() or "(empty)"
    for i in range(0, len(text), 4000):
        params = {"chat_id": chat_id, "text": text[i:i + 4000], "disable_web_page_preview": True}
        if reply_to and i == 0:
            params.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
        _tg("sendMessage", params)


def _tg_send_file(chat_id, path, caption=""):
    size = os.path.getsize(path)
    if size > TG_MAX_FILE:
        raise ValueError("%s is %d MB; Telegram bots can send at most 50 MB" % (os.path.basename(path), size >> 20))
    with open(path, "rb") as f:
        data = f.read()
    params = {"chat_id": chat_id, "caption": caption[:1000] or None}
    if path.lower().endswith(PHOTO_EXT) and size <= 10 * 1024 * 1024:
        try:
            return _tg("sendPhoto", params, {"photo": (os.path.basename(path), data)}, timeout=120)
        except ValueError:
            pass  # e.g. odd dimensions: fall back to a document
    return _tg("sendDocument", params, {"document": (os.path.basename(path), data)}, timeout=120)


def _inside_experiment(name, rel):
    base = os.path.realpath(_get(name)["path"])
    path = os.path.realpath(os.path.join(base, rel or "."))
    if path != base and not path.startswith(base + os.sep):
        raise ValueError("%s is outside the experiment folder" % rel)
    return path


def _tg_relay(name, text, reply=True):
    """Post text to every chat bound to this experiment (from the hooks)."""
    for cid in _tg_bound(name):
        chat = _tg_chat(cid) or {}
        try:
            _tg_say(cid, text, chat.get("reply_to") if reply else None)
        except Exception as e:
            sys.stderr.write("stairway: telegram relay to %s failed: %s\n" % (cid, e))
        if reply:
            _tg_update_chat(cid, reply_to=None)


# ---- reply relay: read finished answers from each agent's own conversation store

OPENCODE_DB = os.path.expanduser(os.environ.get("STAIRWAY_OPENCODE_DB", "~/.local/share/opencode/opencode.db"))
_relay_state = {}  # name -> agent-specific position


def _ro_db(path):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)


def _claude_replies(name, path, st, home="~/.claude"):
    """New final answers in the newest Claude Code transcript of this folder."""
    proj = os.path.join(os.path.expanduser(home), "projects", re.sub(r"[^A-Za-z0-9]", "-", path))
    try:
        files = sorted((os.path.getmtime(os.path.join(proj, f)), f) for f in os.listdir(proj) if f.endswith(".jsonl"))
    except FileNotFoundError:
        files = []
    if not files:
        st.setdefault("file", None)  # the first transcript to appear is a new session: read it from the start
        return []
    newest = os.path.join(proj, files[-1][1])
    if st.get("file") != newest:
        # a new session starts from its beginning; on the first look, skip what was already there
        offset = os.path.getsize(newest) if "file" not in st else 0
        st.update(file=newest, offset=offset, pending=[])
    out, size = [], os.path.getsize(newest)
    if size > st["offset"]:
        with open(newest, "rb") as f:
            f.seek(st["offset"])
            chunk = f.read(size - st["offset"])
        complete = chunk[:chunk.rfind(b"\n") + 1]
        st["offset"] += len(complete)
        for line in complete.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            m = e.get("message") or {}
            if e.get("type") == "assistant" and not e.get("isSidechain") and m.get("stop_reason") == "end_turn":
                st["pending"] += [b.get("text", "") for b in m.get("content", [])
                                  if isinstance(b, dict) and b.get("type") == "text"]
        if complete:
            return []  # still writing: wait for a quiet poll before posting
    if st.get("pending"):
        out, st["pending"] = ["\n\n".join(t for t in st["pending"] if t.strip())], []
    return out


def _opencode_replies(name, path, st):
    if not os.path.exists(OPENCODE_DB):
        return []
    db = _ro_db(OPENCODE_DB)
    try:
        rows = db.execute("select m.id, m.time_created, m.data from message m join session s on s.id = m.session_id "
                          "where s.directory = ? and m.time_created > ? order by m.time_created",
                          (path, st.get("since", 0))).fetchall()
        if "since" not in st:
            st["since"] = rows[-1][1] if rows else int(time.time() * 1000)
            return []
        out = []
        for mid, created, data in rows:
            m = json.loads(data)
            if m.get("role") != "assistant":
                st["since"] = created
                continue
            if not (m.get("time") or {}).get("completed"):
                break  # still running: look again later
            st["since"] = created
            if m.get("error"):
                err = m["error"].get("data", {}).get("message") or m["error"].get("name")
                out.append("⚠️ %s error: %s" % (name, err))
            elif m.get("finish") not in ("tool-calls", "tool_calls"):
                parts = [json.loads(d) for (d,) in db.execute("select data from part where message_id = ? "
                                                              "order by time_created", (mid,))]
                text = "\n\n".join(p.get("text", "") for p in parts if p.get("type") == "text" and not p.get("synthetic"))
                if text.strip():
                    out.append(text)
        return out
    finally:
        db.close()


def _crush_replies(name, path, st):
    dbpath = os.path.join(path, ".crush", "crush.db")
    if not os.path.exists(dbpath):
        return []
    db = _ro_db(dbpath)
    try:
        rows = db.execute("select id, created_at, finished_at, parts from messages where role = 'assistant' "
                          "and created_at >= ? order by created_at", (st.get("since", 0),)).fetchall()
        if "since" not in st:
            st.update(since=int(time.time()), seen=[r[0] for r in rows])
            return []
        out = []
        for mid, created, finished, parts in rows:
            if mid in st["seen"]:
                continue
            if not finished:
                break
            st["seen"] = (st["seen"] + [mid])[-200:]
            st["since"] = created
            parts = json.loads(parts)
            fin = next((p["data"] for p in parts if p.get("type") == "finish"), {})
            if fin.get("reason") == "error":
                out.append("⚠️ %s error: %s %s" % (name, fin.get("message", ""), fin.get("details", "")))
            elif fin.get("reason") not in ("tool_use", "tool_calls"):
                text = "\n\n".join(p["data"].get("text", "") for p in parts if p.get("type") == "text")
                if text.strip():
                    out.append(text)
        return out
    finally:
        db.close()


def _openclaude_replies(name, path, st):
    return _claude_replies(name, path, st, os.path.join(_agent_dir(name), "openclaude"))


REPLY_READERS = {"claude": _claude_replies, "opencode": _opencode_replies, "crush": _crush_replies,
                 "openclaude": _openclaude_replies}


def _relay_loop():
    while True:
        time.sleep(3)
        try:
            bound = {c.get("experiment") for c in _tg_load()["chats"].values() if c.get("approved")} if TG_TOKEN else set()
            reg, running = _load(), _sessions()
            hooked = {n for n, e in reg.items() if e.get("callback")}
            for name in bound | hooked:
                entry = reg.get(name)
                if not entry or name not in running or entry.get("mode", "server") != "session":
                    _relay_state.pop(name, None)
                    continue
                st = _relay_state.setdefault(name, {})
                # agents without a Notification hook: herdr's screen detection says when they ask for permission
                state = _herdr_status.get(name) if running[name].startswith("herdr:") else None
                if state == "blocked" and st.get("herdr") != "blocked" and entry.get("agent") in ("opencode", "crush"):
                    _notify_waiting(name, entry, "%s needs your permission" % entry["agent"],
                                    to_chats=name in bound, to_callback=name in hooked)
                st["herdr"] = state
                for text in REPLY_READERS[entry.get("agent", "claude")](name, entry["path"], st):
                    if name in bound:
                        _tg_relay(name, text)
                    if name in hooked:
                        threading.Thread(target=_callback_post, args=(name, entry, text), daemon=True).start()
        except Exception as e:
            sys.stderr.write("stairway: relay: %s\n" % e)


def _notify_waiting(name, entry, msg, to_chats=True, to_callback=True):
    """It asks for permission: tell bound Telegram chats and the program's callback."""
    session = _sessions().get(name)
    shown = _tail(_tidy(_screen_text(session)), 25) if session else ""
    if to_callback and entry.get("callback"):
        threading.Thread(target=_callback_post, args=(name, entry, shown or msg, "waiting"), daemon=True).start()
    if to_chats and TG_TOKEN and _tg_bound(name):
        text = "⏳ %s: %s" % (name, msg)
        if "permission" in msg.lower():
            text += "\n\n%s\n\nAnswer with /approve or /deny (see /screen)." % _tail(shown, 12)
        threading.Thread(target=_tg_relay, args=(name, text, False), daemon=True).start()


def handle_hook(name, kind, payload):
    """Called by the session's hooks (POST /hooks/<name>/<kind>)."""
    entry = _load().get(name) or {}
    if kind == "notify" and entry.get("callback"):
        msg = payload.get("message") or ""
        idle = payload.get("notification_type") == "idle_prompt" or "waiting for your input" in msg
        if not idle:
            # What it wants to do: the permission prompt on its screen, else the hook's message.
            session = _sessions().get(name)
            shown = _tail(_tidy(_screen_text(session)), 25) if session else ""
            threading.Thread(target=_callback_post, args=(name, entry, shown or msg, "waiting"), daemon=True).start()
    if not TG_TOKEN or not _tg_bound(name):
        return {"ok": bool(entry.get("callback")), "error": None if entry.get("callback") else "no Telegram chat is bound to %s" % name}
    if kind == "stop":
        pass  # replies are relayed by the transcript watcher (_relay_loop); sessions started earlier still call this
    elif kind == "notify":
        msg = payload.get("message") or ""
        if payload.get("notification_type") == "idle_prompt" or "waiting for your input" in msg:
            return {"ok": True}  # Claude finished and is idle: the Stop hook already posted the reply
        text = "⏳ %s: %s" % (name, msg)
        if "permission" in msg.lower():
            text += "\nAnswer with /approve or /deny (see /screen)."
        threading.Thread(target=_tg_relay, args=(name, text, False), daemon=True).start()
    elif kind == "file":
        path = _inside_experiment(name, payload.get("path", ""))
        if not os.path.isfile(path):
            return {"ok": False, "error": "%s is not a file" % payload.get("path")}
        for cid in _tg_bound(name):
            _tg_send_file(cid, path, payload.get("caption", ""))
    else:
        return {"ok": False, "error": "unknown hook"}
    return {"ok": True}


def _tg_help():
    return ("I connect this chat to a Claude experiment on the stairway server.\n\n"
            "Talk to Claude by mentioning me, replying to my messages, or with /ask. Other messages stay between "
            "you. Files you send me (with a mention or as a reply) are saved into the experiment folder.\n\n"
            + "\n".join("/%s %s" % c for c in TG_COMMANDS))


def _tg_sender(msg):
    u = msg.get("from") or {}
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or "someone"
    return name + (" (@%s)" % u["username"] if u.get("username") else "")


def _tg_prompt(cid, chat, msg, text, note=""):
    name = chat.get("experiment")
    if not name:
        return _tg_say(cid, "No experiment is bound here yet: use /new <name> [brief] or /bind <name>.")
    quoted = ""
    r = msg.get("reply_to_message")
    if r and (r.get("from") or {}).get("id") != _tg_me.get("id") and (r.get("text") or r.get("caption")):
        quoted = "\n(replying to %s: %s)" % (_tg_sender(r), (r.get("text") or r.get("caption"))[:800])
    prompt = "[Telegram, %s in %s]%s%s\n%s" % (_tg_sender(msg), chat.get("title") or "a private chat", quoted,
                                             note, text)
    _tg("sendChatAction", {"chat_id": cid, "action": "typing"})
    send_prompt(name, prompt)
    _tg_update_chat(cid, reply_to=msg["message_id"])


def _tg_save_upload(name, msg):
    """Save a document or photo from a message into the experiment; return its relative path."""
    if msg.get("document"):
        file_id, filename = msg["document"]["file_id"], msg["document"].get("file_name") or "file"
    else:
        photo = max(msg["photo"], key=lambda p: p.get("file_size", 0))
        file_id, filename = photo["file_id"], "photo-%d.jpg" % msg["message_id"]
    info = _tg("getFile", {"file_id": file_id})
    filename = re.sub(r"[^\w.-]+", "_", os.path.basename(filename)).strip("._") or "file"
    rel = os.path.join(TG_UPLOADS, filename)
    dest = _inside_experiment(name, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen("%s/file/bot%s/%s" % (TG_API, TG_TOKEN, info["file_path"]),
                                timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return rel


def _tg_command(cid, chat, msg, cmd, arg):
    name = chat.get("experiment")

    def need():
        if not name:
            raise ValueError("No experiment is bound here: use /new <name> [brief] or /bind <name>.")
        return name

    if cmd in ("help", "start") and not (cmd == "start" and name):
        return _tg_say(cid, _tg_help())
    if cmd == "ask":
        return _tg_prompt(cid, chat, msg, arg) if arg else _tg_say(cid, "Usage: /ask <message>")
    if cmd == "new":
        new, _, brief = arg.partition(" ")
        agent = "claude"
        first, _, rest = brief.strip().partition(" ")
        if first.lower() in AGENTS:
            agent, brief = first.lower(), rest
        if not new:
            return _tg_say(cid, "Usage: /new <name> [claude|opencode|crush|openclaude] [brief]")
        _tg_say(cid, "Creating %s…" % new)
        prompt = ("[Telegram, %s in %s]\n%s" % (_tg_sender(msg), chat.get("title") or "a private chat", brief.strip())
                  if brief.strip() else None)
        create_experiment(new.lower(), TG_PERMISSION_MODE, mode="session", prompt=prompt, agent=agent)
        _tg_update_chat(cid, experiment=new.lower(), reply_to=msg["message_id"] if prompt else None)
        entry = _get(new.lower())
        return _tg_say(cid, "Created and bound %s (%s).%s" % (new.lower(), entry["path"],
                                                              " claude.ai/code: " + entry["url"] if entry.get("url") else ""))
    if cmd == "bind":
        entry = _get(arg.strip())
        if entry.get("mode", "server") != "session":
            raise ValueError("%s runs in server mode, which can't receive prompts. Restart it in session mode on "
                             "/status first." % arg.strip())
        _tg_update_chat(cid, experiment=arg.strip())
        return _tg_say(cid, "Bound %s to this chat. Its replies will be posted here." % arg.strip())
    if cmd == "unbind":
        _tg_update_chat(cid, experiment=None, reply_to=None)
        return _tg_say(cid, "Unbound %s." % (name or "nothing"))
    if cmd == "status":
        return _tg_say(cid, experiment_status(need()).split("\n\nCurrent screen:")[0])
    if cmd == "screen":
        session = _sessions().get(need())
        return _tg_say(cid, _tail(_screen_text(session), 30) if session else "%s is not running." % name)
    if cmd in ("approve", "deny"):
        session = _sessions().get(need())
        if not session:
            return _tg_say(cid, "%s is not running." % name)
        _stuff(session, "\r" if cmd == "approve" else "\033")
        time.sleep(2)
        return _tg_say(cid, _tail(_screen_text(session), 12))
    if cmd == "ls":
        path = _inside_experiment(need(), arg.strip())
        entries = sorted(os.listdir(path))
        lines = ["%s%s" % (e, "/" if os.path.isdir(os.path.join(path, e)) else "") for e in entries if e != ".git"]
        return _tg_say(cid, "\n".join(lines[:100]) + ("\n… %d more" % (len(lines) - 100) if len(lines) > 100 else "")
                       if lines else "(empty)")
    if cmd == "get":
        path = _inside_experiment(need(), arg.strip())
        if not os.path.isfile(path):
            raise ValueError("%s is not a file" % arg.strip())
        return _tg_send_file(cid, path, arg.strip())
    if cmd == "start":
        return _tg_say(cid, start_experiment(need(), mode="session", prompt=arg.strip() or None).split(" Current screen:")[0])
    if cmd == "stop":
        return _tg_say(cid, stop_experiment(need()))
    return None  # not ours


def _tg_handle(update):
    if "my_chat_member" in update:
        m = update["my_chat_member"]
        cid, status = m["chat"]["id"], m["new_chat_member"]["status"]
        if status in ("left", "kicked"):
            _tg_drop_chat(cid)
        elif not _tg_chat(cid):
            _tg_update_chat(cid, title=m["chat"].get("title") or _tg_sender(m), type=m["chat"]["type"], approved=False,
                            experiment=None, added_by=_tg_sender(m))
            _tg_say(cid, "Hi! Before I take commands here, approve this chat on %s." % _link("/status"))
        return
    msg = update.get("message")
    if not msg:
        return
    cid = msg["chat"]["id"]
    if msg.get("migrate_to_chat_id"):  # group became a supergroup: its id changes
        old = _tg_drop_chat(cid)
        if old:
            _tg_update_chat(msg["migrate_to_chat_id"], **old)
        return
    chat = _tg_chat(cid)
    if not chat:
        chat = _tg_update_chat(cid, title=msg["chat"].get("title") or _tg_sender(msg), type=msg["chat"]["type"],
                               approved=False, experiment=None, added_by=_tg_sender(msg))
        return _tg_say(cid, "Hi! Before I take commands here, approve this chat on %s." % _link("/status"))
    text = msg.get("text") or msg.get("caption") or ""
    me = "@" + _tg_me.get("username", "")
    private = msg["chat"]["type"] == "private"
    replying_to_me = (msg.get("reply_to_message", {}).get("from") or {}).get("id") == _tg_me.get("id")
    mentioned = me != "@" and me.lower() in text.lower()
    cmd = None
    m = re.match(r"^/([a-z_]+)(?:@(\w+))?\s*(.*)$", text, re.S | re.I)
    if m and (not m.group(2) or m.group(2).lower() == _tg_me.get("username", "").lower()):
        cmd, arg = m.group(1).lower(), m.group(3).strip()
    if not chat.get("approved"):
        if cmd or mentioned or private:
            _tg_say(cid, "This chat is waiting for approval on %s." % _link("/status"))
        return
    try:
        if cmd:
            if _tg_command(cid, chat, msg, cmd, arg) is None and cmd not in dict(TG_COMMANDS):
                _tg_say(cid, "Unknown command. /help lists what I can do.")
            return
        if not (private or mentioned or replying_to_me):
            return
        text = re.sub(re.escape(me), "", text, flags=re.I).strip() if mentioned else text
        note = ""
        if msg.get("document") or msg.get("photo"):
            if not chat.get("experiment"):
                return _tg_say(cid, "Bind an experiment first: /new or /bind.")
            note = "\n(%s shared a file, saved as %s)" % (_tg_sender(msg), _tg_save_upload(chat["experiment"], msg))
        if text or note:
            _tg_prompt(cid, chat, msg, text or "(no message)", note)
    except Exception as e:
        _tg_say(cid, "Error: %s" % e)


def _tg_loop():
    while True:
        try:
            if not _tg_me:
                _tg_me.update(_tg("getMe"))
                _tg("setMyCommands", {"commands": [{"command": c, "description": d[:256]} for c, d in TG_COMMANDS]})
                sys.stderr.write("stairway: telegram bot @%s is polling\n" % _tg_me.get("username"))
            updates = _tg("getUpdates", {"offset": _tg_load()["offset"], "timeout": 50,
                                         "allowed_updates": ["message", "my_chat_member"]}, timeout=70)
            for u in updates:
                with _lock:
                    st = _tg_load()
                    st["offset"] = u["update_id"] + 1
                    _tg_save(st)
                threading.Thread(target=_tg_handle_logged, args=(u,), daemon=True).start()
        except Exception as e:
            sys.stderr.write("stairway: telegram: %s\n" % e)
            time.sleep(10)


def _tg_handle_logged(update):
    try:
        _tg_handle(update)
    except Exception as e:
        sys.stderr.write("stairway: telegram update failed: %s\n" % e)


def _tg_pending_cards():
    e = html.escape
    return ["".join((
        '<div class="card attention"><div class="top"><strong>%s</strong><span class="badge pending">Telegram chat'
        '</span></div><p class="muted">%s added the bot to this %s. Approving lets its members prompt the bound '
        'experiment, answer permission prompts and read its files.</p><div class="actions">%s%s</div></div>'
        % (e(c.get("title") or cid), e(c.get("added_by", "someone")), e(c.get("type", "chat")),
           _button("tg-approve", cid, "Approve", "primary"), _button("tg-reject", cid, "Reject", "danger"))))
        for cid, c in sorted(_tg_load()["chats"].items()) if not c.get("approved")] if TG_TOKEN else []


def _tg_section():
    e = html.escape
    out = ['<h2 id="telegram">Telegram</h2>']
    if not TG_TOKEN:
        return out[0] + ('<p class="muted">Not configured. Create a bot with @BotFather, put its token in '
                         'STAIRWAY_TELEGRAM_TOKEN in .env and restart stairway.</p>')
    out.append('<p class="muted">Bot %s. Add it to a group, approve the group here, then use /new or /bind in '
               'the group.</p>' % (e("@" + _tg_me["username"]) if _tg_me.get("username") else "(connecting…)"))
    chats = {k: v for k, v in _tg_load()["chats"].items() if v.get("approved")}
    if not chats:
        out.append('<p class="muted">No approved chats yet.</p>')
    sessions = sorted(n for n, x in _load().items() if x.get("approved") and x.get("mode") == "session")
    for cid, c in sorted(chats.items(), key=lambda kv: kv[1].get("title") or ""):
        acts = []
        if not c.get("approved"):
            acts += [_button("tg-approve", cid, "Approve", "primary"), _button("tg-reject", cid, "Reject", "danger")]
        else:
            if c.get("experiment"):
                acts.append(_button("tg-unbind", cid, "Unbind"))
            acts.append('<form method="post" action="/status" class="actions"><input type="hidden" name="action" '
                        'value="tg-bind"><input type="hidden" name="chat" value="%s"><select name="experiment">%s'
                        '</select><button>Bind</button></form>'
                        % (e(cid), "".join('<option>%s</option>' % e(n) for n in sessions)))
            acts.append(_button("tg-reject", cid, "Forget", "danger", "Forget this chat? The bot stays in it."))
        out.append('<div class="card"><div class="top"><strong>%s</strong><span class="badge %s">%s</span>'
                   '<span class="muted" style="font-size:12px">%s &middot; added by %s</span></div>'
                   '<div class="path">%s</div><div class="actions">%s</div></div>'
                   % (e(c.get("title") or cid), "running" if c.get("approved") else "pending",
                      "approved" if c.get("approved") else "awaiting approval", e(c.get("type", "")),
                      e(c.get("added_by", "?")),
                      "bound to " + e(c["experiment"]) if c.get("experiment") else "no experiment bound",
                      "".join(acts)))
    return "".join(out)


def _tg_page_approve(cid):
    c = _tg_update_chat(cid, approved=True)
    if TG_TOKEN:
        threading.Thread(target=_tg_say, args=(int(cid), "This chat is approved. " + _tg_help()), daemon=True).start()
    return "Approved Telegram chat %s." % c.get("title", cid)


def _form_tg_bind(f):
    entry = _get(f.get("experiment", ""))
    if entry.get("mode", "server") != "session":
        raise ValueError("only session-mode experiments can be bound")
    _tg_update_chat(f.get("chat", ""), experiment=f["experiment"])
    return "Bound %s to the Telegram chat." % f["experiment"]


FORM_ACTIONS["tg-bind"] = _form_tg_bind
ACTIONS.update({
    "tg-approve": _tg_page_approve,
    "tg-reject": lambda cid: "Forgot Telegram chat %s." % (_tg_drop_chat(cid) or {}).get("title", cid),
    "tg-unbind": lambda cid: (_tg_update_chat(cid, experiment=None, reply_to=None), "Unbound.")[1],
})


# ---------------------------------------------------------------- HTTP

HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer",
               "transfer-encoding"}
CODE_PATH = re.compile(r"^/code/([a-z0-9][a-z0-9_-]{0,62})(/.*)?$")


def _browser_key():
    return hmac.new(TOKEN.encode(), b"stairway-browser", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- OAuth 2.1 (for claude.ai connectors)
# Registered clients, access and refresh tokens are signed blobs, so nothing is stored and changing
# STAIRWAY_TOKEN revokes everything. Authorization codes live in memory for a few minutes.

ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 24 * 3600
_codes = {}  # code -> {client_id, redirect_uri, challenge, exp}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(payload):
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return body + "." + _b64(hmac.new(TOKEN.encode(), b"oauth." + body.encode(), hashlib.sha256).digest())


def _unsign(blob, kind):
    try:
        body, sig = blob.split(".")
        good = _b64(hmac.new(TOKEN.encode(), b"oauth." + body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if payload.get("k") != kind or payload.get("exp", float("inf")) < time.time():
        return None
    return payload


def _valid_redirect(uri):
    u = urlsplit(uri)
    return u.scheme == "https" or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))


def _issue_tokens(client_id):
    now = int(time.time())
    return {"access_token": _sign({"k": "access", "c": client_id[-16:], "exp": now + ACCESS_TTL}),
            "refresh_token": _sign({"k": "refresh", "c": client_id, "exp": now + REFRESH_TTL}),
            "token_type": "Bearer", "expires_in": ACCESS_TTL}


def authorize_page(params, client, logged_in, error=""):
    e = html.escape
    hidden = "".join('<input type="hidden" name="%s" value="%s">' % (e(k), e(v)) for k, v in params.items())
    token_field = "" if logged_in else (
        '<input type="password" name="token" autofocus autocomplete="current-password" placeholder="stairway token">')
    return _page("stairway authorize", (
        '<div class="login"><h1>stairway</h1><p><strong>%s</strong> wants to use stairway: create and attach '
        'experiment folders and run Claude in them on this machine.</p><p class="muted">It will be sent back to '
        '<code>%s</code>.</p>%s<form method="post" action="/authorize">%s%s'
        '<button class="primary" name="decision" value="allow">Allow</button>'
        '<button name="decision" value="deny">Deny</button></form></div>'
        % (e(client.get("n") or "An MCP client"), e(urlsplit(params.get("redirect_uri", "")).netloc),
           '<div class="flash">%s</div>' % e(error) if error else "", hidden, token_field)))


def _pipe(a, b):
    """Shovel bytes both ways until either side closes."""
    try:
        while True:
            ready, _, _ = select.select([a, b], [], [], 3600)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stairway"

    # -- helpers

    def _send(self, code, body=b"", ctype="application/json", headers=()):
        if not isinstance(body, bytes):
            body = (json.dumps(body) if ctype == "application/json" else body).encode()
        self.send_response(code)
        if body:
            self.send_header("Content-Type", ctype)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, code, page, headers=()):
        self._send(code, page, "text/html; charset=utf-8", headers)

    def _redirect(self, location, headers=()):
        self._send(303, headers=[("Location", location)] + list(headers))

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _form(self):
        return {k: v[0] for k, v in parse_qs(self._body().decode()).items()}

    def _base(self):
        return PUBLIC_URL or "%s://%s" % (self.headers.get("X-Forwarded-Proto", "http"), self.headers.get("Host", "localhost"))

    def _mcp_authorized(self, path):
        if path.startswith("/mcp/"):
            return hmac.compare_digest(path[5:].rstrip("/"), TOKEN)
        auth = self.headers.get("Authorization", "")
        if path != "/mcp" or not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth, "Bearer " + TOKEN) or _unsign(auth[7:], "access") is not None

    def _browser_authorized(self):
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get("stairway")
        except CookieError:
            return False
        return morsel is not None and hmac.compare_digest(morsel.value, _browser_key())

    # -- routing

    def _route(self):
        url = urlsplit(self.path)
        path = url.path
        if path == "/mcp" or path.startswith("/mcp/"):
            return self._mcp(path)
        if path.startswith("/.well-known/oauth-protected-resource"):
            return self._send(200, {"resource": self._base() + "/mcp", "authorization_servers": [self._base()],
                                    "bearer_methods_supported": ["header"]})
        if path.startswith("/.well-known/oauth-authorization-server") or path.startswith("/.well-known/openid-configuration"):
            b = self._base()
            return self._send(200, {
                "issuer": b, "authorization_endpoint": b + "/authorize", "token_endpoint": b + "/token",
                "registration_endpoint": b + "/register", "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})
        if path.startswith("/hooks/"):
            return self._hook(path, url.query)
        if path == "/register":
            return self._register()
        if path == "/authorize":
            return self._authorize(url.query)
        if path == "/token":
            return self._token()
        if path == "/login":
            return self._login()
        if path == "/logout":
            return self._redirect("/login", [("Set-Cookie", "stairway=; Path=/; Max-Age=0")])
        if path not in ("/", "/status", "/herdr", "/herdr/", "/herdr/ws") and not path.startswith("/code/"):
            return self._send(404, {"error": "not found"})
        if not self._browser_authorized():
            if self.command == "GET":
                return self._redirect("/login?next=" + quote(self.path, safe=""))
            return self._send(401, {"error": "log in at /login"})
        if path == "/":
            return self._redirect("/status")
        if path == "/status":
            return self._status(url.query)
        if path in ("/herdr", "/herdr/"):
            if not HERDR:
                return self._send(404, {"error": "herdr is not installed"})
            return self._redirect("/herdr/") if path == "/herdr" else self._html(200, HERDR_PAGE)
        if path == "/herdr/ws":
            return self._herdr_ws(url.query)
        return self._code(path, url.query)

    def _herdr_ws(self, query):
        """A websocket running `herdr --session <stairway's session>` in a pseudo-terminal, for /herdr/."""
        origin = urlsplit(self.headers.get("Origin", "")).netloc
        if not HERDR or self.headers.get("Upgrade", "").lower() != "websocket" or origin != self.headers.get("Host"):
            return self._send(400, {"error": "a same-origin websocket is required"})
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
        try:
            _herdr_server()
        except ValueError as e:
            return self._send(500, {"error": str(e)})
        self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True
        q = parse_qs(query)
        _herdr_terminal(self.connection, int(q.get("cols", ["80"])[0]), int(q.get("rows", ["24"])[0]))

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _route

    def _mcp(self, path):
        if not self._mcp_authorized(path):
            if path != "/mcp":
                return self._send(404, {"error": "not found"})
            meta = self._base() + "/.well-known/oauth-protected-resource"
            return self._send(401, {"error": "unauthorized"},
                              headers=[("WWW-Authenticate", 'Bearer resource_metadata="%s"' % meta)])
        if self.command != "POST":  # no server-initiated stream, no sessions to delete
            return self._send(405)
        try:
            msg = json.loads(self._body() or b"null")
        except ValueError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        for m in msg if isinstance(msg, list) else [msg]:
            if isinstance(m, dict):
                sys.stderr.write("mcp %s %s\n" % (m.get("method"), json.dumps((m.get("params") or {}).get("name", ""))))
        if isinstance(msg, list):
            replies = [r for r in (handle(m) for m in msg if isinstance(m, dict)) if r]
            return self._send(200, replies) if replies else self._send(202)
        reply = handle(msg) if isinstance(msg, dict) else None
        return self._send(200, reply) if reply else self._send(202)

    def _hook(self, path, query):
        m = re.match(r"^/hooks/([a-z0-9][a-z0-9_-]{0,62})/(stop|notify|file)$", path)
        key = parse_qs(query).get("k", [""])[0]
        if not m or self.command != "POST" or not hmac.compare_digest(key, _hook_key(m.group(1))):
            return self._send(404, {"error": "not found"})
        try:
            payload = json.loads(self._body() or b"{}")
            return self._send(200, handle_hook(m.group(1), m.group(2), payload if isinstance(payload, dict) else {}))
        except Exception as e:
            return self._send(200, {"ok": False, "error": str(e)})

    def _register(self):
        if self.command != "POST":
            return self._send(405)
        try:
            req = json.loads(self._body() or b"{}")
        except ValueError:
            req = None
        uris = req.get("redirect_uris") if isinstance(req, dict) else None
        if not uris or not all(isinstance(u, str) and _valid_redirect(u) for u in uris):
            return self._send(400, {"error": "invalid_redirect_uri"})
        name = str(req.get("client_name", ""))[:80]
        client_id = _sign({"k": "client", "r": uris, "n": name})
        return self._send(201, {"client_id": client_id, "client_name": name, "redirect_uris": uris,
                                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
                                "token_endpoint_auth_method": "none", "client_id_issued_at": int(time.time())})

    def _authorize(self, query):
        params = ({k: v[0] for k, v in parse_qs(query).items()} if self.command == "GET" else self._form())
        client = _unsign(params.get("client_id", ""), "client")
        redirect_uri = params.get("redirect_uri", "")
        if not client or redirect_uri not in client["r"]:
            return self._html(400, _page("stairway", "<p>Unknown client or redirect URI.</p>"))

        def back(**q):
            if params.get("state"):
                q["state"] = params["state"]
            sep = "&" if "?" in redirect_uri else "?"
            return self._send(302, headers=[("Location", redirect_uri + sep + urlencode(q))])

        if params.get("response_type") != "code" or params.get("code_challenge_method") != "S256" \
                or not params.get("code_challenge"):
            return back(error="invalid_request", error_description="response_type=code and S256 PKCE are required")
        fields = {k: params[k] for k in ("response_type", "client_id", "redirect_uri", "code_challenge",
                                         "code_challenge_method", "state", "scope", "resource") if k in params}
        logged_in = self._browser_authorized()
        if self.command != "POST":
            return self._html(200, authorize_page(fields, client, logged_in))
        if params.get("decision") != "allow":
            return back(error="access_denied")
        if not logged_in and not hmac.compare_digest(params.get("token", ""), TOKEN):
            time.sleep(1)
            return self._html(401, authorize_page(fields, client, False, "Wrong token."))
        now = time.time()
        for c in [c for c, v in _codes.items() if v["exp"] < now]:
            _codes.pop(c, None)
        code = secrets.token_urlsafe(32)
        _codes[code] = {"client_id": params["client_id"], "redirect_uri": redirect_uri,
                        "challenge": params["code_challenge"], "exp": now + 300}
        return back(code=code)

    def _token(self):
        if self.command != "POST":
            return self._send(405)
        f = self._form()
        no_store = [("Cache-Control", "no-store")]
        if f.get("grant_type") == "authorization_code":
            grant = _codes.pop(f.get("code", ""), None)
            verifier = f.get("code_verifier", "")
            if (not grant or grant["exp"] < time.time() or grant["client_id"] != f.get("client_id")
                    or grant["redirect_uri"] != f.get("redirect_uri")
                    or not hmac.compare_digest(_b64(hashlib.sha256(verifier.encode()).digest()), grant["challenge"])):
                return self._send(400, {"error": "invalid_grant"}, headers=no_store)
            return self._send(200, _issue_tokens(grant["client_id"]), headers=no_store)
        if f.get("grant_type") == "refresh_token":
            r = _unsign(f.get("refresh_token", ""), "refresh")
            if not r or (f.get("client_id") and f["client_id"] != r["c"]):
                return self._send(400, {"error": "invalid_grant"}, headers=no_store)
            return self._send(200, _issue_tokens(r["c"]), headers=no_store)
        return self._send(400, {"error": "unsupported_grant_type"}, headers=no_store)

    def _login(self):
        if self.command != "POST":
            nxt = parse_qs(urlsplit(self.path).query).get("next", ["/status"])[0]
            return self._html(200, login_page(nxt))
        form = self._form()
        nxt = form.get("next", "/status")
        if not nxt.startswith("/") or nxt.startswith("//"):
            nxt = "/status"
        if not hmac.compare_digest(form.get("token", ""), TOKEN):
            time.sleep(1)
            return self._html(401, login_page(nxt, "Wrong token."))
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        cookie = "stairway=%s; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict%s" % (_browser_key(), secure)
        return self._redirect(nxt, [("Set-Cookie", cookie)])

    def _status(self, query):
        if self.command == "POST":
            form = self._form()
            act = form.get("action")
            try:
                if act in FORM_ACTIONS:
                    msg = FORM_ACTIONS[act](form)
                elif act in ACTIONS:
                    msg = ACTIONS[act](form.get("name", ""))
                else:
                    msg = "Unknown action."
            except Exception as e:
                msg = "Error: %s" % e
            return self._redirect("/status?msg=" + quote(msg))
        base = PUBLIC_URL or "%s://%s" % (self.headers.get("X-Forwarded-Proto", "http"), self.headers.get("Host", "localhost"))
        return self._html(200, status_page(parse_qs(query).get("msg", [""])[0], base))

    def _code(self, path, query):
        m = CODE_PATH.match(path)
        if not m:
            return self._send(404, {"error": "not found"})
        name, rest = m.group(1), m.group(2)
        if rest is None:
            return self._redirect("/code/%s/%s" % (name, "?" + query if query else ""))
        try:
            sock_path = _ensure_editor(name)
        except Exception as e:
            return self._html(404 if isinstance(e, ValueError) else 500, _page(
                "stairway", '<p>%s</p><p><a href="/status">Back to status</a></p>' % html.escape(str(e))))
        upgrade = "upgrade" in self.headers.get("Connection", "").lower()
        head = ["%s %s HTTP/1.1" % (self.command, rest + ("?" + query if query else ""))]
        head += ["%s: %s" % (k, v) for k, v in self.headers.items() if k.lower() not in HOP_HEADERS]
        head.append("Connection: Upgrade" if upgrade else "Connection: close")
        body = self._body()
        upstream = socket.socket(socket.AF_UNIX)
        try:
            upstream.connect(sock_path)
            upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
            _pipe(self.connection, upstream)
        finally:
            upstream.close()
            self.close_connection = True

    def log_message(self, fmt, *args):
        if not self.path.startswith("/code/"):
            sys.stderr.write("%s %s\n" % (self.headers.get("X-Real-IP") or self.address_string() if self.headers else
                                           self.address_string(), re.sub(r"k=[0-9a-f]+", "k=<key>", re.sub(r"/mcp/[^ ]+", "/mcp/<token>", fmt % args))))


# ---------------------------------------------------------------- /herdr/: herdr's own UI in the browser

def _ws_frame(opcode, payload):
    n = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else
                                       bytes([126]) + n.to_bytes(2, "big") if n < 65536 else
                                       bytes([127]) + n.to_bytes(8, "big"))
    return head + payload


def _ws_frames(buf):
    """Complete client frames in buf as (opcode, payload), and the unparsed rest."""
    out = []
    while len(buf) >= 2:
        opcode, n, at = buf[0] & 0x0F, buf[1] & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                break
            n, at = int.from_bytes(buf[2:4], "big"), 4
        elif n == 127:
            if len(buf) < 10:
                break
            n, at = int.from_bytes(buf[2:10], "big"), 10
        masked = buf[1] & 0x80
        if len(buf) < at + (4 if masked else 0) + n:
            break
        mask = buf[at:at + 4] if masked else b"\0\0\0\0"
        at += 4 if masked else 0
        out.append((opcode, bytes(b ^ mask[i % 4] for i, b in enumerate(buf[at:at + n]))))
        buf = buf[at + n:]
    return out, buf


def _herdr_terminal(conn, cols, rows):
    """Bridge a websocket to a herdr client in a pseudo-terminal. Client messages are JSON:
    {"i": "<input>"} or {"r": [cols, rows]}; server messages are binary terminal output."""
    import fcntl, pty, signal, struct, termios

    def winsize(fd, c, r):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(2, min(r, 500)), max(10, min(c, 500)), 0, 0))
    pid, master = pty.fork()
    if pid == 0:
        try:
            os.chdir(STATE)
            env = dict(_child_env(), TERM="xterm-256color", COLORTERM="truecolor")
            os.execvpe(HERDR, [HERDR, "--session", HERDR_SESSION], env)
        finally:
            os._exit(127)
    winsize(master, cols, rows)
    buf = b""
    try:
        while True:
            ready, _, _ = select.select([conn, master], [], [], 60)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if not data:
                    break
                conn.sendall(_ws_frame(2, data))
            if conn in ready:
                data = conn.recv(65536)
                if not data:
                    break
                frames, buf = _ws_frames(buf + data)
                for opcode, payload in frames:
                    if opcode == 8:
                        raise EOFError
                    if opcode == 9:
                        conn.sendall(_ws_frame(10, payload))
                    elif opcode == 1:
                        msg = json.loads(payload.decode("utf-8", "replace"))
                        if "i" in msg:
                            _write_all(master, msg["i"].encode("utf-8"))
                        elif "r" in msg:
                            winsize(master, int(msg["r"][0]), int(msg["r"][1]))
                            os.kill(pid, signal.SIGWINCH)
    except (EOFError, OSError, ValueError):
        pass
    finally:
        try:
            conn.sendall(_ws_frame(8, b""))
        except OSError:
            pass
        os.close(master)
        try:
            os.kill(pid, signal.SIGHUP)  # the client only: herdr's server and the agents keep running
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)


HERDR_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<title>herdr · stairway</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.min.css"
 integrity="sha384-tStR1zLfWgsiXCF3IgfB3lBa8KmBe/lG287CL9WCeKgQYcp1bjb4/+mwN6oti4Co" crossorigin="anonymous">
<style>
html,body{margin:0;height:100%;background:#151514;color:#e6e6e0;font:14px system-ui,sans-serif}
body{display:flex;flex-direction:column;height:100dvh;padding:env(safe-area-inset-top) env(safe-area-inset-right)
 env(safe-area-inset-bottom) env(safe-area-inset-left);box-sizing:border-box}
#term{flex:1;min-height:0;padding:4px}
#keys{display:flex;gap:6px;padding:6px;overflow-x:auto;border-top:1px solid #33332f;flex:none}
#keys button,#keys a{flex:none;background:#2a2a27;color:#e6e6e0;border:1px solid #3a3a36;border-radius:7px;
 padding:7px 11px;font:13px ui-monospace,monospace;text-decoration:none}
#keys .on{background:#82b3e3;color:#151514}#state{flex:none;font-size:12px;color:#9a9a92;align-self:center;padding:0 6px}
</style></head><body><div id="term"></div><div id="keys">
<a href="/status">&larr;</a><button data-k="\\u001b">Esc</button><button data-k="\\t">Tab</button>
<button id="ctrl">Ctrl</button><button data-k="\\u0002">Ctrl-b</button><button data-k="\\u0003">Ctrl-c</button>
<button data-k="\\u001b[A">&uarr;</button><button data-k="\\u001b[B">&darr;</button>
<button data-k="\\u001b[D">&larr;</button><button data-k="\\u001b[C">&rarr;</button>
<button data-k="\\r">Enter</button><span id="state">connecting…</span></div>
<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js"
 integrity="sha384-J4qzUjBl1FxyLsl/kQPQIOeINsmp17OHYXDOMpMxlKX53ZfYsL+aWHpgArvOuof9" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js"
 integrity="sha384-XGqKrV8Jrukp1NITJbOEHwg01tNkuXr6uB6YEj69ebpYU3v7FvoGgEg23C1Gcehk" crossorigin="anonymous"></script>
<script>
const term = new Terminal({cursorBlink: true, fontSize: innerWidth < 600 ? 12 : 14, scrollback: 0,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', theme: {background: '#151514'}});
const fit = new FitAddon.FitAddon(); term.loadAddon(fit); term.open(document.getElementById('term')); fit.fit();
const state = document.getElementById('state'), ctrl = document.getElementById('ctrl');
let ws, ctrlOn = false;
function send(m) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); }
function input(d) {
  if (ctrlOn && d.length === 1) { const c = d.toUpperCase().charCodeAt(0); if (c >= 64 && c < 96) d = String.fromCharCode(c - 64);
    ctrlOn = false; ctrl.classList.remove('on'); }
  send({i: d});
}
function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/herdr/ws?cols=' +
                     term.cols + '&rows=' + term.rows);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => { state.textContent = ''; term.focus(); };
  ws.onmessage = e => term.write(new Uint8Array(e.data));
  ws.onclose = () => { state.innerHTML = 'disconnected · <a href="" style="color:#82b3e3">reconnect</a>'; };
}
term.onData(input);
term.onBinary(d => send({i: d}));
term.onResize(({cols, rows}) => send({r: [cols, rows]}));
new ResizeObserver(() => fit.fit()).observe(document.getElementById('term'));
document.querySelectorAll('#keys button[data-k]').forEach(b => b.addEventListener('click', e => {
  e.preventDefault(); input(JSON.parse('"' + b.dataset.k + '"')); term.focus(); }));
ctrl.addEventListener('click', e => { e.preventDefault(); ctrlOn = !ctrlOn; ctrl.classList.toggle('on', ctrlOn); term.focus(); });
connect();
</script></body></html>
"""


# ---------------------------------------------------------------- tty256: 24-bit color -> 256 colors
# `stairway.py tty256 <cmd...>` runs cmd in a pseudo-terminal and rewrites 24-bit color codes
# (ESC[38;2;r;g;bm) to the nearest xterm-256 color, because screen 4 garbles them.

_CUBE = [0, 95, 135, 175, 215, 255]
_SGR = re.compile(rb"\x1b\[([0-9;:]*)m")


def _xterm256(r, g, b):
    def near(v):
        return min(range(6), key=lambda i: abs(_CUBE[i] - v))
    ri, gi, bi = near(r), near(g), near(b)
    cube = (16 + 36 * ri + 6 * gi + bi, (_CUBE[ri] - r) ** 2 + (_CUBE[gi] - g) ** 2 + (_CUBE[bi] - b) ** 2)
    level = min(23, max(0, round(((r + g + b) / 3 - 8) / 10)))
    gv = 8 + 10 * level
    gray = (232 + level, (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2)
    return min(cube, gray, key=lambda c: c[1])[0]


def _sgr_to_256(m):
    t, out, i = re.split(rb"[;:]", m.group(1)), [], 0
    while i < len(t):
        if t[i] in (b"38", b"48", b"58") and i + 1 < len(t) and t[i + 1] == b"2":
            rgb = t[i + 2:i + 5]
            skip = 5
            if len(t) >= i + 6 and rgb and rgb[0] == b"" and b":" in m.group(1):  # 38:2::r:g:b
                rgb, skip = t[i + 3:i + 6], 6
            try:
                r, g, b = (int(x or 0) for x in rgb)
                out += [t[i], b"5", str(_xterm256(r, g, b)).encode()]
            except ValueError:
                out.append(t[i])
            i += skip
            continue
        out.append(t[i])
        i += 1
    return b"\x1b[" + b";".join(out) + b"m"


def _write_all(fd, data):
    while data:
        data = data[os.write(fd, data):]


def tty256(argv):
    import fcntl, pty, signal, termios, tty
    pid, master = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)

    def resize(*_):
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
        except OSError:
            pass
    signal.signal(signal.SIGWINCH, resize)
    resize()
    saved = termios.tcgetattr(0) if os.isatty(0) else None
    if saved:
        tty.setraw(0)
    pending = b""
    try:
        while True:
            ready, _, _ = select.select([0, master], [], [])
            if 0 in ready:
                data = os.read(0, 65536)
                if not data:
                    break
                _write_all(master, data)
            if master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:  # the child closed the terminal
                    break
                if not data:
                    break
                data, pending = pending + data, b""
                cut = data.rfind(b"\x1b")
                if cut >= 0 and not re.search(rb"[@-~]", data[cut + 2:]) and len(data) - cut < 64:
                    data, pending = data[:cut], data[cut:]  # an escape sequence split across reads
                _write_all(1, _SGR.sub(_sgr_to_256, data))
    finally:
        if saved:
            termios.tcsetattr(0, termios.TCSADRAIN, saved)
    _, status = os.waitpid(pid, 0)
    return os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1


def main():
    if sys.argv[1:2] == ["tty256"]:
        sys.exit(tty256(sys.argv[2:]))
    sys.stderr = open(sys.stderr.fileno(), "w", buffering=1, closefd=False)  # line-buffered logs under systemd
    if len(TOKEN) < 16:
        sys.exit("Set STAIRWAY_TOKEN to a secret of at least 16 characters.")
    os.makedirs(ROOT, exist_ok=True)
    os.makedirs(STATE, mode=0o700, exist_ok=True)
    if RUNTIME not in ("screen", "herdr"):
        sys.exit("STAIRWAY_RUNTIME must be screen or herdr.")
    if RUNTIME == "herdr" and not HERDR:
        sys.exit("STAIRWAY_RUNTIME=herdr, but herdr is not installed; set STAIRWAY_HERDR.")
    print("stairway: http://%s:%d  (MCP at /mcp/<token>, status at /status), experiments root %s, runtime %s, "
          "code-server: %s" % (HOST, PORT, ROOT, RUNTIME if RUNTIME == "screen" else "herdr (%s, session %s)"
                               % (HERDR, HERDR_SESSION), CODE_SERVER), file=sys.stderr)
    if TG_TOKEN:
        threading.Thread(target=_tg_loop, daemon=True).start()
    # Finished replies go to bound Telegram chats and to experiments' callbacks.
    threading.Thread(target=_relay_loop, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
