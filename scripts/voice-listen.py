#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# Name:    voice-listen
# Purpose: voiceLoop's "ears" in conversation mode: hands-free, half-duplex
#          talking to Claude Code. Loop: record (ends after N s of quiet; 0.5 s
#          pre-roll keeps soft first syllables) -> transcribe locally (whisper.cpp
#          + Silero VAD) -> type it into the Claude pane by iTerm session ID and
#          press Enter -> close the mic until the reply has been spoken
#          (voice-speak.py) -> listen again. Local commands (never sent to Claude):
#          "switch to <project>", "which projects", "voice help", "stop listening".
#          Spoken slash commands: "command clear" / "slash compact" -> /clear, /compact,
#          ONLY for names in "commands" (config); "clear" asks for a spoken yes first.
#          Wake mode (--wake / `voice listen wake`): only "Claude, …" is sent, plus
#          a 30 s follow-up window after each reply.
#          Panes are known from the registry voice-speak.py's Stop hook writes.
# Usage:   Normally via the voice CLI, from inside Claude Code (it needs the
#          pane's $ITERM_SESSION_ID and $CLAUDE_CODE_SESSION_ID):
#            voice listen [on] | voice listen off | voice listen status
#          Direct (foreground, for debugging):
#            voice-listen.py --iterm-session <id> --claude-session <id>
#          App mode (Claude desktop app, Code tab: no iTerm pane to type into; the
#          session runs this as a background command and IS its own ears):
#            voice listen once [--wake] [--json] [--timeout 120]
#            = voice-listen.py --once …  → prints {"status": "heard"|"stop"|"timeout"|
#              "busy"|"fail", "heard": "<text>"}. See /voice-app and docs/HOW-IT-WORKS.md.
#          Setup wizard: voice setup (= voice-listen.py --setup): mic, room, wake-word
#            training, speak-and-hear test, settings, cheat sheet.
#          Room check (used by install.sh): voice-listen.py --measure-room [2]
#            → {"ok", "median", "p95", "max", "threshold": "0.9%"} (stay quiet while it records)
# Deps:    stdlib; brew whisper-cpp + sox; models in ~/.local/share/voiceloop/models/
#          (ggml-base.en.bin, ggml-silero-v5.1.2.bin). Settings: "listen" object in
#          ~/.claude/voice/config.json.
# Notes:   The mic permission (TCC) belongs to the app that started us (iTerm).
#          If it's denied, sox records digital silence and we never trigger; see the
#          log. Heard text is sanitized: a leading ! / or # is stripped so speech can
#          never become a shell or slash command. Half-duplex: the mic is closed while
#          Claude thinks and speaks, so we never transcribe our own TTS.
#          Vocabulary: global "prompt"/"replacements" in config.json, plus the TARGET
#          project's <project>/.claude/voice.json (vocabulary list + replacements map).
#          Safety: stops after idle_minutes with nothing sent, and checks before EVERY
#          send that Claude still runs in the pane (otherwise speech would hit zsh).
#          Log: $TMPDIR/claude-listen.log. Lessons: [[A voice loop for Claude Code]], [[One mic, many sessions — routing voice to Claude panes]],
#          [[Whisper invents words from silence]], [[Hands-free speech-to-text with whisper.cpp]].

from __future__ import annotations

import argparse
import array
import collections
import difflib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

MODELS = Path.home() / ".local" / "share" / "voiceloop" / "models"
DEFAULTS = {
    "silence_seconds": 2.5,      # quiet this long = you're done talking
    "threshold": "0.5%",         # RMS loudness that counts as sound (raise in a noisy room)
    "preroll": 0.5,              # seconds kept from BEFORE the trigger (soft first syllables)
    "max_seconds": 60,           # hard cap on one utterance
    "model": str(MODELS / "ggml-base.en.bin"),
    "vad_model": str(MODELS / "ggml-silero-v5.1.2.bin"),
    # Vocabulary hint for Whisper + spelling fixes. Put YOUR words in config.json ("listen":
    # {"prompt": "...", "replacements": {...}}) or per project in <project>/.claude/voice.json.
    "prompt": "Claude, Claude Code, iTerm, GitHub, Python, JavaScript, TypeScript, JSON, API, macOS",
    "replacements": {"claude code": "Claude Code", "i term": "iTerm", "git hub": "GitHub",
                     "mac os": "macOS", "type script": "TypeScript", "java script": "JavaScript"},
    "stop_phrases": ["stop listening"],
    "reply_timeout": 600,        # max seconds to wait for Claude to answer + speak
    "cooldown": 0.6,             # pause after speech ends, so room echo dies down
    "wake": False,               # True = only sentences starting "Claude, …" are sent
    "follow_up_seconds": 30,     # after a reply, answer without the wake word for this long
    "wake_words": [],            # extra spellings of "Claude" learned by `voice setup` on this Mac
                                 # ("Bot", "Pod"); like "Cloud" they count only when punctuation
                                 # or the end follows, so "Pod racing" stays an ordinary sentence
    # Earcons (macOS system sounds by name, or a file path; "" = off). Always played while
    # the mic is CLOSED, so the listener never records its own cues.
    # listening = mode starts / "Claude?" woke it; accepted = sent; ready = your turn; stopped = ended
    "sounds": {"listening": "Tink", "accepted": "Purr", "ready": "", "stopped": "Basso"},
    "sound_volume": 0.3,
    "sound_volumes": {},         # per-cue override, e.g. {"listening": 0.6}
    # Spoken slash commands: "command clear" / "slash compact" types /clear, /compact. ONLY
    # names listed here run; anything else is sent to Claude as an ordinary sentence.
    # confirm = ask "…? Say yes." first; reply = False for commands Claude doesn't answer
    # (the mic must not wait for a spoken reply that never comes).
    "commands": {"clear": {"confirm": True, "reply": False, "ask": "Clear this conversation?"},
                 "compact": {"reply": False}},
    "confirm_seconds": 8,        # how long a confirmation waits for "yes"
    "idle_minutes": 20,          # nothing sent for this long -> stop listening (mic off)
    "pane_check_seconds": 60,    # while idle, re-check that Claude still runs in the pane
}
# Whisper's classic inventions on silence/noise. Matched against the WHOLE
# transcript only, so a real sentence containing these words still goes through.
# Deliberately NOT included: a bare "thank you" (people really say it).
HALLUCINATIONS = {"you", "thanks for watching", "thank you for watching", "subscribe",
                  "please subscribe", "bye", "so", "okay", "the end", "thanks for watching!"}


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


def voice_dir() -> Path:
    return Path(os.environ.get("CLAUDE_VOICE_DIR") or Path.home() / ".claude" / "voice")


LOG = lambda: tmp_dir() / "claude-listen.log"          # noqa: E731
PIDFILE = lambda: tmp_dir() / "claude-listen.pid"      # noqa: E731
TARGET = lambda: tmp_dir() / "claude-listen.target"   # noqa: E731  (current Claude session)
CONTROL = lambda: tmp_dir() / "claude-listen.control"  # noqa: E731  ("switch <project>" | "stop", from `voice listen switch` or a dashboard)


def take_control():
    """One pending control request, or None. Read-and-delete, so it runs once."""
    try:
        raw = CONTROL().read_text().strip()
        CONTROL().unlink()
    except OSError:
        return None
    if raw == "stop":
        return ("stop", "")
    m = re.fullmatch(r"switch ([A-Za-z0-9 ._-]{1,60})", raw)
    return ("switch", m.group(1)) if m else None
LAST_SPOKEN = lambda: tmp_dir() / "claude-voice.last.txt"  # written by voice-speak.py
SPEAK_PID = lambda: tmp_dir() / "claude-voice.pid"         # written by voice-speak.py


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((voice_dir() / "config.json").read_text()).get("listen", {})
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS and k != "sounds"})
            if isinstance(user.get("sounds"), dict):   # MERGE: a user list must not hide new cues
                cfg["sounds"] = {**DEFAULTS["sounds"], **user["sounds"]}
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)   # stderr: stdout is --once's JSON result
    try:
        with LOG().open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- ears ---------------------------------------------------------------------
RATE, FRAME = 16000, 480            # 30 ms frames of 16-bit mono
FRAME_S = FRAME / RATE


def _rms(frame: bytes) -> float:
    a = array.array("h", frame[: len(frame) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) / 32768 if a else 0.0


def _threshold(cfg: dict) -> float:
    t = str(cfg["threshold"]).strip()
    return float(t[:-1]) / 100 if t.endswith("%") else float(t)


def suggest_threshold(levels: list) -> dict:
    """Room noise (RMS per 30 ms frame, 0..1) -> a threshold safely above it.
    Rule: max(0.5 %, 2 x median, 1.5 x the 95th percentile), rounded UP to 0.1 %.
    A quiet laptop mic (median 0.43 %, max 0.55 %) gets 0.9 %; under the 0.5 %
    default its recordings never ended. All zeros = no signal (permission?)."""
    if not levels or max(levels) == 0:
        return {"ok": False, "why": "no signal: the mic gave digital silence (Microphone permission?)"}
    v = sorted(levels)
    med, p95 = v[len(v) // 2], v[min(len(v) - 1, int(len(v) * 0.95))]
    pct = max(0.5, 2 * med * 100, 1.5 * p95 * 100)
    pct = math.ceil(round(pct * 10, 6)) / 10
    return {"ok": True, "median": round(med * 100, 3), "p95": round(p95 * 100, 3),
            "max": round(v[-1] * 100, 3), "threshold": f"{pct:g}%"}


def measure_room(secs: float = 2.0) -> dict:
    """Record `secs` of room sound (stay quiet) and suggest a threshold."""
    p = subprocess.run(["rec", "-q", "-r", str(RATE), "-c", "1", "-b", "16", "-e", "signed-integer",
                        "-t", "raw", "-", "trim", "0", str(secs)],
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=secs + 15)
    data, n = p.stdout, FRAME * 2
    return suggest_threshold([_rms(data[i:i + n]) for i in range(0, len(data) - n + 1, n)])


def capture(stream, cfg: dict, interrupted=lambda: False, deadline: float = 0.0):
    """Endpointing with pre-roll, from any stream of raw 16 kHz 16-bit mono PCM.
    Idle: keep the last `preroll` seconds in a ring buffer. Start: 0.1 s above the
    threshold; the buffer is prepended, so a soft first syllable isn't lost (sox's
    silence effect drops it: "What happens" came out as "weapons"). Stop: after
    `silence_seconds` below the threshold, or `max_seconds`.
    `deadline` (epoch s): give up with "timeout" if nobody has started talking by then.
    Returns ("ok", pcm) | ("spoke", b"") | ("timeout", b"") | ("fail", b"")."""
    thr = _threshold(cfg)
    pre = collections.deque(maxlen=max(1, int(float(cfg["preroll"]) / FRAME_S)))
    start_need = max(1, int(0.1 / FRAME_S))
    stop_need = int(float(cfg["silence_seconds"]) / FRAME_S)
    max_frames = int(float(cfg["max_seconds"]) / FRAME_S)
    loud = quiet = n = 0
    out = None
    while True:
        frame = stream.read(FRAME * 2)
        if not frame:
            return ("ok", b"".join(out)) if out else ("fail", b"")
        n += 1
        if n % 3 == 0:
            if interrupted():
                return "spoke", b""
            if out is None and deadline and time.time() > deadline:
                return "timeout", b""
        level = _rms(frame)
        if out is None:
            pre.append(frame)
            loud = loud + 1 if level > thr else 0
            if loud >= start_need:
                out = list(pre)
        else:
            out.append(frame)
            quiet = quiet + 1 if level <= thr else 0
            if quiet >= stop_need or len(out) >= max_frames:
                return "ok", b"".join(out)


def write_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def record(cfg: dict, wav: str, deadline: float = 0.0) -> str:
    """Mic -> capture() -> wav. Returns "ok", "fail", or "spoke": TTS started while
    the mic was open (e.g. the reply to something typed), so the recording is
    discarded. Otherwise we'd transcribe Samantha and send her words back to Claude."""
    p = subprocess.Popen(["rec", "-q", "-r", str(RATE), "-c", "1", "-b", "16",
                          "-e", "signed-integer", "-t", "raw", "-"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    try:
        status, pcm = capture(p.stdout, cfg, interrupted=lambda: speaking() or CONTROL().exists(),
                              deadline=deadline)
    finally:
        p.terminate()
        p.wait()
    if status == "ok":
        write_wav(wav, pcm)
    return status


def wait_quiet(cfg: dict) -> None:
    while speaking():
        time.sleep(0.2)
    time.sleep(float(cfg["cooldown"]))


def transcribe(cfg: dict, wav: str) -> str:
    cmd = ["whisper-cli", "-m", cfg["model"], "-nt", "-np", "-f", wav,
           "--prompt", str(cfg["prompt"])]
    if Path(cfg["vad_model"]).exists():
        cmd += ["--vad", "-vm", cfg["vad_model"]]   # drops non-speech: the main anti-hallucination guard
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return r.stdout.strip()


# --- per-project vocabulary ---------------------------------------------------------
PROMPT_MAX = 600   # chars; whisper's prompt is capped at ~224 tokens, keep well under


def project_vocab(cwd: str) -> dict:
    """<project>/.claude/voice.json: {"vocabulary": [...], "replacements": {...}}.
    Read fresh on every utterance, so edits apply to the next sentence."""
    out = {"vocabulary": [], "replacements": {}}
    if not cwd:
        return out
    try:
        d = json.loads((Path(cwd) / ".claude" / "voice.json").read_text())
    except (OSError, ValueError):
        return out
    if isinstance(d, dict):
        v = d.get("vocabulary")
        if isinstance(v, list):
            out["vocabulary"] = [t.strip()[:60] for t in v if isinstance(t, str) and t.strip()]
        r = d.get("replacements")
        if isinstance(r, dict):
            out["replacements"] = {k: v for k, v in r.items() if isinstance(k, str) and isinstance(v, str)}
    return out


def effective(cfg: dict, cwd: str) -> dict:
    """Global settings + the target project's vocabulary. Project terms go FIRST in
    the prompt (they matter most, and the prompt has a size cap); project
    replacements win over global ones."""
    pv = project_vocab(cwd)
    e = dict(cfg)
    terms, seen = [], set()
    for t in pv["vocabulary"] + [x.strip() for x in str(cfg["prompt"]).split(",")]:
        if t and t.lower() not in seen and len(", ".join(terms + [t])) <= PROMPT_MAX:
            terms.append(t)
            seen.add(t.lower())
    e["prompt"] = ", ".join(terms)
    e["replacements"] = {**(cfg.get("replacements") or {}), **pv["replacements"]}
    return e


def defang(text: str) -> str:
    """Never a shell (!), slash (/) or memory (#) command: strip EVERY leading !, / or #,
    including after spaces ("! /clear"). Applied in clean() AND after the wake word comes
    off in Gate.feed() ("Claude, /clear" -> "clear"; bug found in review 2026-11-25)."""
    return re.sub(r"^[\s!/#]+", "", text)


def clean(text: str, cfg: dict) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text)  # [BLANK_AUDIO], (music), *sigh*
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)                  # no control chars / newlines
    text = re.sub(r"\s+", " ", text).strip()
    for wrong, right in (cfg.get("replacements") or {}).items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.I)
    text = defang(text).strip()                                  # never a shell / slash command
    if text.lower().strip(" .!?,") in HALLUCINATIONS or not re.search(r"[A-Za-z0-9]", text):
        return ""
    return text


DANGLING = {"and", "but", "or", "so", "because", "um", "uh", "like", "then", "also",
            "the", "a", "an", "to", "with", "if", "that", "which", "of", "for"}


def is_incomplete(text: str) -> bool:
    """A thinking pause, not the end: the utterance ends on a word nobody ends on
    ("and…", "so…", "because…"). Whisper often adds a period after it, so strip
    punctuation before looking at the last word."""
    words = re.sub(r"[^a-z' ]", " ", text.lower()).split()
    if not words or words[-1] not in DANGLING:
        return False
    end = text.rstrip()[-1:]
    if end in "?!":          # "Can we work on that?" is a finished question, never a pause
        return False
    if end == "." and words[-1] in ENDS_OK:   # "I like that." ends fine; "and." doesn't
        return False
    return True


ENDS_OK = {"that", "which", "too", "also", "then", "like"}   # real sentence ends


def is_stop(text: str, cfg: dict) -> bool:
    t = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return any(t == p or t.endswith(" " + p) for p in cfg["stop_phrases"])


# --- mouth of the loop: type into the Claude pane -----------------------------
SEND_SCRIPT = r'''
on run argv
  set sid to item 1 of argv
  set mode to item 2 of argv
  tell application id "com.googlecode.iterm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique id of s is sid then
            if mode is "tty" then return tty of s
            tell s to write text (item 3 of argv) newline NO
            delay 0.15
            tell s to write text ""
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''


def send(iterm_sid: str, text: str) -> bool:
    """write text … newline NO, then an empty write = a separate Enter. Typing text
    and newline as one burst can look like a paste to a TUI (newline inserted, not
    submitted). Text goes in as an argv item, never spliced into the script."""
    uuid = iterm_sid.split(":")[-1]
    r = subprocess.run(["osascript", "-e", SEND_SCRIPT, uuid, "send", text],
                       capture_output=True, text=True, timeout=15)
    if r.stdout.strip() != "ok":
        log(f"send failed: {r.stdout.strip() or r.stderr.strip()}")
        return False
    return True


def _has_claude(ps_out: str) -> bool:
    return any(Path(line.split(None, 1)[-1].strip()).name == "claude"
               for line in ps_out.splitlines() if line.strip())


def claude_in_pane(iterm_sid: str) -> bool:
    """Is Claude Code still running in that pane? If you exit Claude, the pane falls
    back to zsh, and typed speech + Enter would RUN AS A SHELL COMMAND. So ask iTerm
    for the pane's tty, then look at what runs on it."""
    try:
        r = subprocess.run(["osascript", "-e", SEND_SCRIPT, iterm_sid.split(":")[-1], "tty"],
                           capture_output=True, text=True, timeout=10)
        tty = r.stdout.strip()
        if not tty.startswith("/dev/"):
            return False
        ps = subprocess.run(["ps", "-t", tty[5:], "-o", "pid=,comm="],
                            capture_output=True, text=True, timeout=5).stdout
        return _has_claude(ps)
    except (OSError, subprocess.SubprocessError):
        return False


def speaking() -> bool:
    try:
        pid = int(SPEAK_PID().read_text().strip())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def speech_on(claude_sid: str) -> bool:
    return (voice_dir() / "enabled").exists() or bool(
        claude_sid and (tmp_dir() / f"claude-voice.{claude_sid}").exists())


def wait_for_reply(cfg: dict, sent_at: float, claude_sid: str = "") -> None:
    """Half-duplex: keep the mic closed until Claude's reply has been spoken.
    voice-speak.py rewrites LAST_SPOKEN when it starts speaking; then we wait
    for its `say` process to exit."""
    deadline = time.time() + float(cfg["reply_timeout"])
    while time.time() < deadline:
        if not speech_on(claude_sid):
            return   # speaking was turned off (e.g. by `voice off all`): no reply will be spoken
        if CONTROL().exists():
            log("control request pending; stopped waiting for the reply")
            return   # a switch/stop request (CLI or dashboard) beats waiting for an old reply
        try:
            if LAST_SPOKEN().stat().st_mtime > sent_at:
                break
        except OSError:
            pass
        time.sleep(0.25)
    else:
        log("no spoken reply before timeout; listening again")
        return
    wait_quiet(cfg)


# --- local voice commands (never sent to Claude) -----------------------------------
SWITCH = re.compile(r"^(?:ok(?:ay)?,?\s+|please\s+)?switch(?:\s+over)?\s+to\s+(?:the\s+)?"
                    r"(?:project\s+)?(.+?)(?:\s+project)?[\s.!?]*$", re.I)
LIST = re.compile(r"^(?:which|what|list)(?:\s+the)?\s+projects?\b", re.I)
HELP = re.compile(r"^voice\s+help\b", re.I)
HELP_TEXT = ("Say: switch to, and a project name. Which projects. Command, and a command name. "
             "Or stop listening. Anything else goes to Claude.")

# Spoken slash commands, on purpose: a trigger word ("command" / "slash") + an allowlist
# (cfg["commands"]) + an optional spoken yes. A stray "/" in a transcript never runs
# anything (defang); this path does, deliberately. Unknown names fall through to Claude.
COMMAND = re.compile(r"^(?:please\s+)?(?:run\s+)?(?:the\s+)?(?:command|slash)[\s,:.-]+(.+?)[\s.!?]*$", re.I)
YES = re.compile(r"^(?:yes|yeah|yep|yup|sure|do it|confirm(?:ed)?|go ahead)\b", re.I)


def parse_command(text: str, allowed: dict):
    """'command wrap session' -> ("wrap-session", {opts}) when allowed, else None."""
    m = COMMAND.match(text.strip())
    if not m or not allowed:
        return None
    names = {norm(k): k for k in allowed}
    hit = difflib.get_close_matches(norm(m.group(1)), list(names), n=1, cutoff=0.8)
    return (names[hit[0]], dict(allowed[names[hit[0]]] or {})) if hit else None


def confirm_by_voice(cfg: dict, wav: str, cwd: str = "", rec=None, stt=None) -> bool:
    """After the question was asked: True only if the next utterance starts with yes."""
    rec = rec or record
    stt = stt or (lambda c, w: clean(transcribe(c, w), c))
    if rec(cfg, wav, deadline=time.time() + float(cfg["confirm_seconds"])) != "ok":
        return False
    answer = strip_wake(stt(effective(cfg, cwd), wav), cfg.get("wake_words") or ())[0]
    log(f"confirmation heard: {answer!r}")
    return bool(YES.match(answer))


# Wake word: a check on the TEXT, since Whisper already transcribes every utterance
# locally. No extra model. Real voices often come out as "Cloud" (synthetic test voices
# give "Claude"), but "cloud" is also a real word. Whisper punctuates a vocative ("Cloud,
# do you…", "Cloud.") and not a noun ("Cloud sync is broken"), so "cloud" only counts
# when punctuation or the end follows it directly.
WAKE = re.compile(r"^(?:(?:hey|hi|ok|okay)[\s,.!]+)?"
                  r"(?:(?:claude|claud|clod|clawed)\b|cloud(?=[,.!?:;]|\s*$))[\s,.!?:;-]*", re.I)


def strip_wake(text: str, extra=()):
    """("rest of sentence", True) if it starts with the wake word, else (text, False).
    extra = this Mac's learned spellings (cfg "wake_words"), punctuation-guarded."""
    m = WAKE.match(text)
    if not m and extra:
        alts = "|".join(re.escape(w) for w in extra if w)
        m = alts and re.match(rf"^(?:(?:hey|hi|ok|okay)[\s,.!]+)?(?:{alts})(?=[,.!?:;]|\s*$)[\s,.!?:;-]*",
                             text, re.I)
    return (text[m.end():].strip(), True) if m else (text, False)


NEVER_WAKE = {"yes", "no", "yeah", "yep", "okay", "ok", "so", "stop", "hey", "hi", "the", "a", "i",
              "you", "and", "what", "why", "how", "wait", "hello", "thanks", "thank"}


def wake_variants(heard: list) -> list:
    """`voice setup` wake training: the user said "Claude" N times; return the spellings
    Whisper produced that the wake check does NOT already accept, most frequent first.
    Only one-word answers count, and everyday words are never learned."""
    counts = {}
    for t in heard:
        words = re.sub(r"[^A-Za-z' ]", " ", t or "").split()
        if len(words) != 1 or strip_wake(words[0] + ".")[1]:
            continue
        w = words[0].lower()
        if len(w) >= 2 and w not in NEVER_WAKE:
            counts[w] = counts.get(w, 0) + 1
    return sorted(counts, key=lambda w: -counts[w])


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_panes(max_age: float = 12 * 3600) -> list:
    """Registry written by voice-speak.py's Stop hook; newest pane per project."""
    best = {}
    for f in (tmp_dir() / "claude-voice-panes").glob("*.json"):
        try:
            p = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if time.time() - p.get("ts", 0) > max_age:
            continue
        k = norm(p.get("project", ""))
        if k and (k not in best or p["ts"] > best[k]["ts"]):
            best[k] = p
    return sorted(best.values(), key=lambda p: -p["ts"])


def match_project(spoken: str, panes: list):
    """Loose match: 'voice loop' -> voiceLoop, 'acme CRM' -> acme-crm."""
    names = {norm(p["project"]): p for p in panes}
    hit = difflib.get_close_matches(norm(spoken), list(names), n=1, cutoff=0.75)
    return names[hit[0]] if hit else None


def cue(cfg: dict, event: str, block: bool = False) -> None:
    """accepted = sent, Claude is working; ready = mic open again; stopped = listening ended."""
    name = (cfg.get("sounds") or {}).get(event) or ""
    if not name:
        return
    path = name if "/" in name else f"/System/Library/Sounds/{name}.aiff"
    if not Path(path).exists():
        return
    vol = (cfg.get("sound_volumes") or {}).get(event, cfg.get("sound_volume", 0.5))
    cmd = ["afplay", "-v", str(vol), path]
    if block:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def say_local(text: str) -> None:
    """Blocking, so the mic stays closed while it talks. Same voice as the replies:
    with "engine": "kokoro", the Kokoro speaker (which falls back to say by itself)."""
    try:
        c = json.loads((voice_dir() / "config.json").read_text())
    except (OSError, ValueError):
        c = {}
    kokoro = Path(__file__).resolve().parent / "voice-kokoro.py"
    if c.get("engine") == "kokoro" and kokoro.exists():
        cmd = [str(kokoro), "--voice", str(c.get("kokoro_voice") or "bf_emma"),
               "--speed", str(float(c.get("kokoro_speed") or 1.0)),
               "--fallback-voice", str(c.get("voice") or "Samantha")]
    else:
        cmd = ["say", "-r", str(int(c.get("rate") or 200))]
        if c.get("voice"):
            cmd += ["-v", str(c["voice"])]
    subprocess.run(cmd, input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --- the gate: which utterances become messages ----------------------------------------
class Gate:
    """Pure decision logic, testable without a mic. feed() one cleaned utterance;
    it returns ("send", text) | ("hold", text) | ("ignore", text) | ("yes", "").
    Rules: in wake mode an utterance must start with the wake word, unless we're in
    the follow-up window or continuing held text; the wake check runs on each
    utterance BEFORE joining held text; only accepted speech is ever held; held text
    expires after `follow` seconds. (Bug 2026-11-25: unwoken chatter ending in "that"
    was held, then glued in FRONT of a real "Claude, ..." sentence, which then failed
    the wake check, and both were lost.)"""

    def __init__(self, wake: bool, follow: float, stop=lambda t: False, extra_wake=()):
        self.wake, self.follow, self.stop, self.extra = wake, follow, stop, tuple(extra_wake or ())
        self.pending, self.pending_at, self.awake_until = "", 0.0, 0.0

    def replied(self, now: float) -> None:     # Claude just finished speaking
        self.awake_until = now + self.follow

    def feed(self, text: str, now: float):
        if self.pending and now - self.pending_at > self.follow:
            self.pending = ""
        if self.wake:
            text, woke = strip_wake(text, self.extra)
            if woke:
                self.awake_until = now + self.follow
                if not text and not self.pending:
                    return "yes", ""
            elif not self.pending and now > self.awake_until and not self.stop(text):
                return "ignore", text
        text = defang(f"{self.pending} {text}".strip())   # the wake word may have hidden a "/"
        if is_incomplete(text):
            self.pending, self.pending_at = text.rstrip(" .,"), now
            return "hold", self.pending
        self.pending = ""
        return "send", text


# --- app mode: one utterance, for a session that listens for itself ----------------
def listener_running() -> bool:
    """True if the iTerm conversation loop is running (it already owns the mic)."""
    try:
        pid = int(PIDFILE().read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    ps = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return "voice-listen.py" in ps.stdout


def listen_once(cfg: dict, timeout: float, wake: bool = False, cwd: str = "",
                rec=None, stt=None) -> dict:
    """App mode (Claude desktop app, Code tab). There is no pane to type into, so the
    session runs this as a background command and gets woken with the result: record
    until ONE usable utterance or `timeout` seconds. Keeps listening (never returns)
    when our own speech cut the recording off ("spoke": the half-duplex guard), when
    nothing usable was heard, when the wake word is missing, or when the sentence
    sounds unfinished. Same Gate as the loop, so wake mode and the follow-up window
    behave identically. rec/stt are injectable for tests.
    Returns {"status": "heard"|"stop"|"timeout"|"busy"|"fail", "heard": text}.
    (First prototyped as a shell wrapper around an older --once; folded in here so the
    timeout ends cleanly and the log stays in $TMPDIR.)"""
    rec = rec or record
    stt = stt or (lambda c, w: clean(transcribe(c, w), c))
    if listener_running():
        return {"status": "busy", "heard": ""}
    end = time.time() + timeout
    ecfg = effective(cfg, cwd)
    gate = Gate(wake, float(cfg["follow_up_seconds"]), stop=lambda t: is_stop(t, cfg),
                extra_wake=cfg.get("wake_words") or ())
    try:   # a reply spoken just now opens the follow-up window, as in the loop
        spoke_at = LAST_SPOKEN().stat().st_mtime
        if time.time() - spoke_at < gate.follow:
            gate.replied(spoke_at)
    except OSError:
        pass
    log(f"app mode: listening for one utterance (max {timeout:.0f} s{', wake word' if wake else ''})")
    with tempfile.TemporaryDirectory(prefix="voiceloop-once-") as td:
        wav = os.path.join(td, "utt.wav")
        while time.time() < end:
            wait_quiet(cfg)
            r = rec(cfg, wav, deadline=end)
            if r == "timeout":
                break
            if r == "fail":
                log("app mode: recording failed (does this app have Microphone permission?)")
                return {"status": "fail", "heard": ""}
            if r == "spoke":
                log("app mode: our own reply started; recording discarded")
                wait_quiet(cfg)
                gate.replied(time.time())
                cue(cfg, "ready", block=True)
                continue
            text = stt(ecfg, wav)
            if not text:
                continue
            log(f"app mode heard: {text}")
            verdict, text = gate.feed(text, time.time())
            if verdict == "yes":
                cue(cfg, "listening", block=True)
                say_local("Yes?")
                continue
            if verdict in ("ignore", "hold"):
                log(f"app mode {'ignored (no wake word)' if verdict == 'ignore' else 'holding'}: {text}")
                continue
            if is_stop(text, cfg):
                cue(cfg, "stopped", block=True)
                return {"status": "stop", "heard": text}
            cue(cfg, "accepted")
            cmd = parse_command(text, cfg["commands"])
            if cmd:   # the app can't type a slash command; the session decides (see /voice-app)
                return {"status": "command", "heard": text, "command": cmd[0]}
            return {"status": "heard", "heard": text}
    if gate.pending:   # an unfinished sentence when time ran out: still better than losing it
        return {"status": "heard", "heard": gate.pending}
    return {"status": "timeout", "heard": ""}


# --- voice setup: an interactive first-run wizard (run in a terminal tab) ------------
def parse_setting(raw: str, current, kind: str):
    """Wizard input -> value. Empty = keep current. kind: seconds | minutes | yesno | percent."""
    raw = raw.strip().lower()
    if not raw:
        return current
    if kind == "yesno":
        if raw in ("y", "yes", "on", "true", "1"):
            return True
        if raw in ("n", "no", "off", "false", "0"):
            return False
        raise ValueError("answer y or n")
    if kind == "percent":
        v = float(raw.rstrip("%"))
        if not 0.05 <= v <= 20:
            raise ValueError("between 0.05 and 20 (%)")
        return f"{v:g}%"
    v = float(raw)
    lo, hi = {"seconds": (0.5, 120), "minutes": (1, 240)}[kind]
    if not lo <= v <= hi:
        raise ValueError(f"between {lo:g} and {hi:g}")
    return int(v) if v.is_integer() else v


def input_device() -> str:
    """Name of the default input device (system_profiler), or ""."""
    out = subprocess.run(["system_profiler", "SPAudioDataType"], capture_output=True, text=True,
                         timeout=20).stdout.splitlines()
    name = ""
    for line in out:
        if line.strip().endswith(":") and line.startswith(" " * 8) and not line.startswith(" " * 10):
            name = line.strip()[:-1]
        if "Default Input Device: Yes" in line:
            return name
    return ""


def save_listen(changes: dict) -> Path:
    """Merge `changes` into config.json's "listen" (backup first). Returns the backup path."""
    path = voice_dir() / "config.json"
    try:
        full = json.loads(path.read_text())
    except (OSError, ValueError):
        full = {}
    backup = path.with_name(f"config.json.bak-setup-{time.strftime('%Y%m%d-%H%M%S')}")
    if path.exists():
        backup.write_text(path.read_text())
    full.setdefault("listen", {}).update(changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(full, indent=2) + "\n")
    return backup


def run_setup(cfg: dict) -> int:
    """`voice setup`: mic check, room noise, wake-word training, a speak-and-hear test,
    the main settings, and a cheat sheet. Nothing is saved until the end, and only
    with a yes; config.json is backed up first."""
    if listener_running():
        print("The listener is running and owns the mic. Stop it first:  voice listen off")
        return 1

    def ask(q, default="y"):
        try:
            a = input(f"{q} [{default}] ").strip().lower() or default
        except EOFError:
            a = "n"          # no keyboard (piped/closed stdin): never change anything
        return a.startswith("y")

    def h(t):
        print(f"\n\033[1m{t}\033[0m")

    def pause(msg):
        try:
            input(msg)
        except EOFError:
            print()

    changes, wav = {}, os.path.join(tempfile.mkdtemp(prefix="voiceloop-setup-"), "s.wav")
    print("voiceLoop setup: about 3 minutes. Nothing is saved until the end.")

    h("1/6  Microphone")
    dev = input_device()
    print(f"  Input device: {dev or 'unknown'}   (change it in System Settings > Sound > Input)")
    vol = subprocess.run(["osascript", "-e", "input volume of (get volume settings)"],
                         capture_output=True, text=True).stdout.strip()
    if vol.isdigit():
        print(f"  Input volume: {vol}" + ("   <- this is 0: nothing can be heard!" if vol == "0" else ""))
    else:
        print("  Input volume: set on the device itself (USB headsets don't expose it to macOS)")
    pause("  Press Enter, then talk for 5 seconds to see the level meter…")
    p = subprocess.Popen(["rec", "-q", "-r", str(RATE), "-c", "1", "-b", "16", "-e", "signed-integer",
                          "-t", "raw", "-", "trim", "0", "5"], stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    peak, thr = 0.0, _threshold(cfg)
    while (chunk := p.stdout.read(FRAME * 2 * 3)):
        lvl = _rms(chunk)
        peak = max(peak, lvl)
        bar = int(min(lvl / 0.05, 1) * 40)
        print(f"\r  {'#' * bar:<40} {lvl * 100:5.2f}%  {'SOUND' if lvl > thr else '     '}", end="", flush=True)
    p.wait()
    print()
    if peak < 0.001:   # all zeros, or a muffled feed: what a missing permission looks like
        print(f"  The mic gave (almost) pure silence ({peak * 100:.3f}%). If you did talk: give this terminal")
        print("  app Microphone permission and relaunch it, check the mute switch, or pick another input.")
        if not ask("  Continue anyway?", "n"):
            return 1
    print(f"  Peak {peak * 100:.2f}%: " + ("good." if peak > 0.02 else "quiet; move closer or raise the input volume."))

    h("2/6  Room noise")
    pause("  Press Enter, then stay quiet for 2 seconds…")
    room = measure_room(2)
    if room.get("ok"):
        print(f"  Room: median {room['median']}%, peak {room['max']}%. Suggested threshold {room['threshold']}"
              f" (now {cfg['threshold']}).")
        if room["threshold"] != cfg["threshold"] and ask("  Use the suggested threshold?"):
            changes["threshold"] = room["threshold"]
            cfg["threshold"] = room["threshold"]
    else:
        print(f"  {room.get('why')}")

    h("3/6  Wake word")
    print('  Say "Claude" on its own after each sound, 5 times. This learns how Whisper hears you.')
    heard = []
    for i in range(5):
        cue(cfg, "listening", block=True)
        r = record(cfg, wav, deadline=time.time() + 6)
        t = clean(transcribe(cfg, wav), cfg) if r == "ok" else ""
        ok = bool(t) and strip_wake(t, cfg.get("wake_words") or ())[1]
        print(f"  {i + 1}: {repr(t) if t else '(nothing heard)':24} "
              f"{'ok' if ok else ('' if not t else 'NOT recognized')}")
        heard.append(t)
    new = [w for w in wake_variants(heard) if w not in (cfg.get("wake_words") or [])]
    if new:
        print(f"  Whisper also wrote: {', '.join(new)}. Accepting these as the wake word on this Mac is")
        print("  safe-ish: they only count at the start and followed by punctuation (\"Pod, …\" / \"Pod.\").")
        if ask(f"  Accept {', '.join(new)}?"):
            changes["wake_words"] = list(cfg.get("wake_words") or []) + new
            cfg["wake_words"] = changes["wake_words"]
    elif not any(heard):
        print("  Nothing was heard. Check the mic (step 1) and run voice setup again.")
    else:
        print("  Nothing new to learn. Tip: \"Hey Claude\" is recognized most reliably.")

    h("4/6  Speak and hear")
    print("  Say a sentence after the sound.")
    cue(cfg, "listening", block=True)
    r = record(cfg, wav, deadline=time.time() + 15)
    t = clean(transcribe(effective(cfg, os.getcwd()), wav), cfg) if r == "ok" else ""
    print(f"  Heard: {t or '(nothing)'}")
    say_local(f"I heard: {t}" if t else "I didn't catch that.")

    h("5/6  Settings (Enter keeps the current value)")
    for key, kind, text in [
            ("silence_seconds", "seconds", "Quiet time that ends your message and sends it"),
            ("follow_up_seconds", "seconds", "After a reply, answer without saying the name for"),
            ("idle_minutes", "minutes", "Turn the mic off after this long with nothing sent"),
            ("wake", "yesno", "Always require the wake word (also for `voice listen`)")]:
        while True:
            cur = cfg[key]
            shown = ("y" if cur else "n") if kind == "yesno" else cur
            try:
                raw = input(f"  {text}: [{shown}] ")
            except EOFError:
                raw = ""
            try:
                v = parse_setting(raw, cur, kind)
                break
            except ValueError as e:
                print(f"    {e}")
        if v != cur:
            changes[key] = v

    h("6/6  Cheat sheet")
    print("  Start:    iTerm: voice session, then voice listen wake  ·  Claude app: /voice-app\n"
          "  Talk:     \"Hey Claude, …\"  ·  no name needed for a while after a reply\n"
          "  Local:    switch to <project> · which projects · voice help · stop listening\n"
          "  Commands: \"command <name>\" for: " + ", ".join(cfg["commands"]) + "\n"
          "  Terminal: voice stop · voice off all · voice listen status · voice status")

    if not changes:
        print("\nNo changes to save.")
        return 0
    print("\nChanges: " + ", ".join(f"{k} = {v}" for k, v in changes.items()))
    if ask("Save them to ~/.claude/voice/config.json?"):
        b = save_listen(changes)
        print(f"Saved (backup: {b.name}). A running listener picks them up when restarted.")
    else:
        print("Not saved.")
    return 0


# --- main loop -------------------------------------------------------------------
def loop(iterm_sid: str, claude_sid: str, cfg: dict, wake: bool = False) -> None:
    if claude_sid:  # make sure replies are spoken, or half-duplex would wait forever
        (tmp_dir() / f"claude-voice.{claude_sid}").touch()
        TARGET().write_text(claude_sid)  # tells voice-speak's UserPromptSubmit hook who we talk to
    log(f"conversation mode ON (pane {iterm_sid.split(':')[-1][:8]}"
        f"{', wake word: Claude' if wake else ''}); say 'stop listening' to end")
    cue(cfg, "listening", block=True)
    with tempfile.TemporaryDirectory(prefix="voiceloop-") as td:
        wav = os.path.join(td, "utt.wav")
        cwd = next((p["cwd"] for p in load_panes() if p.get("claude_session") == claude_sid),
                   os.getcwd())   # the target project's folder (for its vocabulary)
        follow = float(cfg["follow_up_seconds"])
        gate = Gate(wake, follow, stop=lambda t: is_stop(t, cfg), extra_wake=cfg.get("wake_words") or ())
        idle_s = float(cfg["idle_minutes"]) * 60
        last_active = time.time()   # sends and local commands count; ignored chatter doesn't

        def switch_to(target, via: str) -> None:
            """Retarget mic + voice to another pane. Used by voice AND the control file."""
            nonlocal iterm_sid, claude_sid, cwd, last_active
            last_active = time.time()
            if not claude_in_pane(target["iterm_session"]):
                log(f"{target['project']}: Claude isn't running there ({via})")
                say_local(f"{target['project']} isn't running Claude any more.")
                return
            cue(cfg, "accepted", block=True)
            if claude_sid:  # voice follows the mic: old pane goes quiet
                (tmp_dir() / f"claude-voice.{claude_sid}").unlink(missing_ok=True)
            iterm_sid, claude_sid = target["iterm_session"], target["claude_session"]
            cwd = target.get("cwd", cwd)   # vocabulary follows the target project
            (tmp_dir() / f"claude-voice.{claude_sid}").touch()
            TARGET().write_text(claude_sid)
            log(f"switched to {target['project']} (pane {iterm_sid.split(':')[-1][:8]}, {via})")
            say_local(f"Switched to {target['project']}.")

        while True:
            ctl = take_control()
            if ctl and ctl[0] == "stop":
                log("stop requested via the control file; conversation mode OFF")
                cue(cfg, "stopped", block=True)
                say_local("Stopped listening.")
                return
            if ctl and ctl[0] == "switch":
                target = match_project(ctl[1], load_panes())
                if target:
                    switch_to(target, "on request")
                else:
                    log(f"asked to switch to unknown project {ctl[1]!r}")
                continue
            wait_quiet(cfg)
            log("listening…")
            r = record(cfg, wav, deadline=min(last_active + idle_s,
                                              time.time() + float(cfg["pane_check_seconds"])))
            if r == "timeout":
                if time.time() >= last_active + idle_s:
                    log("idle timeout; conversation mode OFF")
                    cue(cfg, "stopped", block=True)
                    say_local(f"Nothing for {cfg['idle_minutes']} minutes, so I stopped listening.")
                    return
                if not claude_in_pane(iterm_sid):
                    log("Claude is no longer running in the target pane; conversation mode OFF")
                    cue(cfg, "stopped", block=True)
                    say_local("Claude isn't running in that pane any more, so I stopped listening.")
                    return
                continue
            if r == "spoke":
                if CONTROL().exists():
                    continue          # a control request interrupted listening; handled at the loop top
                log("(Claude started speaking; recording discarded)")
                wait_quiet(cfg)
                gate.replied(time.time())   # ANY spoken reply opens the window
                cue(cfg, "ready", block=True)
                continue
            if r == "fail":
                log("recording failed (mic permission?); stopping")
                return
            t0 = time.time()
            ecfg = effective(cfg, cwd)
            text = clean(transcribe(ecfg, wav), ecfg)
            if not text:
                log(f"(nothing usable heard, {time.time() - t0:.2f}s)")
                continue
            log(f"heard ({time.time() - t0:.2f}s): {text}")
            verdict, text = gate.feed(text, time.time())
            if verdict == "yes":
                cue(cfg, "listening", block=True)   # instant; "Yes?" in Kokoro takes ~1 s
                say_local("Yes?")
                continue
            if verdict == "ignore":
                log(f"ignored (no wake word): {text}")
                continue
            if verdict == "hold":
                log(f"holding (sounds unfinished): {text}")
                continue
            if is_stop(text, cfg):
                log("stop phrase heard; conversation mode OFF")
                cue(cfg, "stopped", block=True)
                say_local("Stopped listening.")
                return
            outgoing, expect_reply = text, True
            cmd = parse_command(text, cfg["commands"])
            if cmd:
                name, opts = cmd
                if opts.get("confirm"):
                    cue(cfg, "listening", block=True)
                    say_local(f"{opts.get('ask') or 'Run ' + name.replace('-', ' ') + '?'} Say yes.")
                    if not confirm_by_voice(cfg, wav, cwd):
                        log(f"command /{name} NOT confirmed; nothing sent")
                        say_local("Cancelled.")
                        continue
                outgoing, expect_reply = "/" + name, bool(opts.get("reply", True))
                log(f"command: /{name}")
            m = None if cmd else SWITCH.match(text)
            target = match_project(m.group(1), load_panes()) if m else None
            if target:
                switch_to(target, "by voice")
                continue
            if not cmd and (LIST.match(text) or HELP.match(text)):
                last_active = time.time()
                cue(cfg, "accepted", block=True)
            if not cmd and LIST.match(text):
                names = [p["project"] for p in load_panes()]
                log(f"projects: {names}")
                say_local("Open projects: " + ", ".join(names) + "." if names
                          else "I don't know any projects yet.")
                continue
            if not cmd and HELP.match(text):
                say_local(HELP_TEXT)
                continue
            # (a "switch to …" that matched no project falls through to Claude)
            if not claude_in_pane(iterm_sid):   # never type speech into a bare shell
                log("Claude is no longer running in the target pane; not sent; conversation mode OFF")
                cue(cfg, "stopped", block=True)
                say_local("Claude isn't running in that pane any more, so I didn't send that, "
                          "and I stopped listening.")
                return
            last_active = time.time()
            cue(cfg, "accepted")
            sent_at = time.time()
            if not send(iterm_sid, outgoing):
                say_local("I can't reach that pane any more. Say switch to, and a project name.")
                continue
            if not expect_reply:          # e.g. /clear: Claude doesn't answer, so don't wait
                log("sent; no spoken reply expected")
                time.sleep(1.0)
                cue(cfg, "ready", block=True)
                continue
            log("sent; mic closed until the reply has been spoken")
            wait_for_reply(cfg, sent_at, claude_sid)
            gate.replied(time.time())   # a reply invites an answer
            cue(cfg, "ready", block=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="voiceLoop conversation mode")
    ap.add_argument("--iterm-session", default=os.environ.get("ITERM_SESSION_ID", ""))
    ap.add_argument("--claude-session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    ap.add_argument("--once", action="store_true",
                    help="app mode: wait for ONE usable utterance, print it, exit")
    ap.add_argument("--timeout", type=float, default=120, help="--once: give up after N seconds")
    ap.add_argument("--json", action="store_true", help="--once: print a JSON result")
    ap.add_argument("--wake", action="store_true", help="only accept sentences starting with 'Claude'")
    ap.add_argument("--measure-room", type=float, metavar="SECS", nargs="?", const=2.0,
                    help="record SECS (2) of room noise, print a suggested threshold as JSON")
    ap.add_argument("--setup", action="store_true", help="interactive setup wizard (voice setup)")
    a = ap.parse_args()
    if a.setup:
        return run_setup(load_config())
    if a.measure_room is not None:
        print(json.dumps(measure_room(a.measure_room)))
        return 0
    cfg = load_config()
    # A plain SIGTERM would kill Python without running `finally`/`with` cleanup: rec
    # would linger and a recording could stay in the temp dir. Make it a normal exit.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if a.once:
        res = listen_once(cfg, a.timeout, wake=a.wake or bool(cfg["wake"]), cwd=os.getcwd())
        print(json.dumps(res) if a.json else f"{res['status']}: {res['heard']}")
        return 1 if res["status"] in ("busy", "fail") else 0
    if not a.iterm_session:
        print("need --iterm-session (or run inside iTerm)", file=sys.stderr)
        return 1
    PIDFILE().write_text(str(os.getpid()))
    try:
        loop(a.iterm_session, re.sub(r"[^A-Za-z0-9-]", "", a.claude_session), cfg,
             wake=a.wake or bool(cfg["wake"]))
    except KeyboardInterrupt:
        log("interrupted; conversation mode OFF")
    finally:
        for f in (PIDFILE(), TARGET(), CONTROL()):
            try:
                f.unlink()
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
