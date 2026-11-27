#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# Name:    voice-talk
# Purpose: voiceLoop's Talk page: a phone (or any browser) as a remote microphone for the
#          voice loop. A small web server serves one page; the page streams the mic as
#          16 kHz 16-bit mono PCM; the server runs it through the SAME endpointing, Whisper
#          transcription, word list and Gate (wake word, "stop listening") as the desk
#          listener, and DELIVERS what it heard into the Claude session that is active at
#          the desk, as if it had been said there. The page shows what was heard.
#          Which session (first match): 1. the one the desk listener (`voice listen`) is
#          pointed at; 2. a session waiting in `voice listen once` (/voice-app); 3. the
#          session that replied most recently and still runs Claude in its iTerm pane
#          (--project <name> tries that project's pane first).
#          1 and 3 are typed into the pane; 2 gets it through its inbox file. Once the
#          conversation has a session it STAYS there (for the listener's idle time), shown
#          on the page as "Talking to: <project>"; only rule 1 or 2 moves it.
#          The reply comes back the same way: the Stop hook (voice-speak.py) hands the
#          reply text to this server instead of speaking on the Mac; it is made into audio
#          sentence by sentence (Kokoro, or `say`) and the page plays it. Mac quiet.
#          Two doors: http://localhost:8445/ for a browser on this Mac (always), and
#          https://<this-mac>.local:8444/ for PAIRED phones on the same network (only once
#          `setup` has made a certificate). A phone's browser gives a page the microphone
#          only over trusted HTTPS, and a paired phone can speak into a live Claude
#          session, so neither pairing nor HTTPS is optional there.
# Usage:   voice-talk.py [--port 8445] [--tls-port 8444] [--local] [--wake | --no-wake]
#                        [--project <name>] [--cwd <folder>]          run the server
#          voice-talk.py setup            make the HTTPS certificate with mkcert (no sudo)
#          voice-talk.py ca               show a QR code to put this Mac's certificate
#                                         authority on a phone (once per phone), with the steps
#          voice-talk.py pair [--open]    one-time QR code / link that pairs ONE phone
#                                         (10 minutes; --open shows the QR code in a window)
#          voice-talk.py phones           list paired phones
#          voice-talk.py unpair <name>|all   forget a phone (its token stops working at once)
#          --local: this Mac only, even when a certificate exists.
#          --wake / --no-wake: force the wake word on or off. Default: copy the desk
#          session's mode (else "wake" in the "listen" settings). --cwd: whose word list
#          to use while no session is found (<project>/.claude/voice.json).
#          Spoken: "stop listening"; "command <name>" for names in the listener's allowlist
#          (one that needs a yes is asked on the page: say yes).
#          Routes: GET /            the page
#                  GET /api/status  {"running","project","paired","state","last_turn_at"}: NEVER text
#                  GET /api/events  server-sent events: {"type":"state"|"heard"|"reply"|"ready"}
#                  GET /api/reply/<id>/<n>.wav     one sentence of the reply (425 = not made yet)
#                  POST /api/played?id=<id>        the page finished playing the reply
#                  POST /api/audio?c=<client id>   raw PCM (16 kHz, 16-bit, mono, little-endian)
#                  POST /api/stop | /api/start     end / resume the conversation
# Deps:    stdlib; voice-listen.py in the same folder (and what it needs: whisper-cpp, the
#          models). For phones: brew mkcert (certificate) and qrencode (QR codes).
#          Settings: optional "talk" object in ~/.claude/voice/config.json:
#          {"port": 8445, "tls_port": 8444, "threshold": "0.9%"}  (threshold: the phone is a
#          different microphone from the desk's, so it can have its own; "" = the listener's).
#          Certificate, key and the paired phones live in ~/.local/share/voiceloop/talk/
#          (private to this Mac: never in a repo, a backup share or a mirror).
# Notes:   No audio is kept: one utterance lives in a temp folder until it is transcribed.
#          Heard text goes to the page and to the listener log, never to /api/status.
#          While a page streams, the desk microphone rests (one input at a time): this
#          script touches $TMPDIR/claude-talk.live and voice-listen.py watches it.
#          After a delivery the page is deaf until the reply has been played (half-duplex,
#          like the desk). If the page went away meanwhile, the reply is spoken on the Mac.
#          Reply audio lives in a temp folder only while it plays. Typing into iTerm needs the Automation permission of the app
#          this server was started from. Not on the phone: "switch to <project>" (the
#          phone follows the desk).
#          Requests are checked against the Host and Origin headers, so a web page open in
#          another tab cannot post audio here (cross-site request forgery / DNS rebinding).
#          Pairing: `pair` makes a one-time secret (only its hash is stored); the phone trades
#          it for a long random token in a cookie (HttpOnly, SameSite=Strict, Secure); only
#          the token's hash is stored. From the network, everything except /api/status needs
#          the token. The certificate authority's KEY (mkcert -CAROOT) can vouch for any
#          site to a phone that trusts it: it stays on this Mac. `ca` serves the public
#          certificate only. TLS handshakes run in the request's own thread, so a stalled
#          client cannot block the others.
#          Lessons: [[A phone as a remote microphone — streaming audio to a stdlib Python server]],
#          [[Most recent is not the one I am talking to — sticky targets, marker files and an inbox]],
#          [[Trusted LAN HTTPS with mkcert — giving a phone's browser the microphone]].
#          One page streams at a time: the one tapped last. Any other open page gets 409,
#          says so, and offers "Talk here instead".
#          HTTP/1.1 keep-alive is on: the page posts five times a second, and a new
#          connection per post would cost a TLS handshake each once HTTPS is added.

from __future__ import annotations

import argparse
import hashlib
import http.cookies
import http.server
import importlib.util
import json
import os
import queue
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

sys.dont_write_bytecode = True   # importing the sibling must not leave __pycache__ next to the scripts

_spec = importlib.util.spec_from_file_location(
    "voice_listen", Path(__file__).resolve().parent / "voice-listen.py")
vl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vl)

DEFAULTS = {
    "port": 8445,        # plain HTTP, this Mac only
    "tls_port": 8444,    # HTTPS for paired phones (needs `setup`)
    "threshold": "",     # "" = the listener's threshold; or the phone mic's own, e.g. "0.9%"
}
REPLY_PLAY_MAX = 300     # seconds: longest we wait for the page to finish playing a reply
REPLY_FETCH_MAX = 8.0    # seconds: a page that hasn't asked for the audio by then won't (old page, no tap yet)
MAX_BODY = 256 * 1024    # one upload: 200 ms of audio is 6.4 kB, so this is generous (8 s)
MAX_BUFFER = vl.RATE * 2 * 30   # at most 30 s of audio waits for the worker
LIVE_SECONDS = 2.0       # no upload for this long = the client is gone
CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")


def load_talk_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((vl.voice_dir() / "config.json").read_text()).get("talk", {})
        if isinstance(user, dict):
            cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


# --- where the words go: the Claude session that is active at the desk ---------------
def desk_wake(cfg: dict) -> bool:
    """Is the desk listener (the iTerm loop) in wake mode? It was started with --wake, or
    the setting is on."""
    try:
        pid = int(vl.PIDFILE().read_text().strip())
        args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        args = ""
    return "--wake" in args.split() or bool(cfg.get("wake"))


def find_target(cfg: dict, prefer: str = ""):
    """The session a phone utterance belongs to, or None. See "Which session" in the header.
    prefer: a project name (--project); rule 3 tries that project's pane first.
    {"kind": "iterm"|"app", "claude_session", "cwd", "project", "wake": True|False|None,
     "iterm_session" (iterm only), "fallback": True (rule 3: nobody is listening at the desk)}"""
    panes = vl.load_panes()
    try:
        sid = vl.TARGET().read_text().strip()
    except OSError:
        sid = ""
    if sid and vl.listener_running():
        pane = next((p for p in panes if p.get("claude_session") == sid), None)
        if pane:
            return {**pane, "kind": "iterm", "wake": desk_wake(cfg)}
    waiting = vl.waiting_sessions()
    if waiting:
        w = waiting[0]
        return {"kind": "app", "claude_session": w["claude_session"], "cwd": w["cwd"],
                "project": Path(w["cwd"]).name if w["cwd"] else "", "wake": w["wake"]}
    want = vl.norm(prefer)
    for pane in sorted(panes, key=lambda p: vl.norm(p.get("project", "")) != want) if want else panes:
        if vl.claude_in_pane(pane.get("iterm_session", "")):
            return {**pane, "kind": "iterm", "wake": None, "fallback": True}
    return None


VOICED: set = set()      # sessions whose spoken replies THIS server switched on


def release_voice() -> None:
    """The phone conversation ended: sessions we made speak go quiet again. (Found
    2026-11-27: a session the page had talked to kept speaking during a video call.)
    A session whose voice was already on is left alone."""
    while VOICED:
        (vl.tmp_dir() / f"claude-voice.{VOICED.pop()}").unlink(missing_ok=True)


def deliver(target: dict, text: str, command=None) -> bool:
    """Hand one utterance (or an allowed slash command) to the session. iTerm: typed into
    the pane, after the same "is Claude still running there?" check the desk makes before
    every send (otherwise speech would run as a shell command). App: the session's inbox."""
    sid = target["claude_session"]
    flag = vl.tmp_dir() / f"claude-voice.{sid}"       # replies are spoken, as with the desk listener
    if not flag.exists() and not (vl.voice_dir() / "enabled").exists():
        VOICED.add(sid)                               # we switched it on: release_voice() switches it off
    flag.touch()
    vl.reply_file(sid).unlink(missing_ok=True)        # an old, unplayed reply is not this turn's
    vl.turn_file(sid).write_text(str(time.time()))    # the Stop hook answers on the phone, not the Mac
    if target["kind"] == "app":
        vl.put_inbox(sid, {"status": "command", "heard": text, "command": command[0]} if command
                     else {"status": "heard", "heard": text})
        return True
    if not vl.claude_in_pane(target["iterm_session"]):
        vl.log(f"talk: Claude is no longer running in {target.get('project')}'s pane; not sent")
        return False
    return vl.send(target["iterm_session"], "/" + command[0] if command else text)


# --- the reply: text from the Stop hook -> audio for the page --------------------------
def await_reply(cfg: dict, sent_at: float, sid: str, gone=lambda: False):
    """Wait for this turn's reply. Returns its text; "" when there is nothing to play here
    (the hook spoke on the Mac after all, or speaking was turned off); None = gave up."""
    end = time.time() + float(cfg["reply_timeout"])
    while time.time() < end and not gone():
        try:
            text = vl.reply_file(sid).read_text()
            vl.reply_file(sid).unlink()
            return text
        except OSError:
            pass
        try:
            if vl.spoke_file(sid).stat().st_mtime > sent_at:
                vl.wait_quiet(cfg)
                return ""
        except OSError:
            pass
        if not vl.speech_on(sid):
            return ""
        time.sleep(0.2)
    return None


SPLIT_OVER, MIN_HEAD = 12, 2


def chunks(text: str) -> list:
    """The pieces that become one audio file each: sentences, and a long FIRST sentence is
    split after its first comma, so the first sound comes sooner. The same rule as
    voice-kokoro.py's chunks() (that script can't be imported here: it needs Kokoro)."""
    out = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if not out or len(out[0].split()) <= SPLIT_OVER:
        return out
    words = out[0].split()
    for i, w in enumerate(words[:-1]):
        if w.endswith(",") and i + 1 >= MIN_HEAD:
            return [" ".join(words[:i + 1]), " ".join(words[i + 1:])] + out[1:]
    return out


def speak_config() -> dict:
    try:
        c = json.loads((vl.voice_dir() / "config.json").read_text())
    except (OSError, ValueError):
        c = {}
    return c if isinstance(c, dict) else {}


def synthesize(lines: list, folder: str) -> None:
    """lines -> <folder>/0.wav, 1.wav, ... in order. Kokoro when that is the configured
    voice; `say` otherwise, and for any piece Kokoro did not make (a reply is never lost)."""
    c = speak_config()
    kokoro = Path(__file__).resolve().parent / "voice-kokoro.py"
    if c.get("engine") == "kokoro" and kokoro.exists():
        try:
            subprocess.run([str(kokoro), "--to-dir", folder, "--voice", str(c.get("kokoro_voice") or "bf_emma"),
                            "--speed", str(float(c.get("kokoro_speed") or 1.0))],
                           input="\n".join(lines).encode(), stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            vl.log(f"talk: Kokoro failed ({e!r}); using say")
    for i, line in enumerate(lines):
        wav = os.path.join(folder, f"{i}.wav")
        if os.path.exists(wav):
            continue
        if not os.path.isdir(folder):
            return                               # the reply ended (stopped, or played elsewhere)
        cmd = ["say", "-o", wav + ".part.wav", "--data-format=LEI16@22050", "-r", str(int(c.get("rate") or 200))]
        if c.get("voice"):
            cmd += ["-v", str(c["voice"])]
        subprocess.run(cmd, input=line.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        os.replace(wav + ".part.wav", wav)       # the page never sees half a file


class PhoneStream:
    """Uploaded audio as a blocking byte stream, shaped like the mic pipe that
    voice-listen's capture() reads. When the client stalls, read() returns silence
    (so an utterance in progress still ends); after close() it returns b"" (end of stream)."""

    def __init__(self, stall: float = 0.25):
        self.buf = bytearray()
        self.cv = threading.Condition()
        self.stall = stall
        self.closed = False

    def feed(self, data: bytes) -> None:
        with self.cv:
            self.buf += data
            del self.buf[:max(0, len(self.buf) - MAX_BUFFER)]   # oldest audio goes first
            self.cv.notify_all()

    def clear(self) -> None:
        with self.cv:
            self.buf.clear()

    def close(self) -> None:
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def read(self, n: int) -> bytes:
        with self.cv:
            if len(self.buf) < n and not self.closed:
                self.cv.wait(timeout=self.stall)
            if self.closed:
                return b""
            if len(self.buf) < n:
                return b"\0" * n
            out = bytes(self.buf[:n])
            del self.buf[:n]
            return out


class Talk:
    """One conversation: the audio stream, the worker that turns it into utterances, the
    Gate that decides what counts, and the subscribers (pages) that are told about it.
    stt is injectable, so the tests need no Whisper."""

    def __init__(self, cfg: dict, tcfg: dict | None = None, wake=None, cwd: str = "", stt=None,
                 find=None, send=None, wait_reply=None, synth=None, say_mac=None, alive=None):
        """wake: True/False forces the mode; None copies the desk session's.
        find / send / wait_reply / synth / say_mac / alive: find_target, deliver, await_reply,
        synthesize, "speak on the Mac instead" and claude_in_pane; injectable for the tests."""
        self.cfg = dict(cfg)
        if (tcfg or {}).get("threshold"):
            self.cfg["threshold"] = tcfg["threshold"]
        self.cwd = cwd
        self.stt = stt or (lambda c, w: vl.clean(vl.transcribe(c, w), c))
        self.find = find or (lambda c: find_target(c, self.prefer))
        self.prefer = ""             # --project: which pane rule 3 tries first
        self.send = send or deliver
        self.wait_reply = wait_reply or (lambda sent_at, sid: await_reply(
            self.cfg, sent_at, sid, gone=lambda: self.stopped or self.closed))
        self.synth = synth or synthesize
        self.say_mac = say_mac or vl.say_local
        self.alive = alive or vl.claude_in_pane
        self.busy = False            # the worker is waiting for, or playing, a reply
        self.reply = None            # {"id", "dir", "n", "done"}: the reply being played right now
        self.played = threading.Event()
        self.wake_forced = wake is not None
        self.gate = vl.Gate(bool(cfg.get("wake")) if wake is None else wake, float(cfg["follow_up_seconds"]),
                            stop=lambda t: vl.is_stop(t, cfg), extra_wake=cfg.get("wake_words") or ())
        self.target = None           # the session found for the last utterance
        self.sticky = None           # (target, when): the session this phone conversation is with
        self.confirm = None          # (command, target, deadline): a command waiting for a spoken yes
        self.sent = None             # (claude session, when, reply expected): set by a delivery
        self.marked = 0.0
        self.stream = PhoneStream()
        self.lock = threading.Lock()
        self.subs: list = []
        self.state = "idle"          # idle (nobody streaming) | listening | transcribing | thinking | talking | stopped
        self.owner, self.last_audio = "", 0.0
        self.last_turn_at = 0.0
        self.stopped = self.closed = False

    # --- telling the pages ------------------------------------------------------
    def publish(self, event: dict) -> None:
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)      # a slow page drops events; the worker never waits
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=50)
        q.put_nowait(self._state_event())
        with self.lock:
            self.subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)

    def _state_event(self) -> dict:
        """Everything the page shows besides the words: state, wake mode, WHICH project it
        is talking to, and the page's build (an old tab reloads itself after an update)."""
        return {"type": "state", "state": self.state, "wake": self.gate.wake,
                "project": (self.target or {}).get("project") or "", "build": BUILD}

    def set_state(self, state: str) -> None:
        with self.lock:
            if state == self.state:
                return
            self.state = state
        self.publish(self._state_event())

    def status(self) -> dict:
        """For a hub card. Never text: no transcript, no reply."""
        return {"running": True, "project": (self.target or {}).get("project") or "",
                "paired": 0, "state": self.state, "last_turn_at": int(self.last_turn_at)}

    # --- audio in ---------------------------------------------------------------
    def live(self) -> bool:
        return time.time() - self.last_audio < LIVE_SECONDS

    def feed(self, client: str, data: bytes) -> bool:
        """False = another client is streaming right now."""
        with self.lock:
            if self.owner and client != self.owner and self.live():
                return False
            self.owner, self.last_audio = client, time.time()
        if self.stopped:
            return True                  # accepted and dropped: the page stays connected
        now = time.time()
        if now - self.marked > 1.0:      # tells the desk listener to rest its mic (it checks the age)
            self.marked = now
            try:
                vl.TALK_LIVE().touch()
            except OSError:
                pass
        self.stream.feed(data)
        if self.state == "idle":
            self.set_state("listening")
        return True

    def stop(self) -> None:
        self.stopped = True
        self.stream.clear()
        self.gate.pending, self.confirm = "", None
        self.played.set()                # stop speaking: the worker stops waiting for the page
        release_voice()
        self.set_state("stopped")

    def start(self, client: str = "") -> None:
        """The page was tapped. The page tapped LAST is the one that talks: it takes the
        stream from any other open page (found 2026-11-27: a forgotten tab in another
        window owned the stream, and the visible page could only say "another page is talking")."""
        if client:
            with self.lock:
                self.owner, self.last_audio = client, time.time()
        self.stream.clear()
        self.stopped = False
        if not self.busy:                # a page (re)opened while Claude is still thinking: stay "thinking"
            self.set_state("listening" if self.live() else "idle")

    def close(self) -> None:
        self.closed = True
        self.played.set()
        release_voice()
        self.stream.close()

    # --- audio out: the reply ---------------------------------------------------------
    def speak_reply(self, text: str) -> None:
        """Play the reply on the page, sentence by sentence: each piece is fetched as soon
        as it exists, so the first sound doesn't wait for the whole reply. Blocks until the
        page says it has finished (or went away, or was stopped)."""
        lines = chunks(text)
        if not lines or speak_config().get("engine") == "none":
            return
        if not self.live():              # the page is gone: don't lose the reply
            self.say_mac(text)
            return
        folder = tempfile.mkdtemp(prefix="voiceloop-talk-reply-")
        reply = {"id": secrets.token_hex(4), "dir": folder, "n": len(lines), "done": False, "asked": False}

        def work():
            try:
                self.synth(lines, folder)
            except Exception as e:       # noqa: BLE001
                vl.log(f"talk: making the reply audio failed: {e!r}")
            reply["done"] = True
        self.played.clear()
        self.reply = reply
        threading.Thread(target=work, daemon=True).start()
        self.set_state("talking")
        self.publish({"type": "reply", "id": reply["id"], "n": reply["n"]})
        start = time.time()
        while time.time() < start + REPLY_PLAY_MAX and not self.played.wait(0.25):
            if self.stopped or self.closed:
                break
            if not reply["asked"] and (not self.live() or time.time() > start + REPLY_FETCH_MAX):
                # gone, or connected but not playing (no tap yet): don't lose the reply
                vl.log("talk: the page did not fetch the reply; speaking on the Mac")
                self.say_mac(text)
                break
            if not self.live():
                break
        self.reply = None
        shutil.rmtree(folder, ignore_errors=True)

    def reply_wav(self, rid: str, i: int):
        """One piece of the reply for the page: (200, wav) | (425, b"") not made yet | (410, b"") it never will be | (404, b"")."""
        r = self.reply
        if not r or r["id"] != rid or i >= r["n"]:
            return 404, b""
        r["asked"] = True
        wav, nxt = Path(r["dir"]) / f"{i}.wav", Path(r["dir"]) / f"{i + 1}.wav"
        try:
            if wav.exists() and (nxt.exists() or r["done"]):   # never a file still being written
                return 200, wav.read_bytes()
        except OSError:
            pass
        return (410 if r["done"] else 425), b""

    def reply_played(self, rid: str, client: str) -> None:
        """Only the page that is streaming may end the reply. (Found 2026-11-27: a second,
        untapped tab answered "played" at once and the reply was deleted before it was made.)"""
        if self.reply and self.reply["id"] == rid and client == self.owner:
            self.played.set()

    # --- the worker ---------------------------------------------------------------
    def resolve(self):
        """Find the session for this utterance and copy its wake mode.
        A phone conversation STAYS with its session (for the idle time since its last turn):
        an app session only "waits" between its turns, and rule 3 (the pane that replied most
        recently) would otherwise hop to another project the moment that one answers
        something (found 2026-11-27: sentences meant for one session landed in another).
        Only an explicit choice moves it: the desk listener's target, or a waiting app session."""
        t, now = self.find(self.cfg), time.time()
        if t and not t.get("fallback"):
            self.sticky = (t, now)
        elif self.sticky and now - self.sticky[1] < float(self.cfg["idle_minutes"]) * 60 and \
                (self.sticky[0]["kind"] == "app" or self.alive(self.sticky[0].get("iterm_session", ""))):
            t = self.sticky[0]
        elif t:
            self.sticky = (t, now)
        else:
            self.sticky = None
        if t and t.get("wake") is not None and not self.wake_forced:
            self.gate.wake = bool(t["wake"])
        changed = (t or {}).get("project") != (self.target or {}).get("project")
        self.target = t
        if changed:
            self.publish(self._state_event())
        return t

    def _deliver(self, target, text: str, command, now: float) -> str:
        if not target or not self.send(target, text, command):
            return "nowhere"
        self.last_turn_at = now
        self.sticky = (target, time.time())      # the conversation is with this session now
        expect = bool(command[1].get("reply", True)) if command else True
        self.sent = (target["claude_session"], time.time(), expect)
        return "command" if command else "send"

    def heard(self, text: str, now: float, target=False) -> dict:
        """One cleaned utterance -> what happened to it, as the event the pages get.
        verdict: send (delivered) | command (an allowed slash command, delivered) |
        ask (a command that needs a spoken yes; the question is in "ask") | cancelled |
        hold (sounds unfinished) | ignore (no wake word) | yes (wake word alone) | stop |
        nowhere (no Claude session to deliver to)"""
        if target is False:
            target = self.resolve()
        ask = ""
        if self.confirm:
            command, ctarget, deadline = self.confirm
            self.confirm = None
            if now <= deadline:
                answer = vl.strip_wake(text, self.gate.extra)[0]
                verdict = self._deliver(ctarget, "/" + command[0], command, now) if vl.YES.match(answer) else "cancelled"
                return self._tell(verdict, "/" + command[0])
        verdict, out = self.gate.feed(text, now)
        if verdict == "send" and vl.is_stop(out, self.cfg):
            verdict = "stop"
        elif verdict == "send":
            command = vl.parse_command(out, self.cfg["commands"])
            if command and command[1].get("confirm") and target:
                # the answer needs time: the closing quiet, then Whisper
                wait = float(self.cfg["confirm_seconds"]) + float(self.cfg["silence_seconds"]) + 8
                self.confirm = (command, target, now + wait)
                self.gate.replied(now)       # the yes needs no wake word
                verdict = "ask"
                ask = f"{command[1].get('ask') or 'Run ' + command[0].replace('-', ' ') + '?'} Say yes."
            else:
                verdict = self._deliver(target, out, command, now)
        return self._tell(verdict, out, ask)

    def _tell(self, verdict: str, text: str, ask: str = "") -> dict:
        vl.log(f"talk {verdict}: {text}")
        event = {"type": "heard", "verdict": verdict, "text": text}
        if ask:
            event["ask"] = ask
        self.publish(event)
        if verdict == "stop":
            self.stop()
        return event

    def _interrupted(self) -> bool:
        """capture() asks this every ~90 ms. Also the place where a client that went away is noticed."""
        if self.state == "listening" and not self.live():
            self.set_state("idle")
        return self.stopped or self.closed

    def run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="voiceloop-talk-") as td:
            wav = os.path.join(td, "utt.wav")
            while not self.closed:
                if self.stopped:
                    time.sleep(0.1)
                    continue
                status, pcm = vl.capture(self.stream, self.cfg, interrupted=self._interrupted)
                if status != "ok" or self.stopped or self.closed:
                    continue
                self.set_state("transcribing")
                try:
                    target = self.resolve()
                    vl.write_wav(wav, pcm)
                    text = self.stt(vl.effective(self.cfg, (target or {}).get("cwd") or self.cwd), wav)
                    if text:
                        self.heard(text, time.time(), target)
                except Exception as e:   # noqa: BLE001  (a failed utterance must not end the server)
                    vl.log(f"talk: utterance failed: {e!r}")
                sent, self.sent = self.sent, None
                if sent and not self.stopped:
                    # Half-duplex, as at the desk: deaf until the reply has been spoken.
                    sid, at, expect = sent
                    self.busy = True
                    self.set_state("thinking")
                    try:
                        text = self.wait_reply(at, sid) if expect else time.sleep(1.0)
                        if text and not self.stopped:
                            self.speak_reply(text)
                    except Exception as e:   # noqa: BLE001
                        vl.log(f"talk: the reply failed: {e!r}")
                    self.busy = False
                    self.gate.replied(time.time())   # a reply invites an answer
                    self.stream.clear()              # whatever the mic picked up meanwhile
                    self.publish({"type": "ready"})
                if not self.stopped:
                    self.set_state("listening" if self.live() else "idle")


# --- pairing: which phones may come in ---------------------------------------------------
COOKIE = "voiceloop_talk"
PAIR_SECONDS = 600
_FILES = threading.Lock()


def talk_dir() -> Path:
    d = Path(os.environ.get("VOICELOOP_TALK_DIR") or Path.home() / ".local" / "share" / "voiceloop" / "talk")
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _read(name: str):
    try:
        d = json.loads((talk_dir() / name).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(name: str, data: dict) -> None:
    tmp = talk_dir() / f"{name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)
    os.replace(tmp, talk_dir() / name)


def new_pairing(now: float | None = None) -> str:
    """A one-time secret for ONE phone. Only its hash is kept, with an expiry; a new one
    replaces an unused old one."""
    secret = secrets.token_urlsafe(18)
    with _FILES:
        _write("pair.json", {"hash": _sha(secret), "expires": (now or time.time()) + PAIR_SECONDS})
    return secret


def phones() -> dict:
    """{token hash: {"name", "paired_at"}}"""
    return _read("phones.json")


def redeem(secret: str, name: str = "phone", now: float | None = None):
    """Trade the pairing secret for a token: once, and only in time. Returns the token (the
    phone keeps it in a cookie; we keep its hash), or None."""
    now = now or time.time()
    with _FILES:
        d = _read("pair.json")
        if not d or not secret or not secrets.compare_digest(_sha(secret), str(d.get("hash"))):
            return None
        (talk_dir() / "pair.json").unlink(missing_ok=True)       # used or expired: gone either way
        if now > float(d.get("expires") or 0):
            return None
        token, known = secrets.token_urlsafe(32), phones()
        names, n, unique = {p.get("name") for p in known.values()}, 2, name
        while unique in names:
            unique, n = f"{name} {n}", n + 1
        known[_sha(token)] = {"name": unique, "paired_at": int(now)}
        _write("phones.json", known)
    vl.log(f"talk: paired {unique}")
    return token


def paired(token: str) -> bool:
    return bool(token) and _sha(token) in phones()


def unpair(name: str) -> list:
    """Forget the phone with this name ("all" = every phone). Returns the names removed."""
    with _FILES:
        known = phones()
        gone = [h for h, p in known.items() if name == "all" or p.get("name", "").lower() == name.lower()]
        for h in gone:
            removed = known.pop(h)
            gone[gone.index(h)] = removed.get("name", "?")
        _write("phones.json", known)
    return gone


def phone_name(user_agent: str) -> str:
    for word in ("iPhone", "iPad", "Android", "Macintosh", "Windows", "Linux"):
        if word in (user_agent or ""):
            return "Mac" if word == "Macintosh" else word
    return "phone"


# --- HTTPS: the certificate ---------------------------------------------------------------
def cert_files() -> tuple:
    return talk_dir() / "cert.pem", talk_dir() / "key.pem"


def cert_info(path) -> dict:
    """{"names": [lower-case host names and IPs the certificate is valid for], "until": epoch}"""
    try:
        d = ssl._ssl._test_decode_cert(str(path))     # stdlib-only way to read a certificate file
        return {"names": [v.lower() for _, v in d.get("subjectAltName", ())],
                "until": ssl.cert_time_to_seconds(d["notAfter"])}
    except Exception:   # noqa: BLE001  (missing, unreadable, or the private helper changed)
        return {"names": [], "until": 0}


def host_name() -> str:
    """This Mac's Bonjour name ("My-Mac"): what <name>.local resolves to on the network.
    Not `hostname`, which can be a different name on a managed network."""
    try:
        return subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def lan_ips() -> list:
    out = []
    for i in range(8):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", f"en{i}"], capture_output=True, text=True,
                                timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            ip = ""
        if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", ip):
            out.append(ip)
    return out


# --- the web server ---------------------------------------------------------------
NOT_PAIRED = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>voiceLoop Talk</title><body style="font:18px/1.5 -apple-system,system-ui,sans-serif;background:#111;color:#eee;padding:24px">
<h2>%s</h2><p>On the Mac, run <b>voice-talk.py pair</b> and scan the QR code it shows with this phone.</p>"""
PAIRED_OK = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>voiceLoop Talk</title><body style="font:18px/1.5 -apple-system,system-ui,sans-serif;background:#111;color:#eee;padding:24px">
<h2>Paired.</h2><p><a style="color:#2f7" href="/">Open the Talk page</a></p><script>location.replace('/')</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"    # keep-alive; every reply therefore needs a Content-Length
    ping = 15.0                      # seconds between keep-alive comments on the event stream

    def log_message(self, *a):       # quiet: paths could show up in a terminal otherwise
        pass

    def reply(self, code: int, body: bytes = b"", ctype: str = "application/json", headers=()) -> None:
        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if code >= 400:
            self.send_header("Connection", "close")
            self.close_connection = True     # the request body may be unread
        self.end_headers()
        self.wfile.write(body)

    def refuse(self, code: int, why: str) -> None:
        self.reply(code, json.dumps({"error": why}).encode())

    def trusted(self) -> bool:
        """Only requests addressed to this server by its own name (Host: blocks DNS
        rebinding) and, when a browser says where the page came from, only from our own
        page (Origin: blocks other sites posting audio here)."""
        host = self.headers.get("Host", "").lower()
        if host not in self.server.hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or urllib.parse.urlparse(origin).netloc.lower() == host

    def allowed(self) -> bool:
        """This Mac's own browser (a loopback connection), or a paired phone's token."""
        if self.server.trust_loopback and self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        try:
            morsel = http.cookies.SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE)
        except http.cookies.CookieError:
            morsel = None
        return paired(morsel.value if morsel else "")

    def pair(self, query: str) -> None:
        secret = urllib.parse.parse_qs(query).get("s", [""])[0]
        token = redeem(secret, phone_name(self.headers.get("User-Agent", "")))
        if not token:
            return self.reply(403, (NOT_PAIRED % "This pairing code was already used, or it has expired.").encode(),
                              "text/html; charset=utf-8")
        cookie = f"{COOKIE}={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict" + \
            ("; Secure" if self.server.tls else "")
        # A page of our own, not a redirect: the next request then comes from OUR page, so the
        # strictest cookie setting works, and the secret leaves the address bar.
        self.reply(200, PAIRED_OK.encode(), "text/html; charset=utf-8", [("Set-Cookie", cookie)])

    def do_GET(self):
        if not self.trusted():
            return self.refuse(403, "wrong host or origin")
        talk, u = self.server.talk, urllib.parse.urlparse(self.path)
        path = u.path
        if path == "/api/status":            # for a hub card: anyone on the network, never text
            return self.reply(200, json.dumps({**talk.status(), "paired": len(phones())}).encode())
        if path == "/pair":
            return self.pair(u.query)
        if not self.allowed():
            if path == "/":
                return self.reply(403, (NOT_PAIRED % "This phone is not paired yet.").encode(),
                                  "text/html; charset=utf-8")
            return self.refuse(403, "not paired")
        if path == "/":
            return self.reply(200, PAGE.replace("__BUILD__", BUILD).encode(), "text/html; charset=utf-8")
        if path == "/api/events":
            return self.events(talk)
        m = re.fullmatch(r"/api/reply/([0-9a-f]{8})/(\d{1,3})\.wav", path)
        if m:
            code, wav = talk.reply_wav(m.group(1), int(m.group(2)))
            return self.reply(200, wav, "audio/wav") if code == 200 else self.refuse(code, "no audio")
        self.refuse(404, "not found")

    def events(self, talk: Talk) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")      # a stream has no length: it ends when we close
        self.end_headers()
        self.close_connection = True
        q = talk.subscribe()
        try:
            while not talk.closed:
                try:
                    chunk = "data: " + json.dumps(q.get(timeout=self.ping)) + "\n\n"
                except queue.Empty:
                    chunk = ": ping\n\n"             # also how a page that went away is noticed
                self.wfile.write(chunk.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            talk.unsubscribe(q)

    def do_POST(self):
        if not self.trusted():
            return self.refuse(403, "wrong host or origin")
        if not self.allowed():
            return self.refuse(403, "not paired")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.refuse(411, "Content-Length needed")
        if n < 0 or n > MAX_BODY:
            return self.refuse(413, "too large")
        body = self.rfile.read(n)
        talk, u = self.server.talk, urllib.parse.urlparse(self.path)
        if u.path == "/api/audio":
            client = urllib.parse.parse_qs(u.query).get("c", [""])[0]
            if not CLIENT_ID.fullmatch(client):
                return self.refuse(400, "client id needed")
            if len(body) % 2:
                return self.refuse(400, "16-bit samples: an even number of bytes")
            if not talk.feed(client, body):
                return self.refuse(409, "another client is talking")
            return self.reply(204)
        if u.path == "/api/played":
            q = urllib.parse.parse_qs(u.query)
            talk.reply_played(q.get("id", [""])[0], q.get("c", [""])[0])
            return self.reply(204)
        if u.path == "/api/stop":
            talk.stop()
            return self.reply(204)
        if u.path == "/api/start":
            client = urllib.parse.parse_qs(u.query).get("c", [""])[0]
            talk.start(client if CLIENT_ID.fullmatch(client) else "")
            return self.reply(204)
        self.refuse(404, "not found")


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True            # an open event stream must not keep the process alive
    allow_reuse_address = True
    tls = None                       # an ssl.SSLContext = this server speaks HTTPS
    trust_loopback = True            # connections from this Mac itself need no pairing

    def process_request_thread(self, request, client_address):
        """The TLS handshake happens HERE, in the request's own thread. Wrapping the
        listening socket instead would do it inside accept(), where one phone that opens a
        connection and then says nothing would block everybody else."""
        try:
            if self.tls:
                request.settimeout(10)
                request = self.tls.wrap_socket(request, server_side=True)
                request.settimeout(None)
            self.finish_request(request, client_address)
        except Exception:   # noqa: BLE001
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], OSError):   # a client hanging up (or failing TLS) is not news
            super().handle_error(request, client_address)


def make_server(talk: Talk, port: int) -> Server:
    """Plain HTTP, bound to this Mac only: for the Mac's own browser (localhost is a secure
    context, so the mic works without a certificate)."""
    srv = Server(("127.0.0.1", port), Handler)
    srv.talk = talk
    port = srv.server_address[1]
    srv.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    return srv


def make_tls_server(talk: Talk, port: int, cert, key, bind: str = "0.0.0.0") -> Server:
    """HTTPS on the network, for paired phones. Answers only to the names in the certificate."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(str(cert), str(key))
    srv = Server((bind, port), Handler)
    srv.talk, srv.tls = talk, ctx
    port = srv.server_address[1]
    srv.hosts = {f"{n}:{port}" for n in cert_info(cert)["names"]}
    return srv


# --- the page (one file, no outside resources) ----------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voiceLoop Talk</title>
<style>
  body { font: 18px/1.45 -apple-system, system-ui, sans-serif; margin: 0; padding: 22px 16px; background: #111; color: #eee; text-align: center; }
  button { font: inherit; font-weight: 600; width: 100%; padding: 20px; border: 0; border-radius: 14px; background: #2f7; color: #012; }
  button:disabled { background: #333; color: #aaa; }
  #mute { display: none; margin-top: 10px; background: #345; color: #eee; }
  #mute.on { background: #a33; }
  #state { font-size: 34px; font-weight: 700; margin: 34px 0 8px; }
  #mode { color: #888; font-size: 14px; min-height: 1.4em; }
  #who { color: #9cf; font-size: 16px; min-height: 1.4em; }
  .bar { height: 22px; background: #333; border-radius: 8px; overflow: hidden; margin: 14px 0; }
  .bar i { display: block; height: 100%; width: 0; background: #2f7; }
  #heard { min-height: 3em; margin-top: 18px; color: #9c9; }
  #heard.ignore, #heard.cancelled { color: #888; }
  #heard.nowhere { color: #e88; }
  #heard.hold { color: #cc8; }
  #why { color: #888; font-size: 14px; min-height: 1.4em; }
  #info { color: #666; font-size: 13px; margin-top: 22px; white-space: pre-wrap; }
</style></head><body>
<button id="go">Tap once to start</button>
<button id="mute">Mute my mic</button>
<div id="state">&ndash;</div>
<div id="who"></div>
<div id="mode"></div>
<div class="bar"><i id="lvl"></i></div>
<div id="heard"></div>
<div id="why"></div>
<div id="info"></div>
<script>
const $ = id => document.getElementById(id);
const BUILD = '__BUILD__';
const CLIENT = Array.from(crypto.getRandomValues(new Uint8Array(9)), b => b.toString(16).padStart(2, '0')).join('');
const LABEL = { idle: 'Ready', listening: 'Listening', transcribing: 'Got it…', thinking: 'Thinking…', talking: 'Talking', stopped: 'Stopped' };
const WHY = { send: 'sent to Claude', command: 'command sent', cancelled: 'cancelled', hold: 'sounds unfinished, go on…',
  ignore: 'heard, but ignored (no wake word)', yes: 'Yes?', stop: 'stopped listening',
  nowhere: 'not sent: no Claude session is active at the desk' };
let ctx = null, stream = null, timer = null, out = [], muted = false, quietUntil = 0, server = 'idle', lost = false, busy = false, info = [];
let playing = false, source = null;

const note = t => { info.push(new Date().toLocaleTimeString() + ' ' + t); $('info').textContent = info.slice(-5).join('\n'); };
function show() {
  $('state').textContent = lost ? 'Lost the Mac' : busy ? 'Another page is already talking' : !ctx ? 'Tap to start'
    : playing ? 'Talking' : muted ? 'Muted' : (LABEL[server] || server);
  if (busy && ctx) { $('why').textContent = 'It may be a forgotten tab in another window.'; $('go').disabled = false; $('go').textContent = 'Talk here instead'; }
  else if ($('go').textContent === 'Talk here instead') { $('why').textContent = ''; $('go').disabled = true; $('go').textContent = 'On. Just talk'; }
}

// Cue sounds on THIS device (the desk's cues play on the Mac, where nobody is). The mic is
// zeroed while one sounds, so the listener never hears its own beep.
function beep(freq, ms, when = 0) {
  if (!ctx) return;
  const o = ctx.createOscillator(), g = ctx.createGain(), t = ctx.currentTime + when;
  o.frequency.value = freq; g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, t + ms / 1000); o.connect(g); g.connect(ctx.destination);
  o.start(t); o.stop(t + ms / 1000 + 0.02); quietUntil = Date.now() + when * 1000 + ms + 250;
}
const accepted = () => { beep(660, 90); beep(990, 120, 0.1); };
const cue = { send: accepted, command: accepted, yes: () => beep(880, 120), ask: () => beep(880, 120),
  stop: () => { beep(440, 120); beep(330, 180, 0.13); }, cancelled: () => beep(330, 180), nowhere: () => beep(220, 300) };

// The reply: one sound file per sentence, fetched as soon as the Mac has made it, played in
// order. The mic is zeroed meanwhile, so Claude's voice is never heard as your next sentence.
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function playReply(r) {
  if (!ctx || busy) return;                           // not the page that is talking: not ours to play
  playing = true; show();
  for (let i = 0; i < r.n && playing; i++) {
    let buf = null;
    for (let tries = 0; tries < 240 && !buf && playing; tries++) {
      let res = null; try { res = await fetch('/api/reply/' + r.id + '/' + i + '.wav'); } catch (e) {}
      if (res && res.ok) buf = await ctx.decodeAudioData(await res.arrayBuffer());
      else if (res && res.status !== 425) break;      // 410/404: it will not come
      else await sleep(250);
    }
    if (!buf) break;
    await new Promise(done => { source = ctx.createBufferSource(); source.buffer = buf; source.connect(ctx.destination); source.onended = done; source.start(); });
  }
  source = null;
  await sleep(400);                                   // let the room go quiet before the mic counts again
  playing = false; show();
  fetch('/api/played?id=' + r.id + '&c=' + CLIENT, { method: 'POST' }).catch(() => {});
}
function hush() { playing = false; if (source) { try { source.stop(); } catch (e) {} } }

const es = new EventSource('/api/events');   // reconnects by itself
es.onopen = () => { lost = false; show(); };
es.onerror = () => { lost = true; show(); };
es.onmessage = m => {
  const e = JSON.parse(m.data);
  if (e.type === 'state') {
    if (e.build && e.build !== BUILD) { location.reload(); return; }   // the Mac has a newer page
    $('who').textContent = e.project ? 'Talking to: ' + e.project : '';
    server = e.state; $('mode').textContent = e.wake ? 'Start with “Claude, …”' : 'Every sentence counts';
    if (e.state === 'stopped') hush();
    if (e.state === 'stopped' && ctx) { $('go').disabled = false; $('go').textContent = 'Tap to listen again'; }
  }
  if (e.type === 'heard') {
    $('heard').textContent = e.text ? '“' + e.text + '”' : ''; $('heard').className = e.verdict;
    $('why').textContent = e.ask || WHY[e.verdict] || ''; if (cue[e.verdict]) cue[e.verdict]();
  }
  if (e.type === 'reply') playReply(e);
  if (e.type === 'ready') beep(520, 140);          // your turn
  show();
};

const WORKLET = `registerProcessor('tap', class extends AudioWorkletProcessor {
  process(inputs) { const c = inputs[0][0]; if (c) this.port.postMessage(c.slice(0)); return true; } });`;

async function keepAwake() {
  if (!('wakeLock' in navigator)) return;
  try { await navigator.wakeLock.request('screen'); } catch (e) { note('could not keep the screen on: ' + e.name); }
}
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible' && ctx) keepAwake(); });

function micProblem(e) {     // every mic failure looks like "denied" unless the page says which
  if (!window.isSecureContext) return 'This page is not secure (it needs HTTPS or localhost), so the browser hides the mic.';
  if (!navigator.mediaDevices) return 'This browser has no microphone access for web pages.';
  if (e.name === 'NotAllowedError') return 'Microphone refused: allow it for this site in the browser, and for the browser in the system settings.';
  if (e.name === 'NotFoundError') return 'No microphone found.';
  if (e.name === 'NotReadableError') return 'The microphone is in use by another app.';
  return 'Microphone problem: ' + e.name;
}

function hangUp(why) {       // the mic is gone (page was hidden, or the track ended)
  clearInterval(timer); timer = null; out = []; hush();
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (ctx) ctx.close(); ctx = null; stream = null;
  $('lvl').style.width = '0'; $('mute').style.display = 'none';
  $('go').disabled = false; $('go').textContent = 'Tap to start again'; note(why); show();
}

$('go').onclick = async () => {
  $('go').disabled = true;
  if (ctx) { await fetch('/api/start?c=' + CLIENT, { method: 'POST' }).catch(() => {}); busy = false; $('go').textContent = 'On. Just talk'; show(); return; }
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } }); }
  catch (e) { $('state').textContent = 'No microphone'; $('why').textContent = micProblem(e); $('go').disabled = false; return; }
  $('why').textContent = '';
  ctx = new (window.AudioContext || window.webkitAudioContext)();
  await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' })));
  const node = new AudioWorkletNode(ctx, 'tap');
  ctx.createMediaStreamSource(stream).connect(node);
  stream.getAudioTracks()[0].onended = () => hangUp('the browser closed the mic');
  const ratio = ctx.sampleRate / 16000; let pos = 0, acc = 0, cnt = 0;
  node.port.onmessage = ev => {                    // average down to 16 kHz
    const c = ev.data; let peak = 0;
    for (let i = 0; i < c.length; i++) {
      const v = (muted || playing || Date.now() < quietUntil) ? 0 : c[i]; acc += v; cnt++; pos++;
      const a = Math.abs(v); if (a > peak) peak = a;
      if (pos >= ratio) { pos -= ratio; out.push(Math.max(-1, Math.min(1, acc / cnt))); acc = 0; cnt = 0; }
    }
    $('lvl').style.width = Math.min(100, peak * 300) + '%';
  };
  timer = setInterval(async () => {                // upload every 200 ms as 16-bit PCM
    if (!out.length) return;
    const pcm = new Int16Array(out.length); for (let i = 0; i < out.length; i++) pcm[i] = out[i] * 32767; out = [];
    try { const r = await fetch('/api/audio?c=' + CLIENT, { method: 'POST', body: pcm.buffer }); busy = r.status === 409; }
    catch (e) { /* the event stream reports a lost server */ }
    show();
  }, 200);
  await fetch('/api/start?c=' + CLIENT, { method: 'POST' }).catch(() => {});
  $('go').textContent = 'On. Just talk'; $('mute').style.display = 'block';
  keepAwake(); note('mic open at ' + ctx.sampleRate + ' Hz'); show();
};
$('mute').onclick = () => { muted = !muted; $('mute').textContent = muted ? 'Unmute my mic' : 'Mute my mic'; $('mute').className = muted ? 'on' : ''; show(); };
show();
</script></body></html>
"""

BUILD = hashlib.sha256(PAGE.encode()).hexdigest()[:10]   # changes whenever the page does


def qr(text: str, png: str = "") -> bool:
    """A QR code in the terminal (or as a PNG file). False if qrencode isn't installed."""
    if not shutil.which("qrencode"):
        return False
    cmd = ["qrencode", "-o", png, "-s", "10", text] if png else ["qrencode", "-t", "ANSIUTF8", text]
    return subprocess.run(cmd).returncode == 0


def setup_cli() -> int:
    """Make (or keep) the HTTPS certificate for this Mac's names. mkcert creates a local
    certificate authority on first use; nothing here needs sudo."""
    if not shutil.which("mkcert"):
        print("mkcert is missing: brew install mkcert, then run this again.", file=sys.stderr)
        return 1
    host = host_name()
    if not host:
        print("This Mac has no local host name (System Settings > General > Sharing).", file=sys.stderr)
        return 1
    cert, key = cert_files()
    names = [f"{host}.local", f"{host.lower()}.local", "localhost", "127.0.0.1", *lan_ips()]
    info = cert_info(cert)
    if key.exists() and info["until"] > time.time() + 30 * 86400 and all(n.lower() in info["names"] for n in names):
        print(f"certificate: ok (valid until {time.strftime('%Y-%m-%d', time.localtime(info['until']))})")
    else:
        r = subprocess.run(["mkcert", "-cert-file", str(cert), "-key-file", str(key), *dict.fromkeys(names)],
                           capture_output=True, text=True)
        if r.returncode:
            print("mkcert failed:\n" + r.stderr, file=sys.stderr)
            return 1
        key.chmod(0o600)
        print(f"certificate: made for {', '.join(dict.fromkeys(names))}")
    print(f"""
Next:
  1. Each phone must trust this Mac's certificate authority, once:   voice-talk.py ca
  2. Start the server:                                                voice-talk.py
  3. Pair a phone (one QR code per phone, valid 10 minutes):          voice-talk.py pair
The phone's address will be https://{host.lower()}.local:{load_talk_config()['tls_port']}/
(same Wi-Fi network; if macOS asks whether to allow incoming connections, allow them).""")
    return 0


def ca_cli(seconds: int = 600) -> int:
    """Serve the certificate authority's PUBLIC certificate over plain HTTP for a few minutes,
    so a phone can fetch it. Never the key. (Plain HTTP, because the phone can't trust our
    HTTPS until it has this file.)"""
    try:
        root = Path(subprocess.run(["mkcert", "-CAROOT"], capture_output=True, text=True,
                                   timeout=10).stdout.strip()) / "rootCA.pem"
        pem = root.read_bytes()
    except (OSError, subprocess.SubprocessError):
        print("No certificate authority yet: run voice-talk.py setup first.", file=sys.stderr)
        return 1
    ips = lan_ips()
    if not ips:
        print("This Mac has no network address.", file=sys.stderr)
        return 1

    class CA(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            ok = self.path == "/rootCA.pem"
            self.send_response(200 if ok else 404)
            self.send_header("Content-Type", "application/x-x509-ca-cert")
            self.send_header("Content-Length", str(len(pem) if ok else 0))
            self.end_headers()
            self.wfile.write(pem if ok else b"")

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 0), CA)
    url = f"http://{ips[0]}:{srv.server_address[1]}/rootCA.pem"
    print(f"Scan with the phone's camera (or open {url}):\n")
    qr(url)
    print(f"""
iPhone / iPad:
  1. Open the link in Safari and allow the download of the configuration profile.
  2. Settings > General > VPN & Device Management > install the downloaded profile.
  3. Settings > General > About > Certificate Trust Settings > switch the mkcert certificate ON.
     (Downloading it is not trusting it: step 3 is the one that counts.)
Android: Settings > Security > Encryption & credentials > Install a certificate > CA certificate.
To undo it later, remove the profile / certificate in the same place.
This only ever sends the PUBLIC certificate. Serving for {seconds // 60} minutes; Ctrl-C ends it sooner.""")
    threading.Timer(seconds, srv.shutdown).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


def pair_cli(show: bool) -> int:
    cert, key = cert_files()
    if not (cert.exists() and key.exists()):
        print("No certificate yet: run voice-talk.py setup first.", file=sys.stderr)
        return 1
    url = f"https://{host_name().lower()}.local:{load_talk_config()['tls_port']}/pair?s={new_pairing()}"
    print(f"Pair ONE phone: scan this within {PAIR_SECONDS // 60} minutes (it works once).\n")
    if not qr(url):
        print("(no QR code: brew install qrencode)")
    print(f"\n{url}\n\nThe server must be running (voice-talk.py), and the phone on the same network.")
    if show:
        png = str(talk_dir() / "pair.png")
        if qr(url, png):
            subprocess.run(["open", png])
    return 0


def phones_cli() -> int:
    known = phones()
    for p in sorted(known.values(), key=lambda p: p.get("paired_at", 0)):
        print(f"{p.get('name', '?'):<16} paired {time.strftime('%Y-%m-%d %H:%M', time.localtime(p.get('paired_at', 0)))}")
    if not known:
        print("No phone is paired. (voice-talk.py pair)")
    return 0


def main() -> int:
    sub = sys.argv[1] if len(sys.argv) > 1 else ""
    if sub == "setup":
        return setup_cli()
    if sub == "ca":
        return ca_cli()
    if sub == "pair":
        return pair_cli("--open" in sys.argv[2:])
    if sub == "phones":
        return phones_cli()
    if sub == "unpair":
        gone = unpair(" ".join(sys.argv[2:]).strip()) if sys.argv[2:] else []
        print("Forgot: " + ", ".join(gone) if gone else "usage: voice-talk.py unpair <name>|all   (names: voice-talk.py phones)")
        return 0 if gone else 1
    cfg, tcfg = vl.load_config(), load_talk_config()
    ap = argparse.ArgumentParser(description="voiceLoop Talk page: a browser or a paired phone as the "
                                             "microphone and speaker of the active Claude session",
                                 epilog="other commands: setup | ca | pair [--open] | phones | unpair <name>|all")
    ap.add_argument("--port", type=int, default=int(tcfg["port"]), help="plain HTTP, this Mac only")
    ap.add_argument("--tls-port", type=int, default=int(tcfg["tls_port"]), help="HTTPS for paired phones")
    ap.add_argument("--local", action="store_true", help="this Mac only, even if a certificate exists")
    ap.add_argument("--wake", dest="wake", action="store_true", default=None,
                    help="force the wake word on (default: copy the desk session)")
    ap.add_argument("--no-wake", dest="wake", action="store_false", help="force the wake word off")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--project", default="", help="start with this project's Claude session when "
                    "no listener is running at the desk (default: the one that replied last)")
    a = ap.parse_args()
    talk = Talk(cfg, tcfg, wake=a.wake, cwd=a.cwd)
    talk.prefer = a.project
    try:
        srv = make_server(talk, a.port)
    except OSError as e:
        print(f"voice-talk: port {a.port} is not free ({e.strerror}). Try --port.", file=sys.stderr)
        return 1
    servers, phone_line = [srv], "phones: off (voice-talk.py setup makes the certificate)"
    cert, key = cert_files()
    if a.local:
        phone_line = "phones: off (--local)"
    elif cert.exists() and key.exists():
        try:
            servers.append(make_tls_server(talk, a.tls_port, cert, key))
            phone_line = (f"phones: https://{host_name().lower()}.local:{a.tls_port}/  "
                          f"({len(phones())} paired; pair one with: voice-talk.py pair)")
        except (OSError, ssl.SSLError) as e:
            phone_line = f"phones: off ({e})"
    for f in vl.tmp_dir().glob("claude-talk.turn.*"):   # turns a previous run was waiting on:
        f.unlink(missing_ok=True)                        # their replies are spoken on the Mac
    threading.Thread(target=talk.run, daemon=True).start()
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    # `kill` (and later launchd) sends TERM, not Ctrl-C: end the same clean way.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(f"voiceLoop Talk  (wake word: {'like the desk session' if a.wake is None else 'on' if a.wake else 'off'}; "
          f"Ctrl-C stops)\n  this Mac: http://localhost:{srv.server_address[1]}/\n  {phone_line}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        talk.close()
        for one in servers:
            one.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
