#!/usr/bin/env -S uv run -q --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
# Name:    voice-setup
# Purpose: voiceLoop's setup as a page in the browser instead of a text wizard. Three
#          screens: (1) is everything ready (tools, models, hooks), showing only what is
#          missing; (2) "say something": a live level bar, what was understood, and the
#          voice answering; (3) done, with the two lines on how to start. Fine-tuning
#          (which microphone, wake-word training, the main settings) is optional and
#          comes after the first success.
#          The page is only the display. The RECORDING happens here, in this process,
#          through the same sox + Whisper path the voice loop uses: macOS grants the
#          microphone per app, so a test through the browser's own microphone could pass
#          while the app that runs voiceLoop still records silence.
# Usage:   voice-setup.py [--port 8446] [--no-open]      (also: `voice setup web`, /voice-setup)
#          Start it from the app you will talk through (the Claude app's session, or your
#          terminal): the microphone test then uses that app's permission.
#          It stops when you press Finish on the page, on Ctrl-C, or after 30 idle minutes.
# Deps:    stdlib; voice-listen.py in the same folder (and what it needs).
# Notes:   This computer only (127.0.0.1), and only its own page may call it (Host and
#          Origin are checked): it can record the microphone and change settings.
#          Nothing is saved without a click. Listener settings go to
#          ~/.claude/voice/config.json (backup first). The hooks button adds voiceLoop's
#          three hook entries to ~/.claude/settings.json: it shows what it will add, keeps
#          a backup next to the file, writes the new file whole, and never removes or
#          changes an entry that is already there.
#          No audio is kept: a recording lives in a temp folder until it is transcribed.
#          The same steps as the terminal wizard (`voice setup`), which keeps working.

from __future__ import annotations

import argparse
import copy
import http.server
import importlib.util
import json
import os
import shutil
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

IDLE_SECONDS = 30 * 60
HOOK_CMD = "$HOME/.claude/hooks/voice-speak.py"
HOOKS = {   # what voiceLoop needs in settings.json: the same three entries install.sh prepares
    "Stop": {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 10}]},
    "UserPromptSubmit": {"hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 5}]},
    "Notification": {"matcher": "permission_prompt|elicitation_dialog",
                     "hooks": [{"type": "command", "command": HOOK_CMD, "timeout": 10}]},
}
SETTINGS = {   # the few listener settings worth a first-day choice: key -> (kind, question, why)
    "wake": ("yesno", "Only listen to sentences that start with “Claude”",
             "Turn this on in a room where other people talk. Off: every sentence is sent."),
    "silence_seconds": ("seconds", "Pause that ends your message (seconds)",
                        "How long you can stop talking before what you said is sent."),
    "follow_up_seconds": ("seconds", "No name needed after a reply (seconds)",
                          "After Claude answers, you can reply without saying “Claude” for this long."),
    "idle_minutes": ("minutes", "Microphone turns itself off after (minutes)",
                     "With nothing said for this long, listening stops by itself."),
}


# --- hooks in Claude Code's settings.json ----------------------------------------------
def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_SETTINGS") or Path.home() / ".claude" / "settings.json")


def read_settings(path: Path):
    """(settings dict, "") | ({}, "") when there is no file yet | (None, why) when it can't be used."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, ""
    except OSError as e:
        return None, f"can't read it ({e.strerror})"
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return None, "it is not valid JSON, so it is left alone"
    return (data, "") if isinstance(data, dict) else (None, "it is not a JSON object, so it is left alone")


def hooks_missing(settings: dict) -> list:
    """Which of our three events have no voice-speak entry yet."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    out = []
    for event in HOOKS:
        groups = hooks.get(event) if isinstance(hooks.get(event), list) else []
        if not any("voice-speak" in str(h.get("command", "")) for g in groups if isinstance(g, dict)
                   for h in (g.get("hooks") or []) if isinstance(h, dict)):
            out.append(event)
    return out


def hooks_merged(settings: dict) -> dict:
    """A copy of the settings with our missing entries APPENDED. Nothing else is touched."""
    new = copy.deepcopy(settings)
    if not isinstance(new.get("hooks"), dict):
        new["hooks"] = {}
    for event in hooks_missing(settings):
        if not isinstance(new["hooks"].get(event), list):
            new["hooks"][event] = []
        new["hooks"][event].append(copy.deepcopy(HOOKS[event]))
    return new


def hooks_apply(path: Path) -> dict:
    """Add the missing hook entries. Backup first, write whole, then read back to be sure."""
    settings, why = read_settings(path)
    if settings is None:
        return {"ok": False, "error": f"{path.name}: {why}"}
    missing = hooks_missing(settings)
    if not missing:
        return {"ok": True, "added": [], "backup": ""}
    backup = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            b = path.with_name(f"{path.name}.bak-voiceloop-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, b)
            backup = b.name
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(hooks_merged(settings), indent=2) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        return {"ok": False, "error": f"could not write {path.name} ({e.strerror})"}
    after, _ = read_settings(path)
    if after is None or hooks_missing(after):
        return {"ok": False, "error": "the file was written but the entries are not there; nothing else was changed"}
    return {"ok": True, "added": missing, "backup": backup}


# --- the microphone, as a stream that reports its level -----------------------------------
class Tap:
    """Wraps the raw audio stream that capture() reads, and reports the level about ten
    times a second, so the page can draw a bar while you talk."""

    def __init__(self, stream, emit, threshold: float):
        self.stream, self.emit, self.threshold = stream, emit, threshold
        self.peak, self.n = 0.0, 0

    def read(self, n: int) -> bytes:
        data = self.stream.read(n)
        if data:
            level = vl._rms(data)
            self.peak = max(self.peak, level)
            self.n += 1
            if self.n % 3 == 0:
                self.emit({"level": round(level * 100, 3), "sound": level > self.threshold})
        return data


class Mic:
    """One open microphone: sox, the same command the listener uses."""

    def __init__(self, device: str = ""):
        self.p = subprocess.Popen(vl.rec_cmd(device), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        self.stream = self.p.stdout

    def close(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
        self.p.wait()


class Setup:
    """One setup run. Everything that touches the outside (mic, Whisper, the voice, the room
    measurement, the device list) is injectable, so the tests need no microphone."""

    def __init__(self, cfg: dict, settings: Path | None = None, mic=None, stt=None, say=None,
                 room=None, devices=None, busy=None, cue=None):
        self.cfg = dict(cfg)
        self.settings = settings or settings_path()
        self.mic = mic or Mic
        self.stt = stt or (lambda c, w: vl.clean(vl.transcribe(c, w), c))
        self.say = say or vl.say_local
        self.room = room or vl.measure_room
        self.devices = devices or vl.input_devices
        self.mic_taken = busy or (lambda: "listener" if vl.listener_running() else "phone" if vl.phone_live() else "")
        self.cue = cue or (lambda event: vl.cue(self.cfg, event, block=True))
        self.changes: dict = {}       # listener settings to save at the end (nothing is saved before)
        self.proposed: list = []      # wake-word spellings the training offered
        self.lock = threading.Lock()  # one recording at a time
        self.finished = threading.Event()
        self.touched = time.time()

    # --- screen 1: is everything ready? --------------------------------------------
    def check(self) -> dict:
        """Only what is in the way of a first try. Each item: ok, what it is, and the one thing to do."""
        self.touched = time.time()
        items = []
        missing = [name for name, tool in (("whisper-cli", "whisper-cli"), ("sox", "rec"), ("uv", "uv"))
                   if not shutil.which(tool)]
        items.append({"key": "tools", "ok": not missing, "title": "The tools are installed",
                      "fix": f"Missing: {', '.join(missing)}. Run ./install.sh in the voiceLoop folder." if missing else ""})
        models = [n for n, p in (("the speech model", self.cfg.get("model")), ("the voice detector", self.cfg.get("vad_model")))
                  if not (p and Path(str(p)).exists())]
        items.append({"key": "models", "ok": not models, "title": "The speech models are here",
                      "fix": f"Missing: {' and '.join(models)}. Run ./install.sh in the voiceLoop folder." if models else ""})
        settings, why = read_settings(self.settings)
        need = hooks_missing(settings) if settings is not None else list(HOOKS)
        items.append({"key": "hooks", "ok": not need, "title": "Claude Code hands its replies to voiceLoop",
                      "fix": "" if not need else f"The settings file can't be changed from here: {why}." if settings is None
                      else "Three small entries (“hooks”) are missing from Claude Code's settings.",
                      "can_apply": bool(need) and settings is not None, "missing": need})
        return {"ready": all(i["ok"] for i in items), "items": items, "mic_taken": self.mic_taken()}

    # --- screen 2: say something ------------------------------------------------------
    def try_it(self, emit) -> None:
        """Room, then one sentence, then the voice answers. emit(event) feeds the page:
        {"step": room|speak|thinking|speaking}, {"level", "sound"}, {"heard"}, {"done"} or
        {"error": listener|phone|busy|silent|nothing|notext, ...}."""
        self.touched = time.time()
        taken = self.mic_taken()
        if taken:
            return emit({"error": taken})
        if not self.lock.acquire(blocking=False):
            return emit({"error": "busy"})
        folder = tempfile.mkdtemp(prefix="voiceloop-setup-")
        try:
            device = str(self.cfg.get("device") or "")
            emit({"step": "room"})
            room = self.room(1.5, device)
            if room.get("ok") and room.get("threshold") and room["threshold"] != self.cfg.get("threshold"):
                self.cfg["threshold"] = self.changes["threshold"] = room["threshold"]   # fits THIS room; saved with the rest
            emit({"step": "speak"})
            status, pcm, peak = self._capture(emit, 20)
            if status != "ok":
                # (almost) pure zeros is what a missing microphone permission looks like
                return emit({"error": "silent" if max(peak, float(room.get("max") or 0) / 100) < 0.001 else "nothing"})
            emit({"step": "thinking"})
            wav = os.path.join(folder, "try.wav")
            vl.write_wav(wav, pcm)
            text = self.stt(vl.effective(self.cfg, os.getcwd()), wav)
            if not text:
                return emit({"error": "notext"})
            emit({"heard": text})
            emit({"step": "speaking"})
            self.say(f"I heard you. You said: {text}")
            emit({"done": True})
        except Exception as e:   # noqa: BLE001  (the page must always get an answer)
            emit({"error": "failed", "detail": type(e).__name__})
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            self.lock.release()
            self.touched = time.time()

    def _capture(self, emit, seconds: float):
        mic = self.mic(str(self.cfg.get("device") or ""))
        tap = Tap(mic.stream, emit, vl._threshold(self.cfg))
        try:
            status, pcm = vl.capture(tap, self.cfg, deadline=time.time() + seconds)
        finally:
            mic.close()
        return status, pcm, tap.peak

    # --- fine-tuning ---------------------------------------------------------------------
    def mics(self) -> dict:
        names, default = self.devices()
        return {"names": names, "default": default, "current": str(self.cfg.get("device") or "")}

    def choose_mic(self, name: str) -> dict:
        names, _ = self.devices()
        if name and name not in names:
            return {"ok": False, "error": "that microphone is not connected"}
        self.cfg["device"] = self.changes["device"] = name      # "" = follow the macOS default
        return {"ok": True}

    def train_wake(self, emit, rounds: int = 5) -> None:
        """Say "Claude" a few times: learn how Whisper writes it in YOUR voice."""
        self.touched = time.time()
        taken = self.mic_taken()
        if taken:
            return emit({"error": taken})
        if not self.lock.acquire(blocking=False):
            return emit({"error": "busy"})
        folder = tempfile.mkdtemp(prefix="voiceloop-setup-")
        try:
            heard, known = [], tuple(self.cfg.get("wake_words") or ())
            for i in range(rounds):
                emit({"round": i, "listen": True})
                self.cue("listening")
                status, pcm, _ = self._capture(emit, 6)
                text = ""
                if status == "ok":
                    wav = os.path.join(folder, "wake.wav")
                    vl.write_wav(wav, pcm)
                    text = self.stt(self.cfg, wav)
                heard.append(text)
                emit({"round": i, "heard": text, "ok": bool(text) and vl.strip_wake(text, known)[1]})
            self.proposed = [w for w in vl.wake_variants(heard) if w not in known]
            emit({"new": self.proposed, "any": any(heard)})
        except Exception as e:   # noqa: BLE001
            emit({"error": "failed", "detail": type(e).__name__})
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            self.lock.release()
            self.touched = time.time()

    def accept_wake(self, words: list) -> dict:
        take = [w for w in words if w in self.proposed]      # only what the training itself offered
        if take:
            self.cfg["wake_words"] = self.changes["wake_words"] = list(self.cfg.get("wake_words") or []) + take
            self.proposed = [w for w in self.proposed if w not in take]
        return {"ok": True, "accepted": take}

    def options(self) -> list:
        return [{"key": k, "kind": kind, "label": label, "why": why, "value": self.cfg.get(k)}
                for k, (kind, label, why) in SETTINGS.items()]

    def set_option(self, key: str, raw) -> dict:
        if key not in SETTINGS:
            return {"ok": False, "error": "unknown setting"}
        kind = SETTINGS[key][0]
        text = ("yes" if raw else "no") if isinstance(raw, bool) else str(raw)
        try:
            value = vl.parse_setting(text, self.cfg.get(key), kind)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        self.cfg[key] = value
        self.changes[key] = value
        return {"ok": True, "value": value}

    # --- screen 3: done ---------------------------------------------------------------------
    def pending(self) -> dict:
        return dict(self.changes)

    def finish(self, save: bool) -> dict:
        saved, backup = {}, ""
        if save and self.changes:
            b = vl.save_listen(self.changes)
            saved, backup, self.changes = dict(self.changes), b.name, {}
        self.finished.set()
        return {"ok": True, "saved": saved, "backup": backup}


# --- the web side -----------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def ours(self) -> bool:
        """Addressed to us by our local name, and (when the browser says where the page came
        from) from our own page: another site open in the browser must not be able to
        record the microphone or change settings through this server."""
        host = self.headers.get("Host", "").lower()
        if host not in self.server.hosts:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and urllib.parse.urlparse(origin).netloc.lower() != host:
            return False
        self.server.setup.touched = time.time()      # somebody is using the page
        return True

    def stream(self, work) -> None:
        """One JSON object per line while `work` runs (a level bar needs many small messages)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event: dict) -> None:
            try:
                self.wfile.write(json.dumps(event).encode() + b"\n")
                self.wfile.flush()
            except OSError:
                pass             # the page went away; the work finishes by itself
        work(emit)

    def body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n)) if 0 < n < 8192 else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def do_GET(self):
        if not self.ours():
            return self.send(403, {"error": "wrong host or origin"})
        s = self.server.setup
        if self.path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/api/check":
            return self.send(200, s.check())
        if self.path == "/api/mics":
            return self.send(200, s.mics())
        if self.path == "/api/options":
            return self.send(200, {"options": s.options(), "pending": s.pending()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.ours():
            return self.send(403, {"error": "wrong host or origin"})
        s, data = self.server.setup, self.body()
        if self.path == "/api/try":
            return self.stream(s.try_it)
        if self.path == "/api/wake":
            return self.stream(s.train_wake)
        if self.path == "/api/wake/accept":
            words = data.get("words")
            return self.send(200, s.accept_wake([str(w) for w in words] if isinstance(words, list) else []))
        if self.path == "/api/mic":
            r = s.choose_mic(str(data.get("name", "")))
            return self.send(200 if r["ok"] else 400, r)
        if self.path == "/api/option":
            r = s.set_option(str(data.get("key", "")), data.get("value", ""))
            return self.send(200 if r["ok"] else 400, r)
        if self.path == "/api/hooks":
            r = hooks_apply(s.settings)
            return self.send(200 if r["ok"] else 400, r)
        if self.path == "/api/privacy":      # the one System Settings pane a newcomer has to find
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.send(200, {"ok": True})
        if self.path == "/api/finish":
            return self.send(200, s.finish(bool(data.get("save"))))
        self.send(404, {"error": "not found"})


def make_server(setup: Setup, port: int) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)   # this computer only
    srv.daemon_threads = True
    srv.setup = setup
    port = srv.server_address[1]
    srv.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    return srv


# --- the page (one file, no outside resources) ---------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voiceLoop setup</title>
<style>
  :root { --bg:#f5f6f8; --card:#fff; --edge:#dfe3e8; --fg:#15181c; --dim:#5f6873; --accent:#0b7a63; --on:#fff;
          --ok:#1a7f37; --warn:#9a6700; --bad:#b42318; --bar:#e6e9ee; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0e1116; --card:#171b22; --edge:#262c36; --fg:#e6edf3; --dim:#8b949e; --accent:#7ee0c4; --on:#06231c;
            --ok:#3fb950; --warn:#d29922; --bad:#f85149; --bar:#232a34; } }
  * { box-sizing:border-box; }
  body { margin:0; padding:32px 16px; background:var(--bg); color:var(--fg); font:17px/1.5 system-ui, -apple-system, sans-serif; }
  main { max-width:620px; margin:0 auto; }
  .steps { display:flex; gap:8px; margin:0 0 18px; padding:0; list-style:none; font-size:13px; color:var(--dim); }
  .steps li { flex:1; padding-top:8px; border-top:3px solid var(--edge); }
  .steps li.now { border-color:var(--accent); color:var(--fg); font-weight:600; }
  .steps li.past { border-color:var(--ok); }
  .card { background:var(--card); border:1px solid var(--edge); border-radius:16px; padding:28px; }
  h1 { margin:0 0 6px; font-size:26px; line-height:1.25; text-wrap:balance; }
  p { margin:0 0 14px; } .lead { color:var(--dim); margin-bottom:22px; }
  .item { display:flex; gap:12px; align-items:flex-start; padding:12px 0; border-top:1px solid var(--edge); }
  .mark { flex:none; width:24px; height:24px; border-radius:50%; display:grid; place-items:center; font-size:14px; font-weight:700;
          background:var(--ok); color:#fff; }
  .mark.no { background:var(--warn); }
  .item b { display:block; font-weight:600; } .item span { color:var(--dim); font-size:15px; }
  button { font:inherit; font-weight:600; padding:13px 22px; border-radius:12px; border:1px solid var(--edge);
           background:var(--card); color:var(--fg); cursor:pointer; }
  button.main { background:var(--accent); border-color:var(--accent); color:var(--on); }
  button.small { padding:7px 14px; font-size:14px; font-weight:500; border-radius:9px; }
  button:disabled { opacity:.5; cursor:default; }
  .acts { display:flex; gap:10px; flex-wrap:wrap; margin-top:22px; align-items:center; }
  .bar { height:26px; background:var(--bar); border-radius:10px; overflow:hidden; margin:18px 0 8px; }
  .bar i { display:block; height:100%; width:0; background:var(--accent); transition:width .08s linear; }
  .bar i.sound { background:var(--ok); }
  #stage { font-size:20px; font-weight:600; min-height:1.6em; }
  #said { margin-top:14px; padding:14px 16px; border-radius:12px; background:var(--bg); border:1px solid var(--edge); font-size:19px; display:none; }
  .note { color:var(--dim); font-size:15px; } .bad { color:var(--bad); }
  .box { margin-top:16px; padding:14px 16px; border-radius:12px; border:1px solid var(--warn); font-size:15px; display:none; }
  .box b { display:block; margin-bottom:4px; }
  details { border-top:1px solid var(--edge); padding:14px 0; } summary { cursor:pointer; font-weight:600; }
  details > div { padding-top:12px; } label { display:block; font-size:15px; margin-top:12px; }
  select, input[type=number] { font:inherit; padding:8px 10px; border-radius:9px; border:1px solid var(--edge); background:var(--bg); color:var(--fg); }
  input[type=number] { width:7em; }
  .dots { display:flex; gap:10px; margin:14px 0; } .dots i { width:20px; height:20px; border-radius:50%; border:2px solid var(--edge); }
  .dots i.yes { background:var(--ok); border-color:var(--ok); } .dots i.no { background:var(--warn); border-color:var(--warn); }
  .dots i.now { border-color:var(--accent); }
  code { background:var(--bg); border:1px solid var(--edge); border-radius:6px; padding:1px 6px; font-size:15px; }
  section { display:none; } section.on { display:block; }
</style></head><body><main>
<ol class="steps"><li id="s1">1 &middot; Ready?</li><li id="s2">2 &middot; Say something</li><li id="s3">3 &middot; Done</li></ol>
<div class="card">

<section id="ready">
  <h1>Almost ready</h1>
  <p class="lead" id="readyLead">One thing is in the way. Fix it and you can talk.</p>
  <div id="items"></div>
  <div class="acts"><button class="main" id="recheck">Check again</button></div>
</section>

<section id="try">
  <h1>Say something</h1>
  <p class="lead">Press the button, stay quiet for a moment, then say a sentence. Anything will do.</p>
  <div id="stage"></div>
  <div class="bar"><i id="lvl"></i></div>
  <div id="said"></div>
  <div class="box" id="help"></div>
  <div class="acts"><button class="main" id="go">Start</button>
    <button id="next" style="display:none">It works. Continue</button></div>
</section>

<section id="done">
  <h1>You're set</h1>
  <p class="lead" id="doneLead">voiceLoop heard you and answered.</p>
  <div class="item"><span class="mark">1</span><div><b>In the Claude app</b><span>Type <code>/voice-app</code> in a session, then just talk.</span></div></div>
  <div class="item"><span class="mark">2</span><div><b>In a terminal with iTerm</b><span>Type <code>voice listen wake</code>, then start each sentence with &ldquo;Claude&rdquo;.</span></div></div>
  <div class="item"><span class="mark">3</span><div><b>To stop</b><span>Say &ldquo;stop listening&rdquo;.</span></div></div>

  <details id="tune"><summary>Fine-tune (optional)</summary><div>
    <label>Microphone<br><select id="mic"></select></label>
    <div id="opts"></div>
    <label style="margin-top:18px">Teach it how you say &ldquo;Claude&rdquo;</label>
    <p class="note">Say &ldquo;Claude&rdquo; on its own after each sound, five times.</p>
    <div class="dots" id="dots"><i></i><i></i><i></i><i></i><i></i></div>
    <div class="note" id="wakeMsg"></div>
    <div class="acts" style="margin-top:10px"><button class="small" id="wake">Start the five tries</button>
      <button class="small" id="wakeOk" style="display:none">Accept</button></div>
  </div></details>

  <p class="note" id="pending" style="margin-top:16px"></p>
  <div class="acts"><button class="main" id="finish">Finish</button><span class="note" id="finishMsg"></span></div>
</section>

</div></main><script>
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api = async (path, body) => (await fetch(path, body === undefined ? {} :
  { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();
function show(id) {
  for (const s of ['ready', 'try', 'done']) $(s).className = s === id ? 'on' : '';
  const n = { ready: 1, try: 2, done: 3 }[id];
  for (let i = 1; i <= 3; i++) $('s' + i).className = i < n ? 'past' : i === n ? 'now' : '';
}
// One line of JSON at a time from a request that stays open while the Mac records.
async function follow(path, onEvent) {
  const r = await fetch(path, { method: 'POST' }), reader = r.body.getReader(), dec = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i; while ((i = buf.indexOf('\n')) >= 0) { const line = buf.slice(0, i); buf = buf.slice(i + 1); if (line) onEvent(JSON.parse(line)); }
  }
}
const level = e => { $('lvl').style.width = Math.min(100, e.level * 12) + '%'; $('lvl').className = e.sound ? 'sound' : ''; };

// --- 1. ready? Shown only when something is missing.
async function check() {
  const d = await api('/api/check');
  if (d.ready) { show('try'); if (d.mic_taken) { $('help').innerHTML = HELP[d.mic_taken]; $('help').style.display = 'block'; } return; }
  const open = d.items.filter(i => !i.ok).length;
  $('readyLead').textContent = (open === 1 ? 'One thing is' : open + ' things are') + ' in the way. Fix ' + (open === 1 ? 'it' : 'them') + ' and you can talk.';
  $('items').innerHTML = d.items.map(i => `<div class="item"><span class="mark ${i.ok ? '' : 'no'}">${i.ok ? '&check;' : '!'}</span><div>
    <b>${esc(i.title)}</b>${i.ok ? '' : `<span>${esc(i.fix)}</span>`}
    ${i.can_apply ? `<div class="acts" style="margin-top:10px"><button class="small" id="hooks">Add them for me</button>
      <span class="note">Adds ${i.missing.length} entr${i.missing.length === 1 ? 'y' : 'ies'} (${i.missing.map(esc).join(', ')}) to <code>~/.claude/settings.json</code>. A backup is kept, nothing else is changed.</span></div>
      <div class="note" id="hooksMsg" style="margin-top:8px"></div>` : ''}</div></div>`).join('');
  if ($('hooks')) $('hooks').onclick = async () => {
    $('hooks').disabled = true;
    const r = await api('/api/hooks', {});
    $('hooksMsg').className = 'note' + (r.ok ? '' : ' bad');
    $('hooksMsg').textContent = r.ok ? `Added. ${r.backup ? 'Backup: ' + r.backup + '. ' : ''}Now restart Claude Code (or type /hooks in the session) so it loads them, then press “Check again”.` : 'Not changed: ' + r.error;
    if (!r.ok) $('hooks').disabled = false;
  };
  show('ready');
}
$('recheck').onclick = check;

// --- 2. say something
const HELP = {
  silent: `<b>The microphone gave pure silence.</b>The app you started this from has no microphone permission yet. Turn it on for that app, then <b>quit and reopen the app</b>: macOS only applies it after a restart.<div class="acts" style="margin-top:10px"><button class="small" id="privacy">Open the microphone settings</button></div>`,
  nothing: `<b>I could hear the room, but no speech.</b>Move closer to the microphone, or raise its input volume, and try again. You have about 20 seconds after the bar starts moving.`,
  notext: `<b>I heard sound, but could not make out words.</b>Try one clear sentence, a little closer to the microphone.`,
  listener: `<b>The microphone is already in use by voiceLoop.</b>Listening is on in a terminal. Say “stop listening” or type <code>voice listen off</code>, then try again.`,
  phone: `<b>A phone is the microphone right now.</b>Close the Talk page on the phone, then try again.`,
  busy: `<b>A test is already running.</b>Wait a moment and try again.`,
  failed: `<b>Something went wrong on the Mac.</b>Try again. If it keeps happening, run <code>./install.sh --check</code> in the voiceLoop folder.`,
};
const STAGE = { room: 'Stay quiet for a moment…', speak: 'Now say a sentence', thinking: 'Got it…', speaking: 'Listen…' };
$('go').onclick = async () => {
  $('go').disabled = true; $('help').style.display = 'none'; $('said').style.display = 'none'; $('next').style.display = 'none';
  let ok = false;
  try {
    await follow('/api/try', e => {
      if (e.step) $('stage').textContent = STAGE[e.step] || '';
      if (e.level !== undefined) level(e);
      if (e.heard) { $('said').textContent = '“' + e.heard + '”'; $('said').style.display = 'block'; }
      if (e.done) ok = true;
      if (e.error) { $('stage').textContent = ''; $('help').innerHTML = HELP[e.error] || HELP.failed; $('help').style.display = 'block';
        if ($('privacy')) $('privacy').onclick = () => api('/api/privacy', {}); }
    });
  } catch (e) { $('help').innerHTML = HELP.failed; $('help').style.display = 'block'; }
  $('lvl').style.width = '0'; $('go').disabled = false; $('go').textContent = 'Try again';
  if (ok) { $('stage').textContent = 'That is what I heard. Did you hear the answer?'; $('next').style.display = ''; $('go').className = ''; $('next').className = 'main'; }
};
$('next').onclick = () => { show('done'); pending(); };

// --- 3. done, and the optional fine-tuning
async function pending() {
  const d = await api('/api/options'), keys = Object.keys(d.pending);
  $('pending').textContent = keys.length ? 'Not saved yet: ' + keys.map(k => k.replace(/_/g, ' ')).join(', ') + '. “Finish” saves it.' : '';
  return d;
}
$('tune').addEventListener('toggle', async () => {
  if (!$('tune').open) return;
  const m = await api('/api/mics');
  $('mic').innerHTML = `<option value="">Follow the Mac (now: ${esc(m.default || 'unknown')})</option>` +
    m.names.map(n => `<option value="${esc(n)}" ${n === m.current ? 'selected' : ''}>${esc(n)}</option>`).join('');
  const d = await pending();
  $('opts').innerHTML = d.options.map(o => `<label>${esc(o.label)}<br>${o.kind === 'yesno'
    ? `<select data-opt="${esc(o.key)}"><option value="no" ${o.value ? '' : 'selected'}>No</option><option value="yes" ${o.value ? 'selected' : ''}>Yes</option></select>`
    : `<input type="number" step="0.5" min="0" data-opt="${esc(o.key)}" value="${esc(o.value)}">`}
    <span class="note" style="display:block">${esc(o.why)}</span><span class="note bad" data-err="${esc(o.key)}"></span></label>`).join('');
});
$('mic').onchange = async () => { await api('/api/mic', { name: $('mic').value }); pending(); };
document.addEventListener('change', async e => {
  const key = e.target.dataset && e.target.dataset.opt; if (!key) return;
  const r = await api('/api/option', { key, value: e.target.value });
  document.querySelector(`[data-err="${key}"]`).textContent = r.ok ? '' : r.error;
  pending();
});
let offered = [];
$('wake').onclick = async () => {
  $('wake').disabled = true; $('wakeOk').style.display = 'none'; $('wakeMsg').textContent = '';
  const dots = [...$('dots').children]; dots.forEach(d => d.className = '');
  await follow('/api/wake', e => {
    if (e.listen) { dots[e.round].className = 'now'; $('wakeMsg').textContent = 'Say “Claude” now (' + (e.round + 1) + ' of 5)'; }
    if (e.heard !== undefined) { dots[e.round].className = e.ok ? 'yes' : 'no'; $('wakeMsg').textContent = e.heard ? 'Heard: “' + e.heard + '”' : 'Heard nothing'; }
    if (e.new) { offered = e.new;
      $('wakeMsg').textContent = !e.any ? 'Nothing was heard. Check the microphone above and try again.'
        : e.new.length ? 'It sometimes writes your “Claude” as: ' + e.new.join(', ') + '. Accept that as the wake word on this Mac?'
        : 'It understands your “Claude”. Nothing to learn.';
      $('wakeOk').style.display = e.new.length ? '' : 'none'; }
    if (e.error) $('wakeMsg').innerHTML = HELP[e.error] || HELP.failed;
  });
  $('wake').disabled = false; $('wake').textContent = 'Try again';
};
$('wakeOk').onclick = async () => { await api('/api/wake/accept', { words: offered }); $('wakeOk').style.display = 'none'; $('wakeMsg').textContent = 'Accepted.'; pending(); };
$('finish').onclick = async () => {
  $('finish').disabled = true;
  const r = await api('/api/finish', { save: true }), keys = Object.keys(r.saved || {});
  $('finishMsg').textContent = (keys.length ? 'Saved (' + keys.map(k => k.replace(/_/g, ' ')).join(', ') + '). ' : '') + 'You can close this page.';
  $('pending').textContent = '';
};
check();
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="voiceLoop setup as a page in your browser (this computer only)")
    ap.add_argument("--port", type=int, default=8446)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    a = ap.parse_args()
    setup = Setup(vl.load_config())
    try:
        srv = make_server(setup, a.port)
    except OSError as e:
        print(f"voice-setup: port {a.port} is not free ({e.strerror}). Try --port.", file=sys.stderr)
        return 1
    url = f"http://localhost:{srv.server_address[1]}/"
    print(f"voiceLoop setup: {url}   (press Finish on the page, or Ctrl-C, to end)", flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if not a.no_open:
        subprocess.run(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while not setup.finished.wait(1.0):
            if time.time() - setup.touched > IDLE_SECONDS and not setup.lock.locked():
                print("voiceLoop setup: nobody used the page for a while; stopped.", flush=True)
                break
        else:
            time.sleep(0.5)          # let the page receive the answer to "Finish"
            print("voiceLoop setup: finished.", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
