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
#            voice listen once [--wake] [--json] [--timeout N]   (default N: idle_minutes)
#            = voice-listen.py --once …  → prints {"status": "heard"|"stop"|"timeout"|
#              "busy"|"fail", "heard": "<text>"}. See /voice-app and docs/HOW-IT-WORKS.md.
#          Word list: voice words [add X | fix heard = Right | remove X] (~/.claude/voice/words.txt)
#          Setup wizard: voice setup (= voice-listen.py --setup): mic (pick which input),
#            room, wake-word training, speak-and-hear test, settings, cheat sheet.
#          Mic: "device" in config.json's "listen" = an input's exact name ("" = the macOS
#            default). Unplugged -> the default is used until it's back (logged). A new mic
#            needs a new threshold: run the setup's room-noise step again.
#          Benchmark (no mic): voice benchmark [--models base,small] [--noise] [--quick] [--show]
#            voice benchmark --record: read the sentences once (terminal); the clips go to
#            ~/.local/share/voiceloop/bench/ and every later run scores them as speaker "you".
#            = voice-listen.py --benchmark …: `say` speaks known sentences (built-ins + your
#            word list) to WAV (plus two Kokoro voices, kokoro:af_heart / kokoro:bm_george, when
#            Kokoro is installed), every model transcribes them with the word list off and on,
#            prints accuracy, wake word caught, seconds per sentence, words it got wrong.
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
#          [[Whisper invents words from silence]], [[Hands-free speech-to-text with whisper.cpp]],
#          [[Benchmarking speech-to-text without a microphone]].

from __future__ import annotations

import argparse
import array
import collections
import difflib
import hashlib
import json
import math
import os
import re
import shutil
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
    # small.en (default since 2026-11-25: 2 vs 6 words wrong of 44 on real speech, ~0.5 s);
    # falls back to base.en on installs that only have that one
    "model": str(MODELS / ("ggml-small.en.bin" if (MODELS / "ggml-small.en.bin").exists()
                           else "ggml-base.en.bin")),
    "vad_model": str(MODELS / "ggml-silero-v5.1.2.bin"),
    # Vocabulary hint for Whisper + spelling fixes. Put YOUR words in config.json ("listen":
    # {"prompt": "...", "replacements": {...}}) or per project in <project>/.claude/voice.json.
    "prompt": "Claude, Claude Code, iTerm, GitHub, Python, JavaScript, TypeScript, JSON, API, macOS",
    "replacements": {"claude code": "Claude Code", "i term": "iTerm", "git hub": "GitHub",
                     "mac os": "macOS", "type script": "TypeScript", "java script": "JavaScript"},
    "stop_phrases": ["stop listening"],
    "reply_timeout": 600,        # max seconds to wait for Claude to answer + speak
    "cooldown": 0.6,             # pause after speech ends, so room echo dies down
    "device": "",                # mic: "" = the macOS default input; or an input's exact name
                                 # ("RIG WX1"), so a webcam or AirPods becoming the default
                                 # can't take over. Unplugged -> the default is used meanwhile.
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
    "quiet_threshold": "",       # `voice quiet on`: the threshold for a soft voice
                                 # ("" = 0.6 x threshold). Applies from the next recording.
    # Spoken snippets: "snippet <name>" in a sentence becomes the stored text.
    # {"standup": "Summarise what changed since yesterday, …"}; per project in voice.json too.
    "snippets": {},
    "log_hours": 24,             # what you say is logged in plain text: after this many hours
                                 # the log is set aside, then deleted (kept 2 x at most). 0 = keep
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


def spoke_file(claude_sid: str = "") -> Path:
    """Touched by voice-speak.py when THIS session's reply starts speaking. Without a session
    id, fall back to the shared last-spoken file (any session's reply counts)."""
    return tmp_dir() / f"claude-voice.spoke.{claude_sid}" if claude_sid else LAST_SPOKEN()


# --- the Talk page (voice-talk.py) as a second input ---------------------------------
TALK_LIVE = lambda: tmp_dir() / "claude-talk.live"   # noqa: E731  touched by voice-talk.py while a page streams


def phone_live(max_age: float = 3.0) -> bool:
    """True while a Talk page is streaming audio. Then the phone IS the microphone and the
    desk mic rests: with both open, one sentence arrived twice (a headset reaches the next
    room). The marker ages out by itself, so a crashed server can't mute the desk."""
    try:
        return time.time() - TALK_LIVE().stat().st_mtime < max_age
    except OSError:
        return False


def once_file(claude_sid: str) -> Path:
    """Written by `listen once` while it waits: tells voice-talk.py an app session is listening."""
    return tmp_dir() / f"claude-listen.once.{claude_sid}"


def inbox_file(claude_sid: str) -> Path:
    return tmp_dir() / f"claude-talk.inbox.{claude_sid}"


def turn_file(claude_sid: str) -> Path:
    """Written by voice-talk.py when it delivers: "this turn came from the phone". The Stop
    hook (voice-speak.py) then answers into reply_file() instead of speaking on the Mac."""
    return tmp_dir() / f"claude-talk.turn.{claude_sid}"


def reply_file(claude_sid: str) -> Path:
    return tmp_dir() / f"claude-talk.reply.{claude_sid}"


def put_inbox(claude_sid: str, result: dict) -> None:
    """voice-talk.py hands an utterance to the session's waiting `listen once`. Written
    whole, then renamed, so the reader never sees half a file."""
    tmp = inbox_file(claude_sid).with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(result))
    os.replace(tmp, inbox_file(claude_sid))


def take_inbox(claude_sid: str):
    """One waiting phone utterance as a `listen once` result, or None. Read-and-delete.
    The Talk server already ran it through clean() and the Gate; defang() again anyway,
    because this text becomes a message to Claude."""
    f = inbox_file(claude_sid)
    try:
        raw = f.read_text()
        f.unlink()
        d = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("status") not in ("heard", "command"):
        return None
    heard = defang(re.sub(r"[\x00-\x1f\x7f]", " ", str(d.get("heard") or "")))[:4000].strip()
    if not heard:
        return None
    out = {"status": d["status"], "heard": heard, "via": "phone"}
    if d["status"] == "command":
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", str(d.get("command") or "")):
            return None
        out["command"] = d["command"]
    return out


def waiting_sessions() -> list:
    """App sessions with a `listen once` running right now, newest first:
    [{"claude_session", "cwd", "wake", "ts"}]. Markers of dead processes are removed."""
    out = []
    for f in tmp_dir().glob("claude-listen.once.*"):
        try:
            d = json.loads(f.read_text())
            os.kill(int(d["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            try:
                f.unlink()
            except OSError:
                pass
            continue
        out.append({"claude_session": f.name.split(".once.", 1)[1], "cwd": str(d.get("cwd") or ""),
                    "wake": bool(d.get("wake")), "ts": float(d.get("ts") or 0)})
    return sorted(out, key=lambda w: -w["ts"])


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


def rotate_log(hours: float, now: float = 0.0) -> None:
    """The log holds what you said, in plain text. Once the file is older than `hours` it
    becomes claude-listen.log.old (replacing the previous one), and a .old that was last
    written more than `hours` ago is deleted: text is kept for 2 x hours at most. The newest
    lines stay readable for troubleshooting meanwhile. 0 (or less) = keep everything."""
    if hours <= 0:
        return
    now, path = now or time.time(), LOG()
    old = path.with_name(path.name + ".old")
    try:
        if now - old.stat().st_mtime > hours * 3600:
            old.unlink()
    except OSError:
        pass
    try:
        st = path.stat()
        if now - getattr(st, "st_birthtime", st.st_ctime) > hours * 3600:
            os.replace(path, old)
    except OSError:
        pass


_LOG_CHECKED = 0.0


def log(msg: str) -> None:
    global _LOG_CHECKED
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, file=sys.stderr, flush=True)   # stderr: stdout is --once's JSON result
    if time.time() - _LOG_CHECKED > 3600:      # at start, then hourly: cheap, and never per line
        _LOG_CHECKED = time.time()
        try:
            rotate_log(float(load_config().get("log_hours") or 0))
        except (TypeError, ValueError):
            pass
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


QUIET = lambda: voice_dir() / "quiet"   # noqa: E731  flag file: `voice quiet on` / `off`
QUIET_FACTOR = 0.6   # quiet threshold when none is set. suggest_threshold() leaves the normal
                     # one at >= 2.5 x the room's median noise, so 0.6 x is still above the room.


def _level(t) -> float:
    t = str(t).strip()
    return float(t[:-1]) / 100 if t.endswith("%") else float(t)


def quiet_on() -> bool:
    try:
        return QUIET().exists()
    except OSError:
        return False


def _threshold(cfg: dict) -> float:
    """The loudness that counts as sound. With `voice quiet on` (a soft voice, e.g. while
    someone sleeps) the lower quiet threshold applies. capture() asks on every recording,
    so the switch works on a running listener."""
    base = _level(cfg["threshold"])
    if quiet_on():
        try:
            q = str(cfg.get("quiet_threshold") or "").strip()
            return _level(q) if q else base * QUIET_FACTOR
        except ValueError:
            return base * QUIET_FACTOR
    return base


def suggest_threshold(levels: list) -> dict:
    """Room noise (RMS per 30 ms frame, 0..1) -> a threshold safely above it.
    Rule: max(0.5 %, 2.5 x median, 2 x the 95th percentile), rounded UP to 0.1 %.
    A quiet laptop mic (median 0.43 %, max 0.55 %) gets 1.1 %; under the 0.5 %
    default its recordings never ended, and that room's peak rose to 0.70 % within
    10 minutes, so the margin is generous (speech is typically 2-10 %). All zeros = no signal (permission?)."""
    if not levels or max(levels) == 0:
        return {"ok": False, "why": "no signal: the mic gave digital silence (Microphone permission?)"}
    v = sorted(levels)
    med, p95 = v[len(v) // 2], v[min(len(v) - 1, int(len(v) * 0.95))]
    pct = max(0.5, 2.5 * med * 100, 2 * p95 * 100)
    pct = math.ceil(round(pct * 10, 6)) / 10
    return {"ok": True, "median": round(med * 100, 3), "p95": round(p95 * 100, 3),
            "max": round(v[-1] * 100, 3), "threshold": f"{pct:g}%"}


def rec_cmd(device: str = "", *tail: str) -> list:
    """The sox command that streams the mic as raw 16 kHz 16-bit mono PCM to stdout.
    device "" = the macOS default input (`rec`). A name records from that CoreAudio input
    instead; it must match the name exactly (sox fails fast with exit 2 otherwise)."""
    head = ["sox", "-q", "-t", "coreaudio", device] if device else ["rec", "-q"]
    return head + ["-r", str(RATE), "-c", "1", "-b", "16", "-e", "signed-integer",
                   "-t", "raw", "-", *tail]


def device_missing(device: str, status: str, returncode) -> bool:
    """True when a chosen mic couldn't be opened (unplugged, renamed): sox exited by itself
    with an error before any sound. Our own terminate() gives -15, a normal end 0."""
    return bool(device) and status == "fail" and returncode not in (0, None, -15)


MIC_WARN_EVERY = 600   # seconds between spoken "your mic isn't connected" notices


def should_warn(last: float, now: float, every: float = MIC_WARN_EVERY) -> bool:
    return now - last >= every


def warn_missing_mic(device: str) -> None:
    """Say ONCE in a while that the chosen mic is gone. The fallback mic still uses the
    chosen mic's threshold, which may not fit it (a noisier mic can record until
    max_seconds), so the user should know rather than wonder. The marker file keeps app
    mode (a new process per utterance) from repeating it every time."""
    marker = tmp_dir() / "claude-listen.micwarn"
    try:
        last = marker.stat().st_mtime
    except OSError:
        last = 0.0
    if not should_warn(last, time.time()):
        return
    try:
        marker.touch()
    except OSError:
        pass
    try:
        say_local(f"Your microphone {device} isn't connected, so I'm using the default one.")
    except Exception:
        pass   # a notice must never break listening


def measure_room(secs: float = 2.0, device: str = "") -> dict:
    """Record `secs` of room sound (stay quiet) and suggest a threshold."""
    p = subprocess.run(rec_cmd(device, "trim", "0", str(secs)),
                       stdin=subprocess.DEVNULL, capture_output=True, timeout=secs + 15)
    if device and p.returncode and not p.stdout:
        log(f"mic {device!r} not available; measuring the default input")
        return measure_room(secs)
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


def record(cfg: dict, wav: str, deadline: float = 0.0, also=lambda: False) -> str:
    """Mic -> capture() -> wav. Returns "ok", "fail", or "spoke": TTS started while
    the mic was open (e.g. the reply to something typed), so the recording is
    discarded. Otherwise we'd transcribe Samantha and send her words back to Claude.
    `also`: one more reason to drop the recording (the Talk page took over as the mic)."""
    device = str(cfg.get("device") or "")
    p = subprocess.Popen(rec_cmd(device), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    try:
        status, pcm = capture(p.stdout, cfg, interrupted=lambda: speaking() or CONTROL().exists() or also(),
                              deadline=deadline)
    finally:
        if p.poll() is None:
            p.terminate()
        p.wait()
    if device_missing(device, status, p.returncode):
        # The chosen mic is gone (unplugged): keep listening on the default input rather
        # than stop. Retried every utterance, so plugging it back in switches back.
        log(f"mic {device!r} not available; using the default input")
        warn_missing_mic(device)
        return record({**cfg, "device": ""}, wav, deadline, also)
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
    """<project>/.claude/voice.json: {"vocabulary": [...], "replacements": {...},
    "snippets": {...}, "scanned": [...]}. "scanned" is what `voice words scan` found in
    the project; it comes after your own "vocabulary". Read fresh on every utterance, so
    edits apply to the next sentence."""
    out = {"vocabulary": [], "replacements": {}, "snippets": {}}
    if not cwd:
        return out
    try:
        d = json.loads((Path(cwd) / ".claude" / "voice.json").read_text())
    except (OSError, ValueError):
        return out
    if isinstance(d, dict):
        for key in ("vocabulary", "scanned"):
            v = d.get(key)
            if isinstance(v, list):
                out["vocabulary"] += [t.strip()[:60] for t in v if isinstance(t, str) and t.strip()]
        for key in ("replacements", "snippets"):
            r = d.get(key)
            if isinstance(r, dict):
                out[key] = {k: v for k, v in r.items() if isinstance(k, str) and isinstance(v, str)}
    return out


WORDS = lambda: voice_dir() / "words.txt"   # noqa: E731  your own word list (`voice words`)


def parse_words(text: str) -> dict:
    """words.txt: one word or phrase per line; "heard = Right" also fixes a spelling;
    "#" starts a comment. The right-hand side goes into the vocabulary hint too."""
    out = {"vocabulary": [], "replacements": {}}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line:
            wrong, right = (x.strip() for x in line.split("=", 1))
            if wrong and right:
                out["replacements"][wrong.lower()] = right[:60]
                out["vocabulary"].append(right[:60])
        else:
            out["vocabulary"].append(line[:60])
    return out


def load_words() -> dict:
    try:
        return parse_words(WORDS().read_text())
    except OSError:
        return {"vocabulary": [], "replacements": {}}


def effective(cfg: dict, cwd: str) -> dict:
    """Global settings + your word list + the target project's vocabulary. Order in the
    prompt (it has a size cap): project terms, then words.txt, then the config "prompt".
    Replacements: config < words.txt < project (the most specific wins)."""
    pv, wl = project_vocab(cwd), load_words()
    e = dict(cfg)
    terms, seen = [], set()
    for t in pv["vocabulary"] + wl["vocabulary"] + [x.strip() for x in str(cfg["prompt"]).split(",")]:
        if t and t.lower() not in seen and len(", ".join(terms + [t])) <= PROMPT_MAX:
            terms.append(t)
            seen.add(t.lower())
    e["prompt"] = ", ".join(terms)
    e["replacements"] = {**(cfg.get("replacements") or {}), **wl["replacements"], **pv["replacements"]}
    own = cfg.get("snippets")
    e["snippets"] = {**(own if isinstance(own, dict) else {}), **pv["snippets"]}
    return e


SNIPPET_MAX = 2000   # chars of one snippet's text


def expand_snippets(text: str, snippets) -> str:
    """"snippet <name>" anywhere in a sentence becomes the stored text. The word "snippet"
    is required, so a name that is an ordinary word can't fire by accident. Whisper's
    comma or colon after "snippet" and a hyphen inside the name are accepted; longer names
    are tried first ("standup long" before "standup"). The text is flattened to one line:
    a newline typed into the pane would send the message early."""
    if not isinstance(snippets, dict):
        return text
    for name in sorted((n for n in snippets if isinstance(n, str)), key=len, reverse=True):
        words, body = re.findall(r"[A-Za-z0-9]+", name), snippets[name]
        if not words or not isinstance(body, str):
            continue
        flat = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", body)).strip()[:SNIPPET_MAX]
        if flat:
            pat = r"\bsnippet[\s,:-]+" + r"[\s-]+".join(re.escape(w) for w in words) + r"\b"
            text = re.sub(pat, lambda m: flat, text, flags=re.I)
    return text


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
    text = expand_snippets(text, cfg.get("snippets"))            # BEFORE defang: a snippet can't start a command either
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


HOLD_ON = re.compile(r"[\s,.;:!-]*\b(?:hang on|hold on|one moment|one second|give me a second|let me think)[\s.!,]*$", re.I)


def held_back(text: str):
    """"…, hang on." = I'm not done, keep listening. Returns the sentence so far without
    that phrase (may be ""), or None when the utterance doesn't end with one."""
    m = HOLD_ON.search(text)
    return text[:m.start()].rstrip(" .,") if m else None


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
            if mode is "esc" then
              tell s to write text (ASCII character 27) newline NO
              return "ok"
            end if
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


def press_escape(iterm_sid: str) -> bool:
    """The Escape key into the pane: Claude Code stops what it is doing (the same as
    pressing Esc at the desk). One fixed key, never text. The caller checks first that
    Claude still runs there."""
    r = subprocess.run(["osascript", "-e", SEND_SCRIPT, iterm_sid.split(":")[-1], "esc"],
                       capture_output=True, text=True, timeout=15)
    if r.stdout.strip() != "ok":
        log(f"escape failed: {r.stdout.strip() or r.stderr.strip()}")
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
    voice-speak.py touches spoke_file(<session>) when it starts speaking that session's
    reply; then we wait for the speech process to exit. Per session since 2026-11-25:
    with one shared file, ANOTHER voiced session's reply ended this wait early."""
    deadline = time.time() + float(cfg["reply_timeout"])
    while time.time() < deadline:
        if not speech_on(claude_sid):
            return   # speaking was turned off (e.g. by `voice off all`): no reply will be spoken
        if CONTROL().exists():
            log("control request pending; stopped waiting for the reply")
            return   # a switch/stop request (CLI or dashboard) beats waiting for an old reply
        try:
            if spoke_file(claude_sid).stat().st_mtime > sent_at:
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
    "hold" = the sentence isn't finished (it ends on "and…", or with "hang on"): it is
    kept and the next utterance is joined to it.
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
        kept = held_back(text)
        if kept is not None and not self.stop(text):
            self.pending, self.pending_at = kept, now
            self.awake_until = max(self.awake_until, now + self.follow)   # the rest needs no wake word
            return "hold", kept
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
                rec=None, stt=None, claude_sid: str = "") -> dict:
    """App mode (Claude desktop app, Code tab). There is no pane to type into, so the
    session runs this as a background command and gets woken with the result: record
    until ONE usable utterance or `timeout` seconds. Keeps listening (never returns)
    when our own speech cut the recording off ("spoke": the half-duplex guard), when
    nothing usable was heard, when the wake word is missing, or when the sentence
    sounds unfinished. Same Gate as the loop, so wake mode and the follow-up window
    behave identically. rec/stt are injectable for tests.
    Returns {"status": "heard"|"stop"|"timeout"|"busy"|"fail", "heard": text}.
    (First prototyped as a shell wrapper around an older --once; folded in here so the
    timeout ends cleanly and the log stays in $TMPDIR.)
    The Talk page (voice-talk.py) is a second source: while this waits, a marker file says
    so; an utterance from the phone arrives in the session's inbox and is returned like a
    heard one (plus "via": "phone"). While a page is streaming, the mic here stays closed."""
    if listener_running():
        return {"status": "busy", "heard": ""}
    if not claude_sid:
        return _listen_once(cfg, timeout, wake, cwd, rec, stt, claude_sid)
    once_file(claude_sid).write_text(json.dumps(
        {"pid": os.getpid(), "cwd": cwd, "wake": bool(wake), "ts": time.time()}))
    try:
        return _listen_once(cfg, timeout, wake, cwd, rec, stt, claude_sid)
    finally:
        once_file(claude_sid).unlink(missing_ok=True)


def _listen_once(cfg: dict, timeout: float, wake: bool, cwd: str, rec, stt, claude_sid: str) -> dict:
    from_phone = lambda: bool(claude_sid) and inbox_file(claude_sid).exists()   # noqa: E731
    rec = rec or (lambda c, w, deadline=0.0: record(c, w, deadline, also=lambda: phone_live() or from_phone()))
    stt = stt or (lambda c, w: clean(transcribe(c, w), c))
    end = time.time() + timeout
    ecfg = effective(cfg, cwd)
    gate = Gate(wake, float(cfg["follow_up_seconds"]), stop=lambda t: is_stop(t, cfg),
                extra_wake=cfg.get("wake_words") or ())
    try:   # OUR reply spoken just now opens the follow-up window, as in the loop
        spoke_at = spoke_file(claude_sid).stat().st_mtime
        if time.time() - spoke_at < gate.follow:
            gate.replied(spoke_at)
    except OSError:
        pass
    log(f"app mode: listening for one utterance (max {timeout:.0f} s{', wake word' if wake else ''}"
        f"{', mic ' + repr(cfg['device']) if cfg.get('device') else ''})")
    with tempfile.TemporaryDirectory(prefix="voiceloop-once-") as td:
        wav = os.path.join(td, "utt.wav")
        while time.time() < end:
            got = take_inbox(claude_sid) if claude_sid else None
            if got:
                log(f"app mode: from the phone: {got['heard']}")
                return got
            if phone_live():       # a Talk page is the microphone now: wait for its inbox only
                time.sleep(0.25)
                continue
            wait_quiet(cfg)
            r = rec(cfg, wav, deadline=end)
            if r == "timeout":
                break
            if r == "fail":
                log("app mode: recording failed (does this app have Microphone permission?)")
                return {"status": "fail", "heard": ""}
            if r == "spoke" and (phone_live() or from_phone()):
                continue           # not our reply: the phone took over; handled at the loop top
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


def parse_input_devices(text: str) -> tuple:
    """system_profiler SPAudioDataType text -> ([input names, in order, no duplicates],
    default input name or ""). A device is an input when it lists "Input Channels"; its
    name is the 8-space-indented "Name:" line above. A headset can appear twice (input
    and output halves), so names are de-duplicated."""
    names, default, name = [], "", ""
    for line in text.splitlines():
        if line.strip().endswith(":") and line.startswith(" " * 8) and not line.startswith(" " * 10):
            name = line.strip()[:-1]
        elif name and ("Input Channels:" in line or "Default Input Device: Yes" in line):
            if name not in names:
                names.append(name)
            if "Default Input Device: Yes" in line:
                default = name
    return names, default


def input_devices() -> tuple:
    """([input device names], default input name), from system_profiler (~0.5 s)."""
    try:
        out = subprocess.run(["system_profiler", "SPAudioDataType"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    return parse_input_devices(out)


def input_device() -> str:
    """Name of the default input device, or ""."""
    return input_devices()[1]


def pick_device(answer: str, names: list, current: str):
    """The setup wizard's mic choice. Enter keeps `current`; a number picks from `names`
    (1-based); 0 = follow the macOS default (""). Anything else -> None (ask again)."""
    a = answer.strip()
    if not a:
        return current
    if a.isdigit():
        i = int(a)
        if i == 0:
            return ""
        if 1 <= i <= len(names):
            return names[i - 1]
    return None


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
    names, default = input_devices()
    current = str(cfg.get("device") or "")
    if names:
        print("  Inputs on this Mac:")
        print(f"    0  follow the macOS default (now: {default or 'unknown'})"
              + ("   <- current" if not current else ""))
        for i, n in enumerate(names, 1):
            print(f"    {i}  {n}" + ("   (macOS default)" if n == default else "")
                  + ("   <- current" if n == current else ""))
        if current and current not in names:
            print(f"  Your chosen mic {current!r} isn't connected; the default is used until it is.")
        while True:
            try:
                ans = input("  Which mic? Enter = keep, 0 = follow the default: ")
            except EOFError:
                ans = ""
            choice = pick_device(ans, names, current)
            if choice is not None:
                break
            print(f"  Type a number from 0 to {len(names)}, or press Enter.")
        if choice != current:
            changes["device"] = choice
            cfg["device"] = choice
    dev = cfg.get("device") or default
    print(f"  Listening with: {dev or 'unknown'}"
          + ("" if cfg.get("device") else "   (the macOS default: System Settings > Sound > Input)"))
    if not cfg.get("device"):
        vol = subprocess.run(["osascript", "-e", "input volume of (get volume settings)"],
                             capture_output=True, text=True).stdout.strip()
        if vol.isdigit():
            print(f"  Input volume: {vol}" + ("   <- this is 0: nothing can be heard!" if vol == "0" else ""))
        else:
            print("  Input volume: set on the device itself (USB headsets don't expose it to macOS)")
    pause("  Press Enter, then talk for 5 seconds to see the level meter…")
    p = subprocess.Popen(rec_cmd(str(cfg.get("device") or ""), "trim", "0", "5"),
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
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
    room = measure_room(2, str(cfg.get("device") or ""))
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

    h("5/6  How listening runs, and its settings")
    print("  iTerm (voice listen / voice listen wake): runs until you say \"stop listening\". After you\n"
          "  speak, the mic stays closed while Claude thinks and talks, then reopens by itself (chime).\n"
          "  Claude app (/voice-app): the session listens for one message at a time and starts\n"
          "  listening again after each reply. Both stop after the idle time below.\n"
          "  Enter keeps the current value.")
    for key, kind, text, why in [
            ("silence_seconds", "seconds", "Pause that ends your message (seconds)",
             "How long you can stop talking before what you said is sent. Longer = room to think."),
            ("follow_up_seconds", "seconds", "No-name window after a reply (seconds)",
             "After Claude answers, you can reply without saying \"Claude\" for this long."),
            ("idle_minutes", "minutes", "Mic turns off after (minutes)",
             "With nothing sent for this long, listening stops by itself."),
            ("wake", "yesno", "Always require the wake word (y/n)",
             "y = even `voice listen` only takes sentences starting with \"Claude\".")]:
        print(f"  {why}")
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
        f"{', wake word: Claude' if wake else ''}{', mic ' + repr(cfg['device']) if cfg.get('device') else ''});"
        " say 'stop listening' to end")
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
            if phone_live():       # a Talk page is the microphone now (one input at a time)
                last_active = time.time()   # talking on the phone is not "idle"
                time.sleep(0.5)
                continue
            wait_quiet(cfg)
            log("listening…")
            r = record(cfg, wav, deadline=min(last_active + idle_s,
                                              time.time() + float(cfg["pane_check_seconds"])),
                       also=phone_live)
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
                if CONTROL().exists() or phone_live():
                    continue          # a control request or the Talk page interrupted; handled at the loop top
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


# --- voice benchmark: score Whisper on synthetic speech (no mic, no quiet room) ----------
# `say` writes known sentences to WAV files; each Whisper model transcribes them with the
# word list off (plain Whisper) and on (what the listener really sends); we score the result.
# Computer voices are cleaner than a person in a room: good for comparing models and finding
# words Whisper doesn't know, not for absolute accuracy on YOUR voice.
BENCH_SENTENCES = [
    "Claude, run the tests and tell me what failed.",
    "Commit the changes and push them to GitHub.",
    "Hey Claude, what does this error message mean?",
    "Rename the function and update the TypeScript types.",
    "Claude, open the JSON file and check the API settings.",
    "Can you explain how the Stop hook works?",
    "Claude, switch to the website project.",
    "Read the log file and summarize the last hour.",
]
BENCH_TEMPLATES = ["Claude, tell me about {}.", "Open {} and check the settings.",
                   "What changed in {} today?"]
BENCH_VOICES = ["Samantha", "Daniel"]
# Neural voices as extra speakers (when Kokoro is installed): they sound less like a robot
# than `say`, and add an American woman and a British man with different stress and pace.
KOKORO = "kokoro:"
BENCH_KOKORO = [KOKORO + "af_heart", KOKORO + "bm_george"]
KOKORO_SCRIPT = lambda: Path(__file__).resolve().parent / "voice-kokoro.py"   # noqa: E731
KOKORO_MODEL = lambda: Path(os.environ.get("VOICELOOP_KOKORO_DIR") or          # noqa: E731
                            Path.home() / ".local/share/voiceloop/kokoro") / "kokoro-v1.0.onnx"


def kokoro_available() -> bool:
    return KOKORO_SCRIPT().exists() and KOKORO_MODEL().exists() and bool(shutil.which("uv"))


def kokoro_clips(sentences: list, voice: str, folder: str) -> list:
    """Synthesize every sentence with Kokoro `voice` ("af_heart") in ONE process (the model
    loads once). -> [raw wav path per sentence], or [] if Kokoro failed."""
    os.makedirs(folder, exist_ok=True)
    try:
        p = subprocess.run([str(KOKORO_SCRIPT()), "--to-dir", folder, "--voice", voice],
                           input="\n".join(s.replace("\n", " ") for s in sentences),
                           capture_output=True, text=True, timeout=60 + 10 * len(sentences))
    except (OSError, subprocess.SubprocessError) as e:
        log(f"benchmark: Kokoro {voice} failed: {e}")
        return []
    paths = [os.path.join(folder, f"{i}.wav") for i in range(len(sentences))]
    if p.returncode or not all(os.path.exists(x) for x in paths):
        log(f"benchmark: Kokoro {voice} failed: {p.stderr.strip()[-200:]}")
        return []
    return paths
# Your own recorded clips (voice benchmark --record): a data folder, deliberately NOT under
# ~/.claude (a repo on some Macs). clips.json maps file name -> the sentence you read.
BENCH_DIR = lambda: Path(os.environ.get("VOICELOOP_BENCH_DIR") or   # noqa: E731
                         Path.home() / ".local" / "share" / "voiceloop" / "bench")


# British and American spellings of the same word are the same word to a listener:
# Whisper wrote "summarise" for a British voice saying "summarize". Applied to BOTH sides,
# so a rule only has to be consistent, not a real word ("promise" -> "promize" both ways).
# Deliberately no general -our -> -or rule: "four" would become "for".
_SPELLING = [(re.compile(r"(?<=\w\w)is(e|es|ed|ing|ation|ations)$"), r"iz\1"),   # summarise, organisation
             (re.compile(r"(?<=\w\w)ys(e|es|ed|ing)$"), r"yz\1")]                 # analyse
_SPELLING_WORDS = {"colour": "color", "colours": "colors", "behaviour": "behavior",
                   "favourite": "favorite", "centre": "center", "licence": "license",
                   "grey": "gray", "programme": "program"}


def us_spelling(word: str) -> str:
    word = _SPELLING_WORDS.get(word, word)
    for rx, rep in _SPELLING:
        word = rx.sub(rep, word)
    return word


def norm_words(text: str) -> list:
    return [us_spelling(w) for w in re.sub(r"[^a-z0-9']+", " ", text.lower()).split()]


def word_errors(ref: str, heard: str) -> tuple:
    """(errors, words): word-level edit distance (substitutions + deletions + insertions)
    and the reference length; word error rate = errors / words. Case and punctuation don't
    count, spelling does ("Zeffer in" for "Zephyrine" = 1 substitution + 1 insertion)."""
    r, h = norm_words(ref), norm_words(heard)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1], len(r)


def has_term(term: str, text: str) -> bool:
    """The term appears as whole words (case and punctuation ignored)."""
    t = " ".join(norm_words(term))
    return bool(t) and f" {t} " in f" {' '.join(norm_words(text))} "


def bench_sentences(vocab: list, limit: int = 12) -> list:
    """The built-in sentences, plus one per vocabulary term they don't already contain."""
    out, seen = list(BENCH_SENTENCES), set()
    fresh = []
    for t in vocab:
        k = t.strip().lower()
        if k and k not in seen and not any(has_term(t, s) for s in out):
            seen.add(k)
            fresh.append(t.strip())
    for i, t in enumerate(fresh[:limit]):
        out.append(BENCH_TEMPLATES[i % len(BENCH_TEMPLATES)].format(t))
    return out


def bench_models(folder, names: str = "") -> list:
    """Whisper models in folder (not the Silero VAD), optionally only names like "base,small"."""
    found = sorted(p for p in Path(folder).glob("ggml-*.bin") if "silero" not in p.name)
    want = [n.strip() for n in names.split(",") if n.strip()]
    return [p for p in found if not want or any(p.name[5:].startswith(w) for w in want)]


def bench_summary(rows: list, terms: list, extra_wake=()) -> list:
    """One entry per (model, word list off/on): errors, words, wake caught/total, mean
    seconds per sentence, and which vocabulary terms were missed how often."""
    groups: dict = {}
    for r in rows:
        who = "you" if r["voice"] == "you" else "computer"   # never average your voice into say's
        g = groups.setdefault((r["model"], r["hint"], who), {
            "model": r["model"], "hint": r["hint"], "speaker": who, "errors": 0, "words": 0,
            "wake": 0, "wake_total": 0, "secs": [], "missed": collections.Counter()})
        e, w = word_errors(r["ref"], r["heard"])
        g["errors"] += e
        g["words"] += w
        g["secs"].append(r["secs"])
        if strip_wake(r["ref"])[1]:
            g["wake_total"] += 1
            g["wake"] += strip_wake(r["heard"], extra_wake)[1]
        for t in terms:
            if has_term(t, r["ref"]) and not has_term(t, r["heard"]):
                g["missed"][t] += 1
    for g in groups.values():
        g["secs"] = sum(g["secs"]) / len(g["secs"])
    return list(groups.values())


def bench_report(summary: list) -> str:
    lines = [f"{'speaker':<10}{'model':<12}{'word list':<11}{'accuracy':>9}{'wake':>8}{'s/sentence':>12}"]
    for g in summary:
        acc = max(0.0, 100 * (1 - g["errors"] / g["words"])) if g["words"] else 0.0
        wake = f"{g['wake']}/{g['wake_total']}" if g["wake_total"] else "-"
        lines.append(f"{g.get('speaker', 'computer'):<10}{g['model']:<12}{g['hint']:<11}"
                     f"{acc:>8.1f}%{wake:>8}{g['secs']:>12.2f}")
    missed = [g for g in summary if g["missed"]]
    if missed:
        lines.append("\nWords Whisper got wrong:")
        for g in missed:
            lines.append(f"  {g.get('speaker', 'computer')}, {g['model']}, word list {g['hint']}: " +
                         ", ".join(f"{t} ×{n}" for t, n in g["missed"].most_common()))
    return "\n".join(lines)


def say_voices(listing: str | None = None) -> set:
    """Names `say -v` accepts. Listing lines look like
    "Daniel (English (UK))   en_GB    # Hello! My name is Daniel." or "Samantha   en_US  # …"."""
    if listing is None:
        try:
            listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True,
                                     timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            return set()
    return {re.split(r"\s{2,}|\s\(", line.strip())[0] for line in listing.splitlines() if line.strip()}


def make_clip(text: str, voice: str, path: str, noise: float = 0.0, raw: str = "") -> None:
    """Speech as the mic would deliver it: 16 kHz mono, a little silence around it,
    optionally pink noise (noise = its amplitude, e.g. 0.02) for a room that isn't quiet.
    raw = already-synthesized speech (Kokoro, 24 kHz) to use instead of `say`."""
    if raw:   # Kokoro's 24 kHz -> the mic's 16 kHz mono 16-bit (sox resamples on the way)
        conv = path + ".16k.wav"
        subprocess.run(["sox", raw, "-r", "16000", "-c", "1", "-b", "16", conv],
                       check=True, capture_output=True)
        raw = conv
    else:
        raw = path + ".raw.wav"
        subprocess.run(["say", "-v", voice, "-o", raw, "--data-format=LEI16@16000", text],
                       check=True, capture_output=True, timeout=60)
    if not noise:
        subprocess.run(["sox", raw, path, "pad", "0.3", "0.6"], check=True, capture_output=True)
        return
    with wave.open(raw) as w:
        secs = w.getnframes() / w.getframerate() + 0.9
    hiss = path + ".noise.wav"
    subprocess.run(["sox", "-n", "-r", "16000", "-c", "1", "-b", "16", hiss,
                    "synth", f"{secs:.2f}", "pinknoise", "vol", str(noise)], check=True, capture_output=True)
    padded = path + ".pad.wav"
    subprocess.run(["sox", raw, padded, "pad", "0.3", "0.6"], check=True, capture_output=True)
    subprocess.run(["sox", "-m", padded, hiss, path], check=True, capture_output=True)


def run_benchmark(cfg: dict, sentences: list, voices: list, models: list, noise: float = 0.0,
                  cwd: str = "", progress=lambda: None, recorded=()) -> list:
    """Every sentence × voice × model × word list off/on -> rows for bench_summary().
    recorded = [(sentence, wav)] of your own voice: scored too, as voice "you"."""
    on, off = effective(cfg, cwd), dict(cfg, prompt="", replacements={})
    rows = []

    def score(text, voice, wav):
        for m in models:
            for hint, base in (("off", off), ("on", on)):
                c = dict(base, model=str(m))
                t0 = time.time()
                heard = clean(transcribe(c, wav), c)
                rows.append({"model": Path(m).name[5:-4], "hint": hint, "voice": voice,
                             "ref": text, "heard": heard, "secs": time.time() - t0})
                progress()

    with tempfile.TemporaryDirectory(prefix="voice-bench-") as d:
        neural = {}   # "kokoro:af_heart" -> [raw wav per sentence], synthesized up front
        for v in voices:
            if v.startswith(KOKORO):
                neural[v] = kokoro_clips(sentences, v[len(KOKORO):], os.path.join(d, v[len(KOKORO):]))
        for i, text in enumerate(sentences):
            for v in voices:
                if v.startswith(KOKORO) and not neural.get(v):
                    continue   # Kokoro failed for this voice (logged): leave it out
                wav = os.path.join(d, f"{i}-{re.sub(r'[^A-Za-z0-9]', '', v)}.wav")
                make_clip(text, v, wav, noise, raw=neural[v][i] if v.startswith(KOKORO) else "")
                score(text, v, wav)
    for text, wav in recorded:
        score(text, "you", wav)
    return rows


def recorded_clips(folder=None) -> list:
    """[(sentence, wav path)] from your recordings; missing files and a bad index are skipped."""
    folder = Path(folder or BENCH_DIR())
    try:
        index = json.loads((folder / "clips.json").read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(index, dict):
        return []
    return [(t, str(folder / f)) for f, t in index.items()
            if isinstance(t, str) and re.fullmatch(r"[\w.-]+\.wav", str(f)) and (folder / f).exists()]


def bench_record(cfg: dict, sentences: list, folder, ask=input, rec=None, stt=None) -> int:
    """Read each sentence once; keep the clip. Sentences already recorded are skipped, so
    you can stop (q) and continue later. Returns how many clips were kept this time."""
    rec = rec or record
    ecfg = effective(cfg, os.getcwd())
    stt = stt or (lambda c, w: clean(transcribe(c, w), c))
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    try:
        index = json.loads((folder / "clips.json").read_text())
        index = index if isinstance(index, dict) else {}
    except (OSError, ValueError):
        index = {}
    done, kept = set(index.values()), 0
    todo = [t for t in sentences if t not in done]
    if len(todo) < len(sentences):
        print(f"{len(sentences) - len(todo)} already recorded; {len(todo)} to go.")
    for n, text in enumerate(todo, 1):
        print(f"\n[{n}/{len(todo)}] Say:  {text}")
        while True:
            a = ask("  Enter = record (stops after a pause), s = skip, q = done: ").strip().lower()
            if a in ("q", "s"):
                break
            name = "clip-" + hashlib.sha1(text.encode()).hexdigest()[:10] + ".wav"
            status = rec(cfg, str(folder / name), deadline=time.time() + 20)
            if status != "ok":
                print("  nothing heard (is the room too quiet for the threshold, or no mic permission?)")
                continue
            print(f"  heard: {stt(ecfg, str(folder / name))}")
            if ask("  Enter = keep, r = redo: ").strip().lower() == "r":
                continue
            index[name] = text
            (folder / "clips.json").write_text(json.dumps(index, indent=1))
            kept += 1
            break
        if a == "q":
            break
    print(f"\nKept {kept} new clip(s); {len(index)} in {folder}. Now run: voice benchmark")
    return kept



def bench_cli(argv: list) -> int:
    ap = argparse.ArgumentParser(prog="voice benchmark",
                                 description="Score the Whisper models on computer-spoken sentences "
                                             "(no microphone): word list off vs on.")
    ap.add_argument("--models", default="", help="e.g. base,small (default: every installed model)")
    ap.add_argument("--voices", default="",
                    help="macOS voices for `say`, and Kokoro voices as kokoro:<name> "
                         f"(default: {','.join(BENCH_VOICES + BENCH_KOKORO)}; Kokoro ones if installed)")
    ap.add_argument("--noise", type=float, nargs="?", const=0.02, default=0.0,
                    help="mix in pink noise (amplitude, default 0.02) like a room that isn't quiet")
    ap.add_argument("--quick", action="store_true",
                    help="built-in sentences only (not one per word-list term), first voice only")
    ap.add_argument("--show", action="store_true", help="print every sentence that came back wrong")
    ap.add_argument("--json", action="store_true", help="print the rows and summary as JSON")
    ap.add_argument("--record", action="store_true",
                    help="read the sentences aloud once (in a terminal); later runs score your voice too")
    ap.add_argument("--no-recorded", action="store_true", help="leave your recorded clips out")
    a = ap.parse_args(argv)
    missing = [t for t in ("say", "sox", "whisper-cli") if not shutil.which(t)]
    if missing:
        print(f"voice benchmark needs: {', '.join(missing)}", file=sys.stderr)
        return 1
    cfg, cwd = load_config(), os.getcwd()
    models = bench_models(Path(cfg["model"]).parent, a.models)
    if not models:
        print(f"no Whisper models found in {Path(cfg['model']).parent}", file=sys.stderr)
        return 1
    have = say_voices()
    neural_ok = kokoro_available()
    wanted = a.voices or ",".join(BENCH_VOICES + (BENCH_KOKORO if neural_ok else []))
    usable = lambda v: neural_ok if v.startswith(KOKORO) else v in have   # noqa: E731
    voices = [v.strip() for v in wanted.split(",") if v.strip() and usable(v.strip())]
    skipped = [v.strip() for v in wanted.split(",") if v.strip() and not usable(v.strip())]
    if not voices:
        print(f"none of these voices are installed: {wanted}", file=sys.stderr)
        return 1
    terms = [t.strip() for t in effective(cfg, cwd)["prompt"].split(",") if t.strip()]
    if a.record:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("voice benchmark --record is interactive: run it in a terminal window.", file=sys.stderr)
            return 2
        print("Read each sentence in your normal voice, at your normal distance from the mic.\n"
              "Recording stops after a short pause. Stop any time with q; it continues later.")
        bench_record(cfg, bench_sentences(terms, limit=len(terms)), BENCH_DIR())
        return 0
    recorded = [] if a.no_recorded else recorded_clips()
    sentences = list(BENCH_SENTENCES) if a.quick else bench_sentences(terms, limit=len(terms))
    if a.quick:
        voices = voices[:1]
    total = (len(sentences) * len(voices) + len(recorded)) * len(models) * 2
    if not a.json:
        print(f"voice benchmark: {len(sentences)} sentences × {len(voices)} voice(s) "
              f"({', '.join(voices)}) × {len(models)} model(s) × word list off/on = {total} runs, "
              f"{'pink noise ' + str(a.noise) if a.noise else 'clean audio'}"
              + (f", plus {len(recorded)} of your recordings" if recorded else
                 "" if a.no_recorded else "\n(no recordings of your voice yet: voice benchmark --record)")
              + (f"\n(not installed, skipped: {', '.join(skipped)})" if skipped else ""))
    done = [0]

    def tick():
        done[0] += 1
        if not a.json and sys.stderr.isatty():
            print(f"\r  {done[0]}/{total}", end="", file=sys.stderr, flush=True)

    rows = run_benchmark(cfg, sentences, voices, models, a.noise, cwd, tick, recorded)
    if not a.json and sys.stderr.isatty():
        print("\r" + " " * 20 + "\r", end="", file=sys.stderr)
    summary = bench_summary(rows, terms, cfg.get("wake_words") or ())
    if a.json:
        print(json.dumps({"rows": rows, "summary": summary}, indent=1))
        return 0
    print(bench_report(summary))
    if a.show:
        bad = [r for r in rows if word_errors(r["ref"], r["heard"])[0]]
        print("\nWrong sentences:" if bad else "\nNo wrong sentences.")
        for r in bad:
            print(f"  [{r['model']}, word list {r['hint']}, {r['voice']}]\n"
                  f"    said:  {r['ref']}\n    heard: {r['heard']}")
    print("\nNote: computer voices are cleaner than a real voice in a real room. Use this to\n"
          "compare models and find words to add (voice words add X), not as your own accuracy.")
    return 0


# --- project scan: a vocabulary from the project's own names --------------------------
SCAN_SKIP = {"node_modules", "venv", "dist", "build", "vendor", "target", "__pycache__", "site-packages",
             "tests", "test", "__tests__", "fixtures"}   # test names are written, not spoken
SCAN_CODE = {".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".go", ".rs", ".swift", ".rb", ".php",
             ".java", ".kt", ".c", ".h", ".cpp", ".cs", ".sql"}
SCAN_DOCS = {".md", ".txt"}
SCAN_CHARS, SCAN_TERMS = 300, 40      # half of PROMPT_MAX: your own words must still fit
SCAN_FILES, SCAN_BYTES = 3000, 400_000
DEFINED = re.compile(r"\b(?:def|class|function|const|let|var|func|fn|struct|interface|type|enum)"
                     r"\s+([A-Za-z_][A-Za-z0-9_]*)")
# Not in a dictionary, but Whisper spells them without help (or nobody says them aloud).
SCAN_STOP = set("""args argv kwargs async await bool cfg config configs const ctx dict enum env
    func html http https init json kwarg lib localhost metadata noqa param params plugin plugins
    readme repo repos src stderr stdin stdout str struct sudo tmp todo url urls utf util utils
    api apis app apps css dev docs git sql yaml yml toml txt cli gui ok okay etc eg ie
    macos github homebrew python javascript typescript inbox downloads diff iphone ipad attr
    attrs mtime stdlib setup teardown jsonl beep cookie cookies inline gotcha gotchas lang
    vocab symlink symlinks unsubscribe""".split())
DICT = Path("/usr/share/dict/words")


def load_dictionary(path=None) -> set:
    try:
        return {w.strip().lower() for w in Path(path or DICT).read_text(errors="ignore").split()}
    except OSError:
        return set()


def _known(word: str, words: set) -> bool:
    """In the dictionary, as itself, as a plain inflection (the macOS list has few plurals
    or -ed/-ing forms), or as two dictionary words joined ("timeout", "filename")."""
    w = word.lower()
    stems = [w]
    for suffix in ("s", "es", "ed", "d", "ing", "ly", "er", "ers", "able", "est", "en"):
        if w.endswith(suffix):
            base = w[: -len(suffix)]
            stems += [base, base + "e"] + ([base[:-1]] if base[-2:-1] == base[-1:] else [])   # stopped -> stop
    if w.endswith(("ies", "ied")):
        stems.append(w[:-3] + "y")
    if any(len(s) >= 3 and s in words for s in stems):
        return True
    return any(w[:i] in words and w[i:] in words for i in range(3, len(w) - 2))


def scan_worthy(token: str, words: set) -> bool:
    """A name Whisper is likely to spell wrong: mixed case inside (useEffect, voiceLoop),
    or a word no dictionary knows (Kokoro). Never numbers, ALL CAPS, or very short."""
    if not 3 <= len(token) <= 30 or not token.isalpha() or token.isupper():
        return False
    low = token.lower()
    if low in SCAN_STOP or re.fullmatch(r"x?[a-f]+", low) or low == Path.home().name.lower():
        return False                       # noise words, hex strings (ffe0, xffffb), your login name
    if re.search(r"[a-z][A-Z]", token):
        return True
    return bool(words) and len(token) >= 4 and not _known(token, words)


def scan_terms(root, words=None, have=(), max_chars: int = SCAN_CHARS, max_terms: int = SCAN_TERMS) -> list:
    """Names from a project that are worth a place in Whisper's vocabulary hint, best first.
    Sources and weight: the folder name (5), file names (3), names DEFINED in code (2 each:
    def / class / function / const …), unknown words in the docs (1 each; twice at least).
    Ordered by weight, cut to `max_chars`. `have` = words already listed elsewhere."""
    root = Path(root)
    words = load_dictionary() if words is None else words
    score, spelled, doc_only = collections.Counter(), {}, collections.Counter()

    def add(token: str, weight: int, doc: bool = False) -> None:
        for part in ([token] if re.search(r"[a-z][A-Z]", token) else re.split(r"[_\W]+", token)):
            if scan_worthy(part, words):
                key = part.lower()
                score[key] += weight
                spelled.setdefault(key, collections.Counter())[part] += 1
                if doc:
                    doc_only[key] += 1
                else:
                    doc_only[key] += 2      # a name in code or a file name counts at once

    add(root.resolve().name, 5)
    seen = 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SCAN_SKIP and not d.startswith("."))
        for name in sorted(files):
            p = Path(folder) / name
            ext = p.suffix.lower()
            if name.startswith(".") or ext not in SCAN_CODE | SCAN_DOCS:
                continue
            seen += 1
            if seen > SCAN_FILES:
                break
            add(p.stem, 3)
            try:
                if p.stat().st_size > SCAN_BYTES:
                    continue
                text = p.read_text(errors="ignore")
            except OSError:
                continue
            if ext in SCAN_CODE:
                for m in DEFINED.finditer(text):
                    add(m.group(1), 2)
            else:
                for m in re.finditer(r"[A-Za-z]+(?:'[A-Za-z]+)?", text):
                    if "'" not in m.group(0):          # "doesn't" must not become "doesn"
                        add(m.group(0), 1, doc=True)
    skip = {h.lower() for h in have}
    out = []
    for key, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0])):
        term = spelled[key].most_common(1)[0][0]
        if key in skip or doc_only[key] < 2:
            continue
        if len(out) >= max_terms or len(", ".join(out + [term])) > max_chars:
            break
        out.append(term)
    return out


def scan_cli(args: list) -> int:
    """voice words scan [folder] [--dry]: write what scan_terms() finds to
    <folder>/.claude/voice.json as "scanned". Your own "vocabulary" there is never touched."""
    dry = "--dry" in args
    rest = [a for a in args if a != "--dry"]
    root = Path(rest[0]).expanduser() if rest else Path.cwd()
    if not root.is_dir():
        print(f"scan: not a folder: {root}", file=sys.stderr)
        return 2
    path = root / ".claude" / "voice.json"
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            print(f"scan: {path} is not valid JSON; fix or remove it first", file=sys.stderr)
            return 1
    own = [t for t in data.get("vocabulary") or [] if isinstance(t, str)]
    words = load_dictionary()
    terms = scan_terms(root, words, have=own + load_words()["vocabulary"])
    if not words:
        print("scan: no system dictionary found; only mixedCase names are picked")
    print(f"{root.name}: {len(terms)} name(s) for Whisper" + (":" if terms else ""))
    for t in terms:
        print(f"  {t}")
    if dry:
        print("(--dry: nothing written)")
        return 0
    if terms or "scanned" in data:
        data["scanned"] = terms
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"written to {path} (\"scanned\"). Remove names you don't say; scan again after big changes.")
    used = len(effective(load_config(), str(root))["prompt"])
    print(f"vocabulary hint for this project: {used} of {PROMPT_MAX} characters")
    return 0


def snippets_cli(args: list) -> int:
    """voice snippets [list] | add <name> = <text> | remove <name>   (config.json, "listen")"""
    sub, rest = (args[0] if args else "list"), " ".join(args[1:]).strip()
    have = load_config().get("snippets")
    have = dict(have) if isinstance(have, dict) else {}
    if sub == "add" and "=" in rest:
        name, body = (x.strip() for x in rest.split("=", 1))
        if not re.search(r"[A-Za-z0-9]", name) or not body:
            print("snippets: need a name and a text: voice snippets add <name> = <text>", file=sys.stderr)
            return 2
        have[name.lower()] = body[:SNIPPET_MAX]
        save_listen({"snippets": have})
        print(f"snippet {name.lower()!r} saved. Say: snippet {name.lower()}")
    elif sub == "remove" and rest:
        if have.pop(rest.lower(), None) is None:
            print(f"snippets: no snippet named {rest!r}", file=sys.stderr)
            return 1
        save_listen({"snippets": have})
        print(f"snippet {rest.lower()!r} removed")
    elif sub != "list":
        print("usage: voice snippets [list] | add <name> = <text> | remove <name>", file=sys.stderr)
        return 2
    print(f"{len(have)} snippet(s)" + (" (say \"snippet\" and the name):" if have else
                                        ". Add one: voice snippets add <name> = <text>"))
    for name, body in have.items():
        print(f"  {name}: {body[:70]}{'…' if len(body) > 70 else ''}")
    return 0


def quiet_cli(args: list) -> int:
    """voice quiet [on | off | status]: the lower threshold for a soft voice."""
    sub = args[0] if args else "status"
    if sub == "on":
        QUIET().parent.mkdir(parents=True, exist_ok=True)
        QUIET().write_text("")
    elif sub == "off":
        try:
            QUIET().unlink()
        except OSError:
            pass
    elif sub != "status":
        print("usage: voice quiet [on | off | status]", file=sys.stderr)
        return 2
    cfg = load_config()
    print(f"quiet: {'ON' if quiet_on() else 'off'}  (threshold now {_threshold(cfg) * 100:.2f}%, "
          f"normal {_level(cfg['threshold']) * 100:.2f}%)")
    if sub == "on":
        print("Applies from the next recording. If a recording never ends (the room is louder\n"
              "than the quiet threshold), set \"quiet_threshold\" in config.json a little higher.")
    return 0


def words_cli(args: list) -> int:
    """voice words [list] | add <word or phrase> | fix <heard> = <Right> | remove <word>
    | scan [folder] [--dry]"""
    if args[:1] == ["scan"]:
        return scan_cli(args[1:])
    path, sub = WORDS(), (args[0] if args else "list")
    rest = " ".join(args[1:]).strip()
    lines = path.read_text().splitlines() if path.exists() else [
        "# voiceLoop word list: names and terms Whisper should spell right.",
        "# One per line. \"heard = Right\" also fixes a spelling. Edit freely, or: voice words add …"]
    key = lambda l: l.split("#", 1)[0].split("=", 1)[0].strip().lower()   # noqa: E731
    if sub == "add" and rest:
        new = [w.strip() for w in rest.split(",") if w.strip()]
        have = {key(l) for l in lines}
        added = [w for w in new if w.lower() not in have]
        lines += added
        print(f"added: {', '.join(added) or 'nothing new'}")
    elif sub == "fix" and "=" in rest:
        wrong, right = (x.strip() for x in rest.split("=", 1))
        lines = [l for l in lines if key(l) != wrong.lower()] + [f"{wrong} = {right}"]
        print(f"fix: {wrong!r} -> {right!r}")
    elif sub == "remove" and rest:
        before = len(lines)
        lines = [l for l in lines if key(l) != rest.lower() and l.split("=", 1)[-1].strip().lower() != rest.lower()
                 or l.lstrip().startswith("#")]
        print(f"removed {before - len(lines)} line(s)")
    elif sub != "list":
        print("usage: voice words [list] | add <word>[, <word>…] | fix <heard> = <Right> | remove <word>"
              " | scan [folder] [--dry]", file=sys.stderr)
        return 2
    if sub != "list":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
    w = load_words()
    print(f"{path}: {len(w['vocabulary'])} word(s), {len(w['replacements'])} fix(es)")
    for t in w["vocabulary"]:
        print(f"  {t}")
    for a, b in w["replacements"].items():
        print(f"  {a} -> {b}")
    used = len(effective(load_config(), "")["prompt"])
    print(f"vocabulary hint: {used} of {PROMPT_MAX} characters" +
          ("  (full: later words are dropped; keep the list focused)" if used > PROMPT_MAX - 40 else ""))
    return 0


def main() -> int:
    if sys.argv[1:2] == ["--words"]:
        return words_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--benchmark"]:
        return bench_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--snippets"]:
        return snippets_cli(sys.argv[2:])
    if sys.argv[1:2] == ["--quiet"]:
        return quiet_cli(sys.argv[2:])
    ap = argparse.ArgumentParser(description="voiceLoop conversation mode")
    ap.add_argument("--iterm-session", default=os.environ.get("ITERM_SESSION_ID", ""))
    ap.add_argument("--claude-session", default=os.environ.get("CLAUDE_CODE_SESSION_ID", ""))
    ap.add_argument("--once", action="store_true",
                    help="app mode: wait for ONE usable utterance, print it, exit")
    ap.add_argument("--timeout", type=float, default=None,
                    help="--once: give up after N seconds (default: idle_minutes, like the loop)")
    ap.add_argument("--json", action="store_true", help="--once: print a JSON result")
    ap.add_argument("--wake", action="store_true", help="only accept sentences starting with 'Claude'")
    ap.add_argument("--measure-room", type=float, metavar="SECS", nargs="?", const=2.0,
                    help="record SECS (2) of room noise, print a suggested threshold as JSON")
    ap.add_argument("--setup", action="store_true", help="interactive setup wizard (voice setup)")
    a = ap.parse_args()
    if a.setup:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            print("voice setup is interactive: run it in a terminal window (iTerm, Terminal, or the\n"
                  "Claude app's Terminal panel), not through a pipe or from inside a Claude session.",
                  file=sys.stderr)
            return 2
        return run_setup(load_config())
    if a.measure_room is not None:
        print(json.dumps(measure_room(a.measure_room, str(load_config().get("device") or ""))))
        return 0
    cfg = load_config()
    # A plain SIGTERM would kill Python without running `finally`/`with` cleanup: rec
    # would linger and a recording could stay in the temp dir. Make it a normal exit.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if a.once:
        timeout = a.timeout if a.timeout is not None else float(cfg["idle_minutes"]) * 60
        res = listen_once(cfg, timeout, wake=a.wake or bool(cfg["wake"]), cwd=os.getcwd(),
                          claude_sid=re.sub(r"[^A-Za-z0-9-]", "", a.claude_session))
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
