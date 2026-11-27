"""Tests for voice-talk.py — no microphone, no browser.
Audio is uploaded to the server the way the page does it; the last test uses real speech
made with `say`, so the whole path (upload -> endpointing -> Whisper -> Gate -> event) is covered."""

import importlib.util
import sys
sys.dont_write_bytecode = True  # never drop __pycache__ next to the scripts under test
import array
import contextlib
import http.client
import io
import json
import math
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEV = os.environ.get("VOICELOOP_SRC") or ((_ROOT / ".dev-source").read_text().strip()
                                           if (_ROOT / ".dev-source").exists() else "")
_DEV_COPY = Path(_DEV).expanduser() / "voice-talk.py" if _DEV else None
SCRIPT = Path(os.environ.get("VOICE_TALK") or   # a developer's canonical copy if set, else scripts/
              (_DEV_COPY if _DEV_COPY and _DEV_COPY.exists() else _ROOT / "scripts" / "voice-talk.py"))
spec = importlib.util.spec_from_file_location("voice_talk", SCRIPT)
vt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vt)
vl = vt.vl
# Never read the tester's own config.json / words.txt: same results on every Mac.
os.environ["CLAUDE_VOICE_DIR"] = tempfile.mkdtemp(prefix="voiceloop-test-")
vl.log = lambda *a: None   # test sentences must not land in the real listener log
# NEVER the real $TMPDIR: it holds the pane registry and the inboxes of live Claude sessions,
# and a test must not be able to find one, speak into it, or pause the real desk listener.
TMP = Path(tempfile.mkdtemp(prefix="voiceloop-test-tmp-"))
vl.tmp_dir = lambda: TMP
# ...and never the real pairing folder (certificate, key, paired phones).
os.environ["VOICELOOP_TALK_DIR"] = tempfile.mkdtemp(prefix="voiceloop-test-talk-")
CFG = {**vl.DEFAULTS, "silence_seconds": 0.3, "preroll": 0.1}
PANE = {"kind": "iterm", "claude_session": "sess-1", "iterm_session": "w0t0p0:AAAA", "cwd": "/somewhere/myProject",
        "project": "myProject", "wake": None}


def make(wake=False, target=PANE, ok=True, **kw):
    """A Talk whose session is a fake: `t.out` collects what would have been delivered."""
    out = []

    def send(tgt, text, command=None):
        out.append((tgt["claude_session"], text, command[0] if command else None))
        return ok
    kw.setdefault("wait_reply", lambda at, sid: out.append(("waited", sid)))
    kw.setdefault("alive", lambda iterm: True)
    t = vt.Talk(CFG, wake=wake, find=lambda cfg: target, send=send,
                say_mac=lambda text: out.append(("mac says", text)), **kw)
    t.out = out
    return t
HAVE_AUDIO = all(shutil.which(x) for x in ("say", "whisper-cli")) and Path(CFG["model"]).exists()


def tone(secs: float, amp: int = 8000) -> bytes:
    """A 440 Hz tone as 16 kHz 16-bit mono PCM: 'someone is talking' for the endpointing."""
    return array.array("h", (int(amp * math.sin(2 * math.pi * 440 * i / 16000))
                             for i in range(int(16000 * secs)))).tobytes()


def silence(secs: float) -> bytes:
    return b"\0" * (int(16000 * secs) * 2)


def next_event(q, kind, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            e = q.get(timeout=0.1)
        except queue.Empty:
            continue
        if e["type"] == kind:
            return e
    raise AssertionError(f"no {kind!r} event within {timeout} s")


class Stream(unittest.TestCase):
    def test_reads_what_was_fed_in_order(self):
        s = vt.PhoneStream()
        s.feed(b"abcd")
        s.feed(b"ef")
        self.assertEqual(s.read(4), b"abcd")
        self.assertEqual(s.read(2), b"ef")

    def test_a_stalled_client_reads_as_silence(self):
        s = vt.PhoneStream(stall=0.01)
        s.feed(b"ab")                       # less than asked for
        self.assertEqual(s.read(4), b"\0" * 4)
        self.assertEqual(s.read(2), b"ab")  # and nothing was thrown away

    def test_closed_is_end_of_stream(self):
        s = vt.PhoneStream()
        s.feed(b"abcd")
        s.close()
        self.assertEqual(s.read(4), b"")

    def test_keeps_only_the_newest_audio(self):
        s = vt.PhoneStream()
        s.feed(b"\1" * vt.MAX_BUFFER)
        s.feed(b"\2" * 10)
        self.assertEqual(len(s.buf), vt.MAX_BUFFER)
        self.assertEqual(bytes(s.buf[-10:]), b"\2" * 10)


class Heard(unittest.TestCase):
    """What the Gate's verdict becomes for the page. The Gate itself is tested in test_voice_listen."""

    def talk(self, wake):
        return make(wake)

    def test_plain_sentence_counts(self):
        t = self.talk(False)
        self.assertEqual(t.heard("Open the settings file.", 100.0),
                         {"type": "heard", "verdict": "send", "text": "Open the settings file."})
        self.assertEqual(t.last_turn_at, 100.0)

    def test_wake_mode_ignores_and_says_so(self):
        t = self.talk(True)
        e = t.heard("Open the settings file.", 100.0)
        self.assertEqual((e["verdict"], t.last_turn_at), ("ignore", 0.0))

    def test_wake_word_comes_off(self):
        e = self.talk(True).heard("Claude, open the settings file.", 100.0)
        self.assertEqual((e["verdict"], e["text"]), ("send", "open the settings file."))

    def test_wake_word_alone(self):
        self.assertEqual(self.talk(True).heard("Claude?", 100.0)["verdict"], "yes")

    def test_unfinished_sentence_is_held_then_joined(self):
        t = self.talk(False)
        self.assertEqual(t.heard("Open the file and", 100.0)["verdict"], "hold")
        e = t.heard("show me the top.", 103.0)
        self.assertEqual((e["verdict"], e["text"]), ("send", "Open the file and show me the top."))

    def test_speech_never_becomes_a_command(self):
        self.assertEqual(self.talk(True).heard("Claude, /clear", 100.0)["text"], "clear")

    def test_stop_listening_stops(self):
        t = self.talk(True)                       # no wake word needed to stop
        self.assertEqual(t.heard("Stop listening.", 100.0)["verdict"], "stop")
        self.assertTrue(t.stopped)
        self.assertEqual(t.state, "stopped")
        self.assertTrue(t.feed("a", tone(0.1)))   # still accepted...
        self.assertEqual(len(t.stream.buf), 0)    # ...and dropped
        t.start()
        self.assertFalse(t.stopped)

    def test_phone_can_have_its_own_threshold(self):
        self.assertEqual(vt.Talk(CFG, {"threshold": "1.5%"}).cfg["threshold"], "1.5%")
        self.assertEqual(vt.Talk(CFG, {"threshold": ""}).cfg["threshold"], CFG["threshold"])


class Delivery(unittest.TestCase):
    """What reaches the Claude session. `out` holds (session, text, command)."""

    def test_sentence_is_delivered_to_the_found_session(self):
        t = make()
        self.assertEqual(t.heard("Open the settings file.", 100.0)["verdict"], "send")
        self.assertEqual(t.out, [("sess-1", "Open the settings file.", None)])
        self.assertEqual(t.status()["project"], "myProject")

    def test_nothing_is_delivered_unless_the_gate_says_send(self):
        t = make(True)
        for said in ("Open the settings file.", "Claude?", "Claude, open the file and", "Stop listening."):
            t.heard(said, 100.0)
        self.assertEqual(t.out, [])

    def test_no_session_is_said_not_swallowed(self):
        t = make(target=None)
        self.assertEqual(t.heard("Open the settings file.", 100.0)["verdict"], "nowhere")
        self.assertEqual((t.out, t.last_turn_at, t.sent), ([], 0.0, None))

    def test_failed_delivery_is_nowhere_too(self):      # e.g. Claude was quit in that pane
        t = make(ok=False)
        self.assertEqual(t.heard("Open the settings file.", 100.0)["verdict"], "nowhere")
        self.assertIsNone(t.sent)

    def test_wake_mode_is_copied_from_the_desk_session(self):
        t = make(wake=None, target={**PANE, "wake": True})
        self.assertEqual(t.heard("Open the settings file.", 100.0)["verdict"], "ignore")
        t = make(wake=None, target={**PANE, "wake": False})
        self.assertEqual(t.heard("Open the settings file.", 100.0)["verdict"], "send")

    def test_forced_wake_mode_wins(self):
        t = make(wake=False, target={**PANE, "wake": True})
        self.assertEqual(t.heard("Open the settings file.", 100.0)["verdict"], "send")

    def test_allowed_command_without_confirmation(self):
        t = make()
        e = t.heard("Command compact.", 100.0)
        self.assertEqual(e["verdict"], "command")
        self.assertEqual(t.out, [("sess-1", "Command compact.", "compact")])
        self.assertFalse(t.sent[2])                      # /compact gets no spoken reply: don't wait for one

    def test_unknown_command_is_an_ordinary_sentence(self):
        t = make()
        self.assertEqual(t.heard("Command delete everything.", 100.0)["verdict"], "send")
        self.assertEqual(t.out[0][2], None)

    def test_clear_asks_first_and_runs_on_yes(self):
        t = make(True)
        e = t.heard("Claude, command clear.", 100.0)
        self.assertEqual((e["verdict"], e["ask"]), ("ask", "Clear this conversation? Say yes."))
        self.assertEqual(t.out, [])
        e = t.heard("Yes.", 104.0)                       # no wake word needed for the answer
        self.assertEqual((e["verdict"], e["text"]), ("command", "/clear"))
        self.assertEqual(t.out, [("sess-1", "/clear", "clear")])

    def test_clear_is_cancelled_by_anything_else(self):
        t = make()
        t.heard("Command clear.", 100.0)
        self.assertEqual(t.heard("No, wait.", 104.0)["verdict"], "cancelled")
        self.assertEqual(t.out, [])
        self.assertEqual(t.heard("Yes.", 106.0)["verdict"], "send")   # a later yes is just a sentence

    def test_a_late_yes_does_not_clear(self):
        t = make()
        t.heard("Command clear.", 100.0)
        self.assertEqual(t.heard("Yes.", 200.0)["verdict"], "send")
        self.assertEqual(t.out, [("sess-1", "Yes.", None)])

    def test_app_session_keeps_the_phone_between_its_turns(self):
        app = {"kind": "app", "claude_session": "app-1", "cwd": "/x/appProj", "project": "appProj", "wake": False}
        found = [app]
        t = make(target=None)
        t.find = lambda cfg: found[0]
        t.heard("First sentence here.", 100.0)
        found[0] = {**PANE, "fallback": True}            # it is thinking; another pane replied last
        t.heard("Second sentence here.", 101.0)
        found[0] = None
        t.heard("Third sentence here.", 102.0)
        self.assertEqual([o[0] for o in t.out], ["app-1"] * 3)
        found[0] = PANE                                  # the desk listener points somewhere: that wins
        t.heard("Fourth sentence here.", 103.0)
        self.assertEqual(t.out[-1][0], "sess-1")


class StaysWithItsSession(unittest.TestCase):
    """Bug 2026-11-27: with two Claude panes open, the phone followed whichever pane had
    replied last, so sentences meant for one project were typed into the other."""

    def setUp(self):
        self.a = {**PANE, "fallback": True}
        self.b = {**PANE, "claude_session": "sess-2", "iterm_session": "w0t0p1:BBBB", "project": "other", "fallback": True}
        self.found = [self.a]
        self.running = {"w0t0p0:AAAA", "w0t0p1:BBBB"}
        self.t = make(target=None, alive=lambda iterm: iterm in self.running)
        self.t.find = lambda cfg: self.found[0]

    def say(self, n=1):
        for _ in range(n):
            self.t.heard("Open the settings file.", 100.0)
        return [o[0] for o in self.t.out]

    def test_another_pane_replying_does_not_take_the_phone(self):
        self.say()
        self.found[0] = self.b                       # the other project just answered something
        self.assertEqual(self.say(2), ["sess-1"] * 3)
        self.assertEqual(self.t.status()["project"], "myProject")

    def test_it_moves_when_claude_was_quit_there(self):
        self.say()
        self.found[0] = self.b
        self.running.discard("w0t0p0:AAAA")
        self.assertEqual(self.say(), ["sess-1", "sess-2"])

    def test_it_moves_after_the_idle_time(self):
        self.say()
        self.found[0] = self.b
        self.t.sticky = (self.t.sticky[0], time.time() - float(CFG["idle_minutes"]) * 60 - 1)
        self.assertEqual(self.say(), ["sess-1", "sess-2"])

    def test_the_desk_listener_pointing_elsewhere_moves_it(self):
        self.say()
        self.found[0] = {**self.b, "fallback": False, "wake": False}
        self.assertEqual(self.say(), ["sess-1", "sess-2"])
        self.found[0] = self.a                       # ...and it stays there afterwards
        self.assertEqual(self.say()[-1], "sess-2")

    def test_the_page_is_told_which_project(self):
        q = self.t.subscribe()
        self.assertEqual(q.get_nowait()["project"], "")
        self.say()
        self.assertEqual(q.get_nowait()["project"], "myProject")


class FindTarget(unittest.TestCase):
    def setUp(self):
        names = ("listener_running", "claude_in_pane", "send")
        saved = {n: getattr(vl, n) for n in names}
        self.addCleanup(lambda: [setattr(vl, n, f) for n, f in saved.items()])
        self.addCleanup(self.wipe)
        self.wipe()
        vl.listener_running = lambda: False
        self.alive = set()
        vl.claude_in_pane = lambda iterm: iterm in self.alive

    def wipe(self):
        shutil.rmtree(TMP / "claude-voice-panes", ignore_errors=True)
        for f in TMP.glob("claude-*"):
            f.unlink()

    def pane(self, sid, project, ago=0):
        d = TMP / "claude-voice-panes"
        d.mkdir(exist_ok=True)
        (d / f"{sid}.json").write_text(json.dumps({"claude_session": sid, "iterm_session": f"w0:{sid}",
                                                   "cwd": f"/p/{project}", "project": project, "ts": time.time() - ago}))

    def test_nothing_open(self):
        self.assertIsNone(vt.find_target(CFG))

    def test_the_desk_listeners_session_comes_first(self):
        self.pane("s-old", "alpha", ago=500)
        self.pane("s-new", "beta")
        self.alive = {"w0:s-old", "w0:s-new"}
        vl.TARGET().write_text("s-old")
        vl.listener_running = lambda: True
        t = vt.find_target({**CFG, "wake": True})
        self.assertEqual((t["kind"], t["claude_session"], t["project"], t["wake"]), ("iterm", "s-old", "alpha", True))

    def test_a_stale_target_file_is_not_trusted(self):      # the listener is gone, its file stayed
        self.pane("s-old", "alpha")
        vl.TARGET().write_text("s-old")
        self.assertIsNone(vt.find_target(CFG))

    def test_a_waiting_app_session(self):
        self.pane("s-new", "beta")
        self.alive = {"w0:s-new"}
        vl.once_file("app-1").write_text(json.dumps({"pid": os.getpid(), "cwd": "/p/gamma", "wake": True, "ts": time.time()}))
        t = vt.find_target(CFG)
        self.assertEqual((t["kind"], t["claude_session"], t["project"], t["wake"]), ("app", "app-1", "gamma", True))

    def test_else_the_newest_pane_that_still_runs_claude(self):
        self.pane("s-old", "alpha", ago=500)
        self.pane("s-new", "beta")
        self.alive = {"w0:s-old"}                            # Claude was quit in the newest one
        t = vt.find_target(CFG)
        self.assertEqual((t["claude_session"], t["wake"], t["fallback"]), ("s-old", None, True))

    def test_a_preferred_project_is_tried_first(self):
        self.pane("s-old", "alpha", ago=500)
        self.pane("s-new", "beta")
        self.alive = {"w0:s-old", "w0:s-new"}
        self.assertEqual(vt.find_target(CFG)["project"], "beta")
        self.assertEqual(vt.find_target(CFG, prefer="Alpha")["project"], "alpha")
        self.alive = {"w0:s-new"}                            # not running there: the usual rule
        self.assertEqual(vt.find_target(CFG, prefer="alpha")["project"], "beta")

    def test_deliver_to_an_app_session_goes_to_its_inbox(self):
        app = {"kind": "app", "claude_session": "app-1", "cwd": "/p/gamma"}
        vl.reply_file("app-1").write_text("an old reply nobody played")
        self.assertTrue(vt.deliver(app, "Open the settings file."))
        self.assertTrue(vl.turn_file("app-1").exists())                 # the Stop hook will answer on the phone
        self.assertFalse(vl.reply_file("app-1").exists())
        self.assertTrue((TMP / "claude-voice.app-1").exists())          # its replies will be spoken
        self.assertEqual(vl.take_inbox("app-1"), {"status": "heard", "heard": "Open the settings file.", "via": "phone"})
        vt.deliver(app, "Command wrap session.", ("wrap-session", {}))
        self.assertEqual(vl.take_inbox("app-1")["command"], "wrap-session")

    def test_voice_we_switched_on_goes_off_when_the_conversation_ends(self):
        mine, theirs = TMP / "claude-voice.app-1", TMP / "claude-voice.app-2"
        theirs.touch()                                       # this one was speaking before the phone came
        for sid in ("app-1", "app-2"):
            vt.deliver({"kind": "app", "claude_session": sid, "cwd": "/p"}, "Open the settings file.")
        self.assertTrue(mine.exists() and theirs.exists())
        vt.Talk(CFG, find=lambda c: None).stop()             # "stop listening", or the server ends
        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    def test_deliver_to_a_pane_checks_claude_first(self):
        typed = []
        vl.send = lambda iterm, text: typed.append((iterm, text)) or True
        pane = {**PANE, "iterm_session": "w0:s-1"}
        self.assertFalse(vt.deliver(pane, "rm -rf everything"))         # Claude is gone: it would hit the shell
        self.assertEqual(typed, [])
        self.alive = {"w0:s-1"}
        self.assertTrue(vt.deliver(pane, "Open the settings file."))
        self.assertTrue(vt.deliver(pane, "Command compact.", ("compact", {})))
        self.assertEqual(typed, [("w0:s-1", "Open the settings file."), ("w0:s-1", "/compact")])


class Reply(unittest.TestCase):
    """Seam 2: the reply text from the Stop hook becomes audio for the page."""

    def setUp(self):
        saved = (vl.wait_quiet, vl.speech_on)
        self.addCleanup(lambda: [setattr(vl, n, f) for n, f in zip(("wait_quiet", "speech_on"), saved)])
        vl.wait_quiet = lambda cfg: None
        vl.speech_on = lambda sid: True
        for f in TMP.glob("claude-*"):
            if f.is_file():
                f.unlink()

    def test_chunks_like_the_kokoro_speaker(self):
        self.assertEqual(vt.chunks("One. Two! Three?"), ["One.", "Two!", "Three?"])
        long = "When the tests have all finished running, tell me which ones failed and why they did. Then stop."
        self.assertEqual(vt.chunks(long), ["When the tests have all finished running,",
                                           "tell me which ones failed and why they did.", "Then stop."])
        self.assertEqual(vt.chunks("  "), [])

    def test_await_reply_takes_the_text_once(self):
        vl.reply_file("s1").write_text("It worked. Details follow.")
        self.assertEqual(vt.await_reply(CFG, time.time(), "s1"), "It worked. Details follow.")
        self.assertFalse(vl.reply_file("s1").exists())

    def test_await_reply_when_the_mac_spoke_instead(self):
        vl.spoke_file("s1").write_text("")
        self.assertEqual(vt.await_reply(CFG, time.time() - 5, "s1"), "")

    def test_await_reply_when_speaking_is_off(self):
        vl.speech_on = lambda sid: False
        self.assertEqual(vt.await_reply(CFG, time.time(), "s1"), "")

    def test_await_reply_gives_up(self):
        self.assertIsNone(vt.await_reply({**CFG, "reply_timeout": 0.3}, time.time(), "s1"))
        self.assertIsNone(vt.await_reply(CFG, time.time(), "s1", gone=lambda: True))

    def fake_synth(self, lines, folder):
        for i, line in enumerate(lines):
            Path(folder, f"{i}.wav").write_bytes(b"RIFF" + line.encode())

    def talking(self, **kw):
        t = make(stt=lambda c, w: "Open the settings file.", wait_reply=lambda at, sid: "It worked. Second part.",
                 synth=self.fake_synth, **kw)
        th = threading.Thread(target=t.run, daemon=True)
        th.start()
        self.addCleanup(th.join, 3)
        self.addCleanup(t.close)
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6))
        return t, q

    def test_reply_is_played_on_the_page_piece_by_piece(self):
        t, q = self.talking()
        e = next_event(q, "reply")
        self.assertEqual(e["n"], 2)
        self.assertEqual(t.state, "talking")
        deadline = time.time() + 3
        while t.reply_wav(e["id"], 1)[0] != 200 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(t.reply_wav(e["id"], 0), (200, b"RIFFIt worked."))
        self.assertEqual(t.reply_wav(e["id"], 1), (200, b"RIFFSecond part."))
        self.assertEqual(t.reply_wav(e["id"], 2)[0], 404)
        self.assertEqual(t.reply_wav("00000000", 0)[0], 404)
        folder = t.reply["dir"]
        t.feed("a", tone(0.3))                           # the room, while the reply plays
        t.reply_played("00000000", "a")                  # not this reply: keeps waiting
        t.reply_played(e["id"], "other-tab")             # not the page that is talking: keeps waiting
        time.sleep(0.4)
        self.assertEqual(t.state, "talking")
        self.assertTrue(Path(folder).exists())
        t.reply_played(e["id"], "a")
        next_event(q, "ready")
        self.assertFalse(Path(folder).exists(), "reply audio is not kept")
        self.assertEqual(len(t.stream.buf), 0)
        self.assertEqual(t.reply_wav(e["id"], 0)[0], 404)
        self.assertNotIn("mac says", [o[0] for o in t.out])
        self.assertGreater(t.gate.awake_until, time.time())      # the follow-up window is open

    def test_a_piece_is_not_served_before_it_is_finished(self):
        t = make()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        t.reply = {"id": "abcd1234", "dir": d, "n": 2, "done": False}
        self.assertEqual(t.reply_wav("abcd1234", 0)[0], 425)       # not made yet
        Path(d, "0.wav").write_bytes(b"half")
        self.assertEqual(t.reply_wav("abcd1234", 0)[0], 425)       # may still be being written
        Path(d, "1.wav").write_bytes(b"x")
        self.assertEqual(t.reply_wav("abcd1234", 0)[0], 200)       # the next one exists: this one is whole
        self.assertEqual(t.reply_wav("abcd1234", 1)[0], 425)
        t.reply["done"] = True
        self.assertEqual(t.reply_wav("abcd1234", 1)[0], 200)
        Path(d, "1.wav").unlink()
        self.assertEqual(t.reply_wav("abcd1234", 1)[0], 410)       # it will never come

    def test_page_gone_means_the_mac_speaks(self):
        t = make(synth=self.fake_synth)
        t.speak_reply("It worked.")                      # nobody is streaming
        self.assertEqual(t.out, [("mac says", "It worked.")])
        self.assertIsNone(t.reply)

    def test_a_page_that_never_fetches_the_audio_gets_the_mac_voice(self):
        saved = vt.REPLY_FETCH_MAX
        self.addCleanup(setattr, vt, "REPLY_FETCH_MAX", saved)
        vt.REPLY_FETCH_MAX = 0.3
        t, q = self.talking()
        next_event(q, "reply")
        next_event(q, "ready")
        self.assertIn(("mac says", "It worked. Second part."), t.out)

    def test_reopening_the_page_does_not_hide_that_claude_is_thinking(self):
        """Bug 2026-11-27: /api/start set the state to "listening" while the worker was still
        waiting for a reply, so the page looked ready and nothing spoken was taken."""
        gate = threading.Event()
        t = make(stt=lambda c, w: "Open the settings file.", wait_reply=lambda at, sid: gate.wait(5) and None)
        th = threading.Thread(target=t.run, daemon=True)
        th.start()
        self.addCleanup(th.join, 3)
        self.addCleanup(t.close)
        self.addCleanup(gate.set)
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6))
        while next_event(q, "state")["state"] != "thinking":
            pass
        t.start()                                        # the page was reloaded and tapped
        self.assertEqual(t.state, "thinking")
        gate.set()
        next_event(q, "ready")
        self.assertIn(t.state, ("listening", "idle"))

    def test_page_leaving_before_it_fetched_means_the_mac_speaks(self):
        t, q = self.talking()
        next_event(q, "reply")
        t.last_audio = 0                                 # the tab was closed
        next_event(q, "ready")
        self.assertIn(("mac says", "It worked. Second part."), t.out)

    def test_stop_ends_the_talking(self):
        t, q = self.talking()
        next_event(q, "reply")
        t.stop()
        deadline = time.time() + 3
        while t.reply is not None and time.time() < deadline:
            time.sleep(0.02)
        self.assertIsNone(t.reply)
        self.assertEqual(t.state, "stopped")

    def test_reply_over_http(self):
        t, q = self.talking()
        srv = vt.make_server(t, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        e = next_event(q, "reply")

        def req(method, path):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            self.addCleanup(c.close)
            c.request(method, path, body=b"" if method == "POST" else None)
            r = c.getresponse()
            return r.status, r.read(), r.getheader("Content-Type")
        deadline = time.time() + 3
        while req("GET", f"/api/reply/{e['id']}/1.wav")[0] == 425 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(req("GET", f"/api/reply/{e['id']}/0.wav"), (200, b"RIFFIt worked.", "audio/wav"))
        self.assertEqual(req("GET", "/api/reply/../../etc/passwd")[0], 404)
        self.assertEqual(req("POST", f"/api/played?id={e['id']}&c=a")[0], 204)
        next_event(q, "ready")


@unittest.skipUnless(shutil.which("say"), "needs say")
class Synthesize(unittest.TestCase):
    def test_say_writes_one_playable_wav_per_piece(self):
        with tempfile.TemporaryDirectory() as d:
            vt.synthesize(["It worked.", "Second part."], d)
            self.assertEqual(sorted(os.listdir(d)), ["0.wav", "1.wav"])
            with wave.open(os.path.join(d, "0.wav")) as w:
                self.assertGreater(w.getnframes() / w.getframerate(), 0.3)


class Worker(unittest.TestCase):
    def start(self, stt, **kw):
        t = make(stt=stt, **kw)
        th = threading.Thread(target=t.run, daemon=True)
        th.start()
        self.addCleanup(th.join, 3)
        self.addCleanup(t.close)
        return t

    def test_audio_in_event_out(self):
        seen = []

        def stt(cfg, wav):
            with wave.open(wav) as w:
                seen.append((w.getframerate(), w.getnframes() / w.getframerate()))
            return "Open the settings file."
        t = self.start(stt)
        q = t.subscribe()
        self.assertEqual(q.get_nowait(), {"type": "state", "state": "idle", "wake": False, "project": "", "build": vt.BUILD})
        t.feed("a", silence(0.3) + tone(0.6) + silence(0.6))
        self.assertEqual(next_event(q, "heard")["text"], "Open the settings file.")
        rate, secs = seen[0]
        self.assertEqual(rate, 16000)
        self.assertTrue(0.6 <= secs <= 1.2, secs)    # the tone plus pre-roll and the closing quiet

    def test_states_the_page_sees(self):
        t = self.start(lambda c, w: "Open the settings file.")
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6))
        seen = []
        while seen[-1:] != ["ready"]:
            e = q.get(timeout=5)
            seen.append(e.get("verdict") or e.get("state") or e["type"])
        # the second "transcribing" is the page being told WHICH project it is talking to
        self.assertEqual(seen, ["idle", "listening", "transcribing", "transcribing", "send", "thinking", "ready"])
        self.assertEqual(t.out, [("sess-1", "Open the settings file.", None), ("waited", "sess-1")])
        self.assertEqual(next_event(q, "state")["state"], "listening")   # the page is still there: your turn

    def test_deaf_while_claude_thinks_and_speaks(self):
        gate = threading.Event()
        t = self.start(lambda c, w: "Open the settings file.")
        t.wait_reply = lambda at, sid: gate.wait(5)
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6))
        next_event(q, "heard")
        t.feed("a", tone(0.3))                       # still talking when the reply arrives
        time.sleep(0.2)
        gate.set()
        next_event(q, "ready")
        self.assertEqual(len(t.stream.buf), 0)       # thrown away, not sent late
        with self.assertRaises(AssertionError):
            next_event(q, "heard", timeout=0.8)
        self.assertEqual(len(t.out), 1)

    def thinking(self, said):
        """A Talk that has delivered one sentence and is waiting for the reply."""
        self.gate = threading.Event()
        self.addCleanup(self.gate.set)
        words = iter(["Open the settings file."] + said)
        t = self.start(lambda c, w: next(words, ""))
        t.wait_reply = lambda at, sid: self.gate.wait(5) and None
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6))
        self.assertEqual(next_event(q, "heard")["verdict"], "send")
        while next_event(q, "state")["state"] != "thinking":
            pass
        time.sleep(0.15)       # the wait starts by throwing away what was said before it
        return t, q

    def test_while_claude_works_other_talk_is_shown_but_not_sent(self):
        t, q = self.thinking(["Turn the volume up please."])
        t.feed("a", tone(0.6) + silence(0.6))
        e = next_event(q, "heard")
        self.assertEqual((e["verdict"], e["text"]), ("busy", "Turn the volume up please."))
        self.assertEqual(t.state, "thinking")
        self.gate.set()
        next_event(q, "ready")
        self.assertEqual(len(t.out), 1)                  # only the first sentence ever reached Claude

    def test_stop_listening_works_while_claude_works(self):
        vl.turn_file("sess-1").write_text("0")           # as deliver() leaves it
        t, q = self.thinking(["Claude, stop listening."])
        t.feed("a", tone(0.6) + silence(0.6))
        self.assertEqual(next_event(q, "heard")["verdict"], "stop")
        deadline = time.time() + 5
        while t.busy and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual((t.state, t.busy, t.stopped), ("stopped", False, True))
        self.assertFalse(vl.turn_file("sess-1").exists(), "the reply now belongs to the Mac, not the phone")
        self.assertEqual(len(t.out), 1)

    def test_streaming_marks_the_phone_as_the_microphone(self):
        t = self.start(lambda c, w: "")
        vl.TALK_LIVE().unlink(missing_ok=True)
        self.assertFalse(vl.phone_live())
        t.feed("a", silence(0.1))
        self.assertTrue(vl.phone_live())
        vl.TALK_LIVE().unlink()

    def test_nothing_usable_heard_is_no_event(self):
        t = self.start(lambda c, w: "")
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6))
        next_event(q, "state")                       # idle
        with self.assertRaises(AssertionError):
            next_event(q, "heard", timeout=1.0)

    def test_a_failing_transcription_does_not_end_the_worker(self):
        calls = []

        def stt(cfg, wav):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("whisper fell over")
            return "Second try works."
        t = self.start(stt)
        q = t.subscribe()
        t.feed("a", tone(0.6) + silence(0.6) + tone(0.6) + silence(0.6))
        self.assertEqual(next_event(q, "heard")["text"], "Second try works.")

    def test_one_client_at_a_time(self):
        t = self.start(lambda c, w: "")
        self.assertTrue(t.feed("phone-a", silence(0.1)))
        self.assertFalse(t.feed("phone-b", silence(0.1)))
        t.last_audio -= vt.LIVE_SECONDS + 1          # phone-a went quiet
        self.assertTrue(t.feed("phone-b", silence(0.1)))


class Http(unittest.TestCase):
    def setUp(self):
        self.talk = make(True, stt=lambda c, w: "Claude, open the secret settings file.")
        self.srv = vt.make_server(self.talk, 0)
        self.port = self.srv.server_address[1]
        for target in (self.talk.run, self.srv.serve_forever):
            threading.Thread(target=target, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.talk.close)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(), r

    def events(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)
        c.request("GET", "/api/events")
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")

        def nxt(kind):
            while True:
                line = r.fp.readline().decode()
                if not line:
                    raise AssertionError("event stream ended")
                if line.startswith("data: "):
                    e = json.loads(line[6:])
                    if e["type"] == kind:
                        return e
        return nxt

    def test_page_is_served_and_is_self_contained(self):
        status, body, r = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn(b"voiceLoop Talk", body)
        self.assertIn(f"const BUILD = '{vt.BUILD}'".encode(), body)   # an old tab can tell it is old
        self.assertIsNone(re.search(r"https?://|//[a-z0-9.-]+\.[a-z]{2,}/", vt.PAGE), "the page must load nothing from outside")

    def test_upload_then_heard_event(self):
        nxt = self.events()
        self.assertEqual(nxt("state"), {"type": "state", "state": "idle", "wake": True, "project": "", "build": vt.BUILD})
        pcm = tone(0.6) + silence(0.6)
        for i in range(0, len(pcm), 6400):               # 200 ms per upload, like the page
            self.assertEqual(self.req("POST", "/api/audio?c=test1", pcm[i:i + 6400])[0], 204)
        self.assertEqual(nxt("heard"), {"type": "heard", "verdict": "send", "text": "open the secret settings file."})

    def test_status_never_carries_text(self):
        forget_phones()
        nxt = self.events()
        self.req("POST", "/api/audio?c=test1", tone(0.6) + silence(0.6))
        nxt("heard")
        status, body, _ = self.req("GET", "/api/status")
        d = json.loads(body)
        self.assertEqual(sorted(d), ["last_turn_at", "paired", "project", "running", "state"])
        self.assertEqual((d["running"], d["project"], d["paired"]), (True, "myProject", 0))
        self.assertGreater(d["last_turn_at"], 0)
        self.assertNotIn(b"secret", body)

    def test_another_site_cannot_post_audio(self):
        for origin in ("http://evil.example", "null", f"http://localhost:{self.port + 1}"):
            status, _, _ = self.req("POST", "/api/audio?c=x", tone(0.1), {"Origin": origin})
            self.assertEqual(status, 403, origin)
        self.assertEqual(len(self.talk.stream.buf), 0)
        ok = {"Origin": f"http://127.0.0.1:{self.port}"}   # our own page
        self.assertEqual(self.req("POST", "/api/audio?c=x", tone(0.1), ok)[0], 204)

    def test_wrong_host_is_refused(self):                  # DNS rebinding: evil.example -> 127.0.0.1
        for path in ("/", "/api/status", "/api/events"):
            self.assertEqual(self.req("GET", path, headers={"Host": f"evil.example:{self.port}"})[0], 403, path)
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": f"localhost:{self.port}"})[0], 200)

    def test_bad_uploads(self):
        self.assertEqual(self.req("POST", "/api/audio?c=x", b"abc")[0], 400)              # half a sample
        self.assertEqual(self.req("POST", "/api/audio", b"ab")[0], 400)                    # no client id
        self.assertEqual(self.req("POST", "/api/audio?c=a/b", b"ab")[0], 400)
        self.assertEqual(self.req("POST", "/api/audio?c=x", b"\0" * (vt.MAX_BODY + 2))[0], 413)
        self.assertEqual(self.req("POST", "/api/nope", b"")[0], 404)
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)

    def test_second_client_gets_409(self):
        self.assertEqual(self.req("POST", "/api/audio?c=phone-a", silence(0.1))[0], 204)
        self.assertEqual(self.req("POST", "/api/audio?c=phone-b", silence(0.1))[0], 409)

    def test_the_page_tapped_last_takes_over(self):
        self.assertEqual(self.req("POST", "/api/audio?c=forgotten-tab", silence(0.1))[0], 204)
        self.assertEqual(self.req("POST", "/api/audio?c=visible-tab", silence(0.1))[0], 409)
        self.assertEqual(self.req("POST", "/api/start?c=visible-tab", b"")[0], 204)      # the tap
        self.assertEqual(self.req("POST", "/api/audio?c=visible-tab", silence(0.1))[0], 204)
        self.assertEqual(self.req("POST", "/api/audio?c=forgotten-tab", silence(0.1))[0], 409)
        self.assertEqual(self.req("POST", "/api/start?c=../x", b"")[0], 204)             # no id: nothing taken
        self.assertEqual(self.talk.owner, "visible-tab")

    def test_stop_and_start(self):
        nxt = self.events()
        self.assertEqual(self.req("POST", "/api/stop", b"")[0], 204)
        self.assertEqual(nxt("state")["state"], "idle")
        self.assertEqual(nxt("state")["state"], "stopped")
        self.assertEqual(self.req("POST", "/api/start", b"")[0], 204)
        self.assertEqual(nxt("state")["state"], "idle")

    def test_keep_alive_one_connection_many_uploads(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        for _ in range(3):
            c.request("POST", "/api/audio?c=x", silence(0.2))
            r = c.getresponse()
            r.read()
            self.assertEqual((r.status, r.version), (204, 11))
        self.assertFalse(r.will_close)

    def test_only_this_mac(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")


def forget_phones():
    for f in vt.talk_dir().glob("*.json"):
        f.unlink()


class Pairing(unittest.TestCase):
    def setUp(self):
        forget_phones()

    def test_secret_works_once(self):
        secret = vt.new_pairing()
        token = vt.redeem(secret, "iPhone")
        self.assertTrue(vt.paired(token))
        self.assertIsNone(vt.redeem(secret, "iPhone"))            # replayed
        self.assertEqual(len(vt.phones()), 1)

    def test_wrong_secret_does_not_burn_the_real_one(self):
        secret = vt.new_pairing()
        self.assertIsNone(vt.redeem("guess", "x"))
        self.assertIsNone(vt.redeem("", "x"))
        self.assertTrue(vt.redeem(secret, "x"))

    def test_expired_secret(self):
        secret = vt.new_pairing(now=1000.0)
        self.assertIsNone(vt.redeem(secret, "x", now=1000.0 + vt.PAIR_SECONDS + 1))
        self.assertIsNone(vt.redeem(secret, "x", now=1001.0))     # and it is gone for good

    def test_no_pairing_open_means_nobody_gets_in(self):
        self.assertIsNone(vt.redeem("anything", "x"))

    def test_only_hashes_are_stored_and_files_are_private(self):
        secret = vt.new_pairing()
        self.assertNotIn(secret, (vt.talk_dir() / "pair.json").read_text())
        token = vt.redeem(secret, "iPhone")
        stored = (vt.talk_dir() / "phones.json").read_text()
        self.assertNotIn(token, stored)
        self.assertEqual((vt.talk_dir() / "phones.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(vt.talk_dir().stat().st_mode & 0o777, 0o700)

    def _qr_cli(self, cert=True, show=False):
        """Run qr_cli with a fake qrencode and `open`; returns (exit code, what was made)."""
        made = []
        d = Path(tempfile.mkdtemp(prefix="voiceloop-test-qr-"))
        if cert:
            (d / "cert.pem").write_text("x"), (d / "key.pem").write_text("x")
        old = vt.cert_files, vt.qr, vt.subprocess.run, vt.host_name
        vt.cert_files = lambda: (d / "cert.pem", d / "key.pem")
        vt.qr = lambda text, png="": made.append(("png" if png else "terminal", text)) or True
        vt.subprocess.run = lambda cmd, *a, **k: made.append(("ran", cmd[0]))
        vt.host_name = lambda: "My-Mac"
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return vt.qr_cli(show), made
        finally:
            vt.cert_files, vt.qr, vt.subprocess.run, vt.host_name = old

    def test_page_qr_is_only_the_address(self):
        vt.redeem(vt.new_pairing(), "iPhone")
        code, made = self._qr_cli()
        url = f"https://my-mac.local:{vt.load_talk_config()['tls_port']}/"
        self.assertEqual((code, made), (0, [("terminal", url)]))     # no secret, no window

    def test_page_qr_in_a_window(self):
        vt.redeem(vt.new_pairing(), "iPhone")
        code, made = self._qr_cli(show=True)
        self.assertEqual([m[0] for m in made], ["terminal", "png", "ran"])
        self.assertEqual(made[2], ("ran", "open"))

    def test_page_qr_does_not_open_a_pairing(self):
        vt.redeem(vt.new_pairing(), "iPhone")
        before = len(vt.phones())
        self._qr_cli()
        self.assertFalse((vt.talk_dir() / "pair.json").exists() and
                         json.loads((vt.talk_dir() / "pair.json").read_text()))
        self.assertEqual(len(vt.phones()), before)

    def test_page_qr_needs_a_paired_phone_and_a_certificate(self):
        self.assertEqual(self._qr_cli(), (1, []))                    # nobody paired: pair first
        vt.redeem(vt.new_pairing(), "iPhone")
        self.assertEqual(self._qr_cli(cert=False), (1, []))

    def test_tokens_that_are_not_paired(self):
        for t in ("", "nope", None):
            self.assertFalse(vt.paired(t))

    def test_names_and_unpair(self):
        a = vt.redeem(vt.new_pairing(), vt.phone_name("Mozilla/5.0 (iPhone; CPU iPhone OS 19_0 like Mac OS X)"))
        b = vt.redeem(vt.new_pairing(), "iPhone")
        self.assertEqual(sorted(p["name"] for p in vt.phones().values()), ["iPhone", "iPhone 2"])
        self.assertEqual(vt.unpair("iphone 2"), ["iPhone 2"])
        self.assertTrue(vt.paired(a))
        self.assertFalse(vt.paired(b))
        self.assertEqual(vt.unpair("nobody"), [])
        self.assertEqual(vt.unpair("all"), ["iPhone"])
        self.assertFalse(vt.paired(a))
        self.assertEqual(vt.phone_name(""), "phone")


class FromTheNetwork(unittest.TestCase):
    """What a device that is NOT this Mac may do. The test connects from this Mac, so the
    server is told not to trust loopback connections."""

    def setUp(self):
        forget_phones()
        self.talk = make(stt=lambda c, w: "Open the secret settings file.")
        self.srv = vt.make_server(self.talk, 0)
        self.srv.trust_loopback = False
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.talk.close)

    def req(self, method, path, body=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        c.request(method, path, body=body, headers={"Cookie": cookie} if cookie else {})
        r = c.getresponse()
        return r.status, r.read(), r

    def pair(self):
        status, body, r = self.req("GET", "/pair?s=" + vt.new_pairing())
        self.assertEqual(status, 200)
        return r.getheader("Set-Cookie")

    def test_no_token_no_page_no_api(self):
        status, body, _ = self.req("GET", "/")
        self.assertEqual(status, 403)
        self.assertIn(b"not paired", body)
        self.assertNotIn(b"getUserMedia", body)                   # not even the page's code
        self.assertEqual(self.req("GET", "/api/events")[0], 403)
        self.assertEqual(self.req("GET", "/api/reply/abcd1234/0.wav")[0], 403)
        for path in ("/api/audio?c=x", "/api/start?c=x", "/api/stop", "/api/played?id=abcd1234&c=x"):
            self.assertEqual(self.req("POST", path, tone(0.1))[0], 403, path)
        self.assertEqual(len(self.talk.stream.buf), 0)
        self.assertFalse(self.talk.stopped)

    def test_status_is_open_and_counts_phones(self):
        self.assertEqual(json.loads(self.req("GET", "/api/status")[1])["paired"], 0)
        self.pair()
        status, body, _ = self.req("GET", "/api/status")
        self.assertEqual((status, json.loads(body)["paired"]), (200, 1))

    def test_pairing_gives_a_cookie_that_opens_everything(self):
        cookie = self.pair()
        self.assertRegex(cookie, r"^voiceloop_talk=[\w-]{40,}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict$")
        token = cookie.split(";")[0]
        status, body, _ = self.req("GET", "/", cookie=token)
        self.assertEqual(status, 200)
        self.assertIn(b"getUserMedia", body)
        self.assertEqual(self.req("POST", "/api/audio?c=x", tone(0.1), cookie=token)[0], 204)
        self.assertEqual(self.req("POST", "/api/audio?c=x", tone(0.1), cookie=token + "; other=1")[0], 204)

    def test_wrong_or_replayed_secret_and_wrong_token(self):
        secret = vt.new_pairing()
        self.assertEqual(self.req("GET", "/pair?s=wrong")[0], 403)
        self.assertEqual(self.req("GET", "/pair")[0], 403)
        status, _, r = self.req("GET", "/pair?s=" + secret)
        self.assertEqual(status, 200)
        status, body, r2 = self.req("GET", "/pair?s=" + secret)   # someone else scans the same code
        self.assertEqual(status, 403)
        self.assertIsNone(r2.getheader("Set-Cookie"))
        self.assertIn(b"already used", body)
        self.assertEqual(self.req("GET", "/", cookie="voiceloop_talk=made-up")[0], 403)
        self.assertEqual(self.req("GET", "/", cookie="voiceloop_talk=")[0], 403)
        self.assertEqual(self.req("GET", "/", cookie="garbage;;;=")[0], 403)

    def test_unpaired_phone_is_out_at_once(self):
        token = self.pair().split(";")[0]
        self.assertEqual(self.req("GET", "/", cookie=token)[0], 200)
        vt.unpair("all")
        self.assertEqual(self.req("GET", "/", cookie=token)[0], 403)
        self.assertEqual(self.req("POST", "/api/audio?c=x", tone(0.1), cookie=token)[0], 403)

    def test_this_mac_itself_needs_no_pairing(self):
        self.srv.trust_loopback = True
        self.assertEqual(self.req("GET", "/")[0], 200)


def make_cert(folder):
    cert, key = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-keyout", key,
                        "-out", cert, "-subj", "/CN=localhost", "-addext",
                        "subjectAltName=DNS:localhost,DNS:My-Mac.local,IP:127.0.0.1"], capture_output=True)
    return (cert, key) if r.returncode == 0 else None


@unittest.skipUnless(shutil.which("openssl"), "needs openssl to make a test certificate")
class Https(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="voiceloop-test-cert-")
        cls.files = make_cert(cls.dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        if not self.files:
            self.skipTest("openssl could not make a certificate")
        forget_phones()
        self.talk = make()
        self.srv = vt.make_tls_server(self.talk, 0, *self.files, bind="127.0.0.1")
        self.srv.trust_loopback = False
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.addCleanup(self.talk.close)
        self.ctx = ssl.create_default_context(cafile=self.files[0])

    def req(self, method, path, body=None, headers=None, host="localhost"):
        c = http.client.HTTPSConnection(host, self.port, timeout=5, context=self.ctx)
        self.addCleanup(c.close)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(), r

    def test_certificate_names_are_the_only_hosts(self):
        self.assertEqual(vt.cert_info(self.files[0])["names"], ["localhost", "my-mac.local", "127.0.0.1"])
        self.assertEqual(self.srv.hosts, {f"localhost:{self.port}", f"my-mac.local:{self.port}", f"127.0.0.1:{self.port}"})
        self.assertEqual(self.req("GET", "/api/status")[0], 200)
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": f"My-Mac.local:{self.port}"})[0], 200)
        self.assertEqual(self.req("GET", "/api/status", headers={"Host": f"evil.example:{self.port}"})[0], 403)
        self.assertGreater(vt.cert_info(self.files[0])["until"], time.time())
        self.assertEqual(vt.cert_info("/nonexistent.pem"), {"names": [], "until": 0})

    def test_pair_and_talk_over_https(self):
        status, _, r = self.req("GET", "/pair?s=" + vt.new_pairing())
        cookie = r.getheader("Set-Cookie")
        self.assertTrue(cookie.endswith("; SameSite=Strict; Secure"), cookie)
        token = {"Cookie": cookie.split(";")[0]}
        self.assertEqual(self.req("GET", "/", headers=token)[0], 200)
        self.assertEqual(self.req("POST", "/api/audio?c=x", tone(0.1), token)[0], 204)
        self.assertEqual(self.req("GET", "/")[0], 403)

    def test_plain_http_to_the_https_port_gets_nothing(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        with self.assertRaises((http.client.HTTPException, OSError)):
            c.request("GET", "/")
            c.getresponse()
        self.assertEqual(self.req("GET", "/api/status")[0], 200)      # and the server is fine

    def test_a_silent_client_does_not_block_the_others(self):
        """It connects and never starts the TLS handshake. With the handshake inside
        accept(), every other phone would wait behind it."""
        stalled = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(3)]
        self.addCleanup(lambda: [x.close() for x in stalled])
        t0 = time.time()
        self.assertEqual(self.req("GET", "/api/status")[0], 200)
        self.assertLess(time.time() - t0, 2.0)


@unittest.skipUnless(HAVE_AUDIO, "needs say, whisper-cli and the Whisper model")
class RealSpeech(unittest.TestCase):
    def test_spoken_sentence_arrives_as_text(self):
        with tempfile.TemporaryDirectory() as d:
            wav = os.path.join(d, "s.wav")
            subprocess.run(["say", "-v", "Samantha", "-o", wav, "--data-format=LEI16@16000",
                            "Claude, what is the capital of France?"], check=True)
            with wave.open(wav) as w:
                pcm = w.readframes(w.getnframes())
        got = []
        talk = vt.Talk({**vl.DEFAULTS, "silence_seconds": 1.0}, wake=True, find=lambda cfg: PANE,
                       send=lambda t, text, c=None: got.append(text) or True, wait_reply=lambda *a: None)
        srv = vt.make_server(talk, 0)
        for target in (talk.run, srv.serve_forever):
            threading.Thread(target=target, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(talk.close)
        q = talk.subscribe()
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        self.addCleanup(c.close)
        pcm = silence(0.5) + pcm + silence(1.5)
        for i in range(0, len(pcm), 6400):
            c.request("POST", "/api/audio?c=say", pcm[i:i + 6400])
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, 204)
        e = next_event(q, "heard", timeout=60)
        self.assertEqual(e["verdict"], "send")
        self.assertNotIn("claude", e["text"].lower())      # the wake word came off
        self.assertIn("capital of france", e["text"].lower())
        self.assertEqual(got, [e["text"]])                  # and exactly that was delivered


if __name__ == "__main__":
    unittest.main()
