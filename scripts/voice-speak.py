#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# Name:    voice-speak
# Purpose: The "mouth" of voiceLoop. As a Claude Code Stop hook it speaks the
#          end of each turn: it takes last_assistant_message, cleans the markdown
#          for speech (no code, tables, URLs or long paths), caps the length, and
#          speaks it in the background with macOS `say`. Run with arguments, it is
#          the `voice` CLI that toggles voice on and off.
# Usage:   Hook:   registered under hooks.Stop in ~/.claude/settings.json (stdin = payload)
#          CLI:    voice on | off | session [on|off] | stop | status | test [text]
#                  voice listen [on|wake|off|status|switch <project>|once [--wake --json]]
#                  voice setup   (interactive first-run wizard: mic, wake word, settings)
#                  voice words [add X | fix heard = Right | remove X]   (Whisper word list)
#                  voice benchmark [--models base,small] [--noise] [--quick] [--show]   (no mic)
#          Debug:  voice-speak.py --dry-run < payload.json   # print cleaned text only
# Deps:    stdlib only; macOS `say`. Optional ~/.claude/voice/config.json:
#          {"engine":"say"|"kokoro","voice":"Samantha","kokoro_voice":"bf_emma","rate":200,...}
#          engine "kokoro" runs voice-kokoro.py (same folder) detached, sentence by sentence.
# Notes:   Off by default. ON = ~/.claude/voice/enabled (all sessions) or
#          $TMPDIR/claude-voice.<session_id> (one session). Never blocks a turn:
#          prints nothing, always exits 0, speech runs detached with its output
#          to /dev/null (an inherited pipe would make Claude Code wait).
#          Installed to ~/.claude/hooks/ by install.sh (repo: voiceLoop, docs/HOW-IT-WORKS.md).
#          Lessons: [[A voice loop for Claude Code]], [[One mic, many sessions — routing voice to Claude panes]], [[Stop hooks]].

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

DEFAULTS = {
    "engine": "say",             # say | kokoro | none
    "kokoro_voice": "bf_emma",   # Kokoro voice (bf_/bm_ British, af_/am_ American)
    "kokoro_speed": 1.0,
    "voice": "",                 # say -v <name>; "" = system default voice
    "rate": 200,                 # words per minute
    "mode": "first_paragraph",   # first_paragraph | full
    "max_chars": 600,
}
REST = "The rest is on screen."
CODE = "Code block omitted."
TABLE = "Table omitted."
SPEECH_PROCS = {"say", "afplay"}  # only ever kill these (a stale PID may be reused)


# --- paths ------------------------------------------------------------------
def voice_dir() -> Path:
    return Path(os.environ.get("CLAUDE_VOICE_DIR") or Path.home() / ".claude" / "voice")


_TMP = None


def tmp_dir() -> Path:
    """$TMPDIR, else the per-user temp dir from the OS. launchd jobs (e.g. a dashboard service)
    have no TMPDIR, and os.confstr("CS_DARWIN_USER_TEMP_DIR") fails on Python 3.15, so
    ask getconf, which works everywhere. Falling back to /tmp would silently split state."""
    global _TMP
    if _TMP is None:
        t = os.environ.get("TMPDIR")
        if not t:
            try:
                t = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True,
                                   text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                t = ""
        _TMP = Path(t or "/tmp")
    return _TMP


def pid_file() -> Path:
    return tmp_dir() / "claude-voice.pid"


def last_file() -> Path:
    return tmp_dir() / "claude-voice.last.txt"


def safe_sid(sid) -> str:
    """Session IDs become file names, so allow only UUID-ish characters."""
    return sid if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9-]{1,80}", sid) else ""


def session_flag(sid: str) -> Path:
    return tmp_dir() / f"claude-voice.{sid}"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((voice_dir() / "config.json").read_text())
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def is_enabled(sid: str) -> bool:
    return (voice_dir() / "enabled").exists() or bool(sid and session_flag(sid).exists())


# --- getting the text ---------------------------------------------------------
def text_from_content(c) -> str:
    """message.content is a plain string OR a list of typed parts; keep text parts."""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        parts = [x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text"]
        return "\n\n".join(p for p in parts if p).strip()
    return ""


def text_from_transcript(path, wait: float = 0.3, tail_bytes: int = 1_000_000) -> str:
    """Fallback only. The transcript is written asynchronously: at Stop time the
    final reply is usually NOT in the file yet (measured: present by
    +200 ms). So wait, then accept the text only if the LAST user/assistant entry
    is an assistant one; otherwise we would read out the previous turn."""
    if not isinstance(path, str) or not path or "/subagents/" in path:
        return ""
    time.sleep(wait)
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    lines = data.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # the first line after a seek is probably cut in half
    last = None
    for line in lines:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") in ("user", "assistant"):
            last = o
    if not last or last.get("type") != "assistant":
        return ""
    return text_from_content((last.get("message") or {}).get("content"))


def get_text(payload: dict) -> str:
    msg = payload.get("last_assistant_message")
    if isinstance(msg, str) and msg.strip():
        return msg
    return text_from_transcript(payload.get("transcript_path"))


# --- cleaning markdown for the ear -------------------------------------------
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.M | re.S)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?")
RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
URL = re.compile(r"https?://[^\s<>()\[\]]*[^\s<>()\[\].,;:!?'\"]")
MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
ABS_PATH = re.compile(r"(?<![\w/:.])(?:~|\.{1,2})?/(?:[\w.@+-]+/)*([\w.@+-]+)/?")
REL_PATH = re.compile(r"\b(?:[\w.@+-]+/)+([\w@+-]+\.[A-Za-z0-9]{1,5})\b|\b(?:[\w.@+-]+/){2,}([\w.@+-]+)/?")
FILE_LINE = re.compile(r"\b([\w.@+-]+\.[A-Za-z0-9]{1,5}):\d+(?:[:-]\d+)?\b")
SYMBOLS = {"→": " to ", "⇒": " to ", "←": " ", "≈": "about ", "≥": "at least ", "≤": "at most ",
           "×": " times ", "&": " and ", "—": ", ", "–": "-", "…": "..."}
END_PUNCT = re.compile(r"[.!?:;,]$")


def _inline(s: str) -> str:
    s = MD_LINK.sub(r"\1", s)                       # [text](url) -> text
    s = URL.sub("a link", s)                        # bare URLs
    s = s.replace("`", "")                          # inline code -> plain words
    s = FILE_LINE.sub(r"\1", s)                     # file.py:123 -> file.py
    s = ABS_PATH.sub(r"\1", s)                      # /a/b/c.txt, ~/x/y -> basename
    s = REL_PATH.sub(lambda m: m.group(1) or m.group(2), s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)        # bold
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"\1", s)  # *italic*
    s = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", s)  # snake_case -> words
    s = re.sub(r"\be\.g\.", "for example", s)
    s = re.sub(r"\bi\.e\.", "that is", s)
    s = re.sub(r"~(?=\d)", "about ", s)
    for k, v in SYMBOLS.items():
        s = s.replace(k, v)
    s = re.sub(r"</?[A-Za-z][^>]*>", "", s)          # stray HTML tags
    s = "".join(ch for ch in s if unicodedata.category(ch) not in ("So", "Sk", "Cs", "Co"))
    return re.sub(r"\s+", " ", s).strip()


def blocks(md: str) -> list:
    """Markdown -> list of (kind, text); kind is 'p' (prose), 'h' (heading) or 'omit'."""
    md = FENCE.sub(lambda m: f"\n\n\x00{CODE}\n\n", md.replace("\r\n", "\n"))
    out, para, in_table = [], [], False

    def flush():
        if para:
            text = _inline(" ".join(para))
            if re.search(r"\w", text):
                out.append(("p", text))
            para.clear()

    for line in md.split("\n"):
        st = line.strip()
        if st.startswith("|"):
            if not in_table:
                flush()
                out.append(("omit", TABLE))
                in_table = True
            continue
        in_table = False
        if not st or RULE.match(st):
            flush()
            continue
        if st.startswith("\x00"):
            flush()
            out.append(("omit", st[1:]))
            continue
        h = HEADING.match(line)
        if h:
            flush()
            text = _inline(h.group(1))
            if text:
                out.append(("h", text if END_PUNCT.search(text) else text + "."))
            continue
        st = re.sub(r"^(?:>\s?)+", "", st)            # blockquotes
        if LIST_ITEM.match(st):
            st = LIST_ITEM.sub("", st)
            if not END_PUNCT.search(st):
                st += "."                            # a pause between list items
        para.append(st)
    flush()
    return out


def _cap(text: str, max_chars: int):
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if end >= max_chars * 0.4:
        return cut[: end + 1], True
    cut = cut[: cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(",;:- ") + "...", True


def clean_for_speech(md: str, mode: str = "first_paragraph", max_chars: int = 600) -> str:
    bl = blocks(md or "")
    if not bl:
        return ""
    if mode == "full":
        text, more = " ".join(t for _, t in bl), False
    else:
        prose = [(k, t) for k, t in bl if k != "omit"]
        if not prose:
            text = bl[0][1]
        else:
            text = prose[0][1]
            # a heading or a lead-in ending in ":" means nothing without what follows
            if (prose[0][0] == "h" or text.endswith(":")) and len(prose) > 1:
                text += " " + prose[1][1]
        more = len(bl) > 1 and text != " ".join(t for _, t in bl)
    text, cut = _cap(text, max(80, int(max_chars)))
    return f"{text} {REST}" if (more or cut) else text


# --- speaking -----------------------------------------------------------------
def stop_speech() -> bool:
    """Kill the current speech, if any. Checks the PID really is a speech process
    first: PIDs get reused, and we must never kill something else."""
    pf = pid_file()
    try:
        pid = int(pf.read_text().strip())
    except (OSError, ValueError):
        return False
    killed = False
    try:
        comm = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                              capture_output=True, text=True, timeout=2).stdout.strip()
        args = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                              capture_output=True, text=True, timeout=2).stdout
        # say/afplay by name; the Kokoro speaker runs as uv -> python, so match its script
        if Path(comm).name in SPEECH_PROCS or "voice-kokoro" in args:
            os.killpg(pid, signal.SIGTERM)  # start_new_session made it a group leader
            killed = True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        pf.unlink()
    except OSError:
        pass
    return killed


def speak(text: str, cfg: dict, sid: str = "") -> None:
    stop_speech()                 # one voice at a time: new speech replaces old
    try:
        last_file().write_text(text)
        if sid:   # tells THIS session's listener its reply has started (no text in it)
            (tmp_dir() / f"claude-voice.spoke.{sid}").write_text("")
    except OSError:
        pass
    if not text or cfg.get("engine") == "none":
        return
    env = None
    kokoro = Path(__file__).resolve().parent / "voice-kokoro.py"
    if cfg.get("engine") == "kokoro" and kokoro.exists():
        # neural voice, sentence by sentence; falls back to `say` by itself on any failure
        cmd = [str(kokoro), "--voice", str(cfg.get("kokoro_voice") or "bf_emma"),
               "--speed", str(float(cfg.get("kokoro_speed") or 1.0)),
               "--fallback-voice", str(cfg.get("voice") or "Samantha")]
        env = {**os.environ, "VOICELOOP_T0": str(time.time())}   # for the first-audio timing log
    else:
        cmd = ["say", "-r", str(int(cfg.get("rate") or 200))]
        if cfg.get("voice"):
            cmd += ["-v", str(cfg["voice"])]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
                         env=env)
    p.stdin.write(text.encode("utf-8"))   # stdin, not argv: text may start with "-"
    p.stdin.close()
    pid_file().write_text(str(p.pid))


# --- pane registry (for "switch to <project>" in voice-listen.py) ---------------
def panes_dir() -> Path:
    return tmp_dir() / "claude-voice-panes"


def register_pane(payload: dict) -> None:
    """Remember which iTerm pane each Claude session lives in. The hook inherits
    ITERM_SESSION_ID from Claude Code; the payload brings cwd. Runs on every
    Stop, voice on or off, so every open project is switchable."""
    sid = safe_sid(payload.get("session_id"))
    pane = os.environ.get("ITERM_SESSION_ID", "")
    cwd = payload.get("cwd")
    if not (sid and re.fullmatch(r"[A-Za-z0-9:-]{1,80}", pane) and isinstance(cwd, str)):
        return
    d = panes_dir()
    d.mkdir(exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps(
        {"claude_session": sid, "iterm_session": pane, "cwd": cwd,
         "project": Path(cwd).name, "ts": time.time()}))


# --- hook mode ----------------------------------------------------------------
SPOKEN_CONTEXT = (
    "voiceLoop: this message was SPOKEN (hands-free conversation mode) and your reply's "
    "first paragraph will be read aloud. Open with a direct answer in 1-3 short, plain "
    "sentences (no markdown, code, paths or lists in that first paragraph); put any "
    "detail after it. If the message looks mis-transcribed, ask briefly.")


def listening_to(sid: str) -> bool:
    """True if voice-listen.py is running AND currently targets this session."""
    try:
        target = (tmp_dir() / "claude-listen.target").read_text().strip()
    except OSError:
        return False
    return bool(sid) and target == sid and listener_pid() != 0


def prompt_submit(payload: dict) -> None:
    """UserPromptSubmit: a new message ends any speech still playing (typing over
    Samantha shuts her up), and in conversation mode it gets the short-answer note."""
    stop_speech()
    if listening_to(safe_sid(payload.get("session_id"))):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": SPOKEN_CONTEXT}}))


def hook_main(dry_run: bool = False) -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    if not isinstance(payload, dict):
        return
    if payload.get("hook_event_name") == "UserPromptSubmit":
        prompt_submit(payload)
        return
    if not dry_run:
        try:
            register_pane(payload)
        except OSError:
            pass
    if payload.get("stop_hook_active"):
        return  # a continuation forced by another Stop hook: already spoke this turn
    sid = safe_sid(payload.get("session_id"))
    if not dry_run and not is_enabled(sid):
        return
    cfg = load_config()
    text = clean_for_speech(get_text(payload), str(cfg["mode"]), cfg["max_chars"])
    if dry_run:
        print(text)
    elif text:
        speak(text, cfg, sid)


# --- CLI mode -----------------------------------------------------------------
USAGE = """usage: voice on | off | session [on|off] | stop | status | test [text]
  on            speak in ALL Claude sessions
  off [all]     silence everywhere (global + this session; `all` = every session) and stop
  session [on]  speak only in this Claude session ($CLAUDE_CODE_SESSION_ID)
  session off   turn this session's voice off
  stop          stop the current speech now
  status        show what is on
  test [text]   speak a sample (works even when voice is off)
  listen [on]   conversation mode: hands-free, sends after a pause (voice-listen.py)
  listen wake   same, but only sentences starting "Claude, …" are sent
  listen off    end conversation mode       listen status   show it + recent log
  listen switch <project>   retarget the running listener
  status --json machine-readable state for dashboards (no transcript text)
  words [add X | fix heard = Right | remove X]   the word list Whisper should spell right
  setup         interactive first-run wizard (run it in a terminal window)
  benchmark     score the Whisper models on computer-spoken sentences, no mic needed
                [--models base,small] [--noise] [--quick] [--show] [--json]"""


def listener_pid() -> int:
    """PID of a running voice-listen.py, or 0. Verifies the process really is it."""
    try:
        pid = int((tmp_dir() / "claude-listen.pid").read_text().strip())
        args = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                              capture_output=True, text=True, timeout=2).stdout
        return pid if "voice-listen" in args else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def listen_cli(sub: str, rest: list = ()) -> int:
    pid = listener_pid()
    if sub == "once":   # app mode: blocks until one utterance, prints it (see voice-listen.py)
        script = Path(__file__).resolve().parent / "voice-listen.py"
        os.execv(str(script), [str(script), "--once", *rest])
    if sub == "switch":
        name = " ".join(rest).strip()
        if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,60}", name):
            print("listen: usage: voice listen switch <project>", file=sys.stderr)
            return 2
        if not pid:
            print("listen: not running", file=sys.stderr)
            return 1
        (tmp_dir() / "claude-listen.control").write_text(f"switch {name}")
        print(f"listen: switch to {name} requested")
        return 0
    if sub == "off":
        if not pid:
            print("listen: not running")
            return 0
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)  # uv + python + rec, one group
        except OSError:
            pass
        (tmp_dir() / "claude-listen.pid").unlink(missing_ok=True)
        print("listen: OFF")
    elif sub == "status":
        print(f"listen: {'ON (pid %d)' % pid if pid else 'off'}")
        try:
            print("".join((tmp_dir() / "claude-listen.log").read_text().splitlines(True)[-8:]), end="")
        except OSError:
            pass
    else:
        if pid:
            print(f"listen: already ON (pid {pid})")
            return 0
        if not os.environ.get("ITERM_SESSION_ID"):
            print("listen: no $ITERM_SESSION_ID; run this from Claude Code inside iTerm", file=sys.stderr)
            return 1
        script = Path(__file__).resolve().parent / "voice-listen.py"
        extra = ["--wake"] if sub == "wake" else []
        subprocess.Popen([str(script), *extra], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        print("listen: ON" + (" (wake word: start with 'Claude')" if extra else "")
              + ". Talk; a 2.5 s pause sends it. Say 'stop listening' to end.")
    return 0


def _mtime_iso(path: Path):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime))
    except OSError:
        return None


def status_snapshot() -> dict:
    """Machine-readable state for external tools (dashboards). Deliberately contains NO text:
    not what was heard (the listener log holds your words) and not what was spoken.
    Such tools may be shown to anyone who can see the screen."""
    cfg = load_config()
    t = tmp_dir()
    panes = {}
    for f in panes_dir().glob("*.json"):
        try:
            p = json.loads(f.read_text())
            panes[p["claude_session"]] = p
        except (OSError, ValueError, KeyError):
            continue
    proj = lambda sid: panes.get(sid, {}).get("project", sid[:8])   # noqa: E731
    sessions = sorted(proj(f.name.split(".", 1)[1]) for f in t.glob("claude-voice.*")
                      if re.fullmatch(r"claude-voice\.[A-Za-z0-9-]{8,80}", f.name))
    try:
        playing = bool(int(pid_file().read_text()) and os.kill(int(pid_file().read_text()), 0) is None)
    except (OSError, ValueError):
        playing = False
    try:
        latency = (t / "claude-voice.timing").read_text().split("after")[-1].strip()
    except OSError:
        latency = None
    lpid = listener_pid()
    mode = target = None
    if lpid:
        args = subprocess.run(["ps", "-p", str(lpid), "-o", "args="], capture_output=True,
                              text=True, timeout=2).stdout
        mode = "wake" if "--wake" in args else "plain"
        try:
            target = proj((t / "claude-listen.target").read_text().strip())
        except OSError:
            pass
    last = {}
    try:   # outcome + time of the newest log line only; never its text
        for line in reversed((t / "claude-listen.log").read_text().splitlines()[-30:]):
            for key, kind in (("sent;", "sent"), ("ignored", "ignored"), ("holding", "held"),
                              ("switched to", "switched"), ("nothing usable", "nothing"),
                              ("conversation mode OFF", "stopped"), ("heard (", "heard")):
                if key in line:
                    last = {"at": line[:8], "kind": kind}
                    break
            if last:
                break
    except OSError:
        pass
    try:
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        reg = lambda ev: any("voice-speak.py" in h.get("command", "")   # noqa: E731
                             for g in settings.get("hooks", {}).get(ev, []) for h in g.get("hooks", []))
        hooks = {"stop": reg("Stop"), "userPromptSubmit": reg("UserPromptSubmit")}
    except (OSError, ValueError, AttributeError):
        hooks = {}
    engine = cfg.get("engine", "say")
    try:   # spoken slash commands (names only): the listener's allowlist, same default as there
        listen = json.loads((voice_dir() / "config.json").read_text()).get("listen", {})
    except (OSError, ValueError, AttributeError):
        listen = {}
    allowed = listen.get("commands") if isinstance(listen.get("commands"), dict) else \
        {"clear": {"confirm": True}, "compact": {}}
    commands = [{"name": str(k), "confirm": bool((v or {}).get("confirm"))} for k, v in allowed.items()]
    return {
        "speaking": {"global": (voice_dir() / "enabled").exists(), "sessions": sessions,
                     "engine": engine,
                     "voice": cfg.get("kokoro_voice") if engine == "kokoro" else (cfg.get("voice") or "default"),
                     "playingNow": playing, "lastSpokenAt": _mtime_iso(last_file()),
                     "lastLatency": latency},
        "listening": {"on": bool(lpid), "mode": mode, "target": target, "last": last or None},
        "projects": sorted({p.get("project") for p in panes.values() if p.get("project")}),
        "hooks": hooks,
        "commands": commands,
    }


def ensure_config() -> None:
    vd = voice_dir()
    vd.mkdir(parents=True, exist_ok=True)
    cf = vd / "config.json"
    if not cf.exists():
        cf.write_text(json.dumps(DEFAULTS, indent=2) + "\n")


def cli(args: list) -> int:
    cmd = args[0] if args else "status"
    sid = safe_sid(os.environ.get("CLAUDE_CODE_SESSION_ID"))
    if cmd == "on":
        ensure_config()
        (voice_dir() / "enabled").touch()
        print("voice: ON for all sessions")
    elif cmd == "off":
        (voice_dir() / "enabled").unlink(missing_ok=True)
        if sid:
            session_flag(sid).unlink(missing_ok=True)
        if len(args) > 1 and args[1] == "all":   # callers without a session (scripts, dashboards)
            for fl in tmp_dir().glob("claude-voice.*"):
                if re.fullmatch(r"claude-voice\.[A-Za-z0-9-]{8,80}", fl.name):
                    fl.unlink(missing_ok=True)
        stop_speech()
        print("voice: OFF" + (" (all sessions)" if len(args) > 1 and args[1] == "all" else ""))
    elif cmd == "session":
        if not sid:
            print("voice: no $CLAUDE_CODE_SESSION_ID — run this from inside Claude Code", file=sys.stderr)
            return 1
        if len(args) > 1 and args[1] == "off":
            session_flag(sid).unlink(missing_ok=True)
            stop_speech()
            print(f"voice: OFF for session {sid[:8]}")
        else:
            ensure_config()
            session_flag(sid).touch()
            print(f"voice: ON for session {sid[:8]} (until reboot or `voice session off`)")
    elif cmd == "stop":
        print("voice: stopped" if stop_speech() else "voice: nothing was playing")
    elif cmd == "status" and "--json" in args:
        print(json.dumps(status_snapshot()))
    elif cmd == "status":
        g = (voice_dir() / "enabled").exists()
        s = bool(sid and session_flag(sid).exists())
        cfg = load_config()
        print(f"global:  {'ON' if g else 'off'}")
        print(f"session: {'ON' if s else 'off'}" + (f"  ({sid[:8]})" if sid else "  (not inside Claude Code)"))
        print(f"config:  {json.dumps(cfg)}")
        try:
            print(f"last:    {last_file().read_text()[:120]!r}")
        except OSError:
            pass
    elif cmd == "words":   # word list for Whisper; lives in voice-listen.py (it reads it)
        script = Path(__file__).resolve().parent / "voice-listen.py"
        os.execv(str(script), [str(script), "--words", *args[1:]])
    elif cmd == "benchmark":   # Whisper scoring; lives in voice-listen.py (it owns transcribe)
        script = Path(__file__).resolve().parent / "voice-listen.py"
        os.execv(str(script), [str(script), "--benchmark", *args[1:]])
    elif cmd == "setup":   # interactive wizard; lives in voice-listen.py (it owns the mic code)
        if any(x in ("-h", "--help", "help") for x in args[1:]):
            print("voice setup: interactive first-run wizard, about 3 minutes. Run it in a terminal\n"
                  "window. Steps: mic check + level meter, room noise, wake-word training (say\n"
                  "\"Claude\" 5 times), a speak-and-hear test, the main settings, a cheat sheet.\n"
                  "Nothing is saved without a yes; ~/.claude/voice/config.json is backed up first.")
            return 0
        script = Path(__file__).resolve().parent / "voice-listen.py"
        os.execv(str(script), [str(script), "--setup"])
    elif cmd == "listen":
        return listen_cli(args[1] if len(args) > 1 else "on", args[2:])
    elif cmd == "test":
        sample = " ".join(args[1:]) or "Voice loop test. This is how Claude will sound."
        speak(clean_for_speech(sample, "full", 10_000), load_config())
        print("voice: speaking test sample")
    else:
        print(USAGE)
        return 0 if cmd in ("-h", "--help", "help") else 2
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args and Path(sys.argv[0]).name == "voice":
        args = ["status"]        # bare `voice` = status; the hook runs as voice-speak.py
    if args and args[0] == "--dry-run":
        hook_main(dry_run=True)  # debugging: let errors show
        return 0
    if not args:
        try:
            hook_main()
        except Exception:
            pass  # a live hook must never break a turn
        return 0
    return cli(args)


if __name__ == "__main__":
    sys.exit(main())
