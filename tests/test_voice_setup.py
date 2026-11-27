"""Tests for voice-setup.py (the setup page) — no microphone, no browser, no real settings file."""

import importlib.util
import sys
sys.dont_write_bytecode = True  # never drop __pycache__ next to the scripts under test
import array
import http.client
import io
import json
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEV = os.environ.get("VOICELOOP_SRC") or ((_ROOT / ".dev-source").read_text().strip()
                                           if (_ROOT / ".dev-source").exists() else "")
_DEV_COPY = Path(_DEV).expanduser() / "voice-setup.py" if _DEV else None
SCRIPT = Path(os.environ.get("VOICE_SETUP") or   # a developer's canonical copy if set, else scripts/
              (_DEV_COPY if _DEV_COPY and _DEV_COPY.exists() else _ROOT / "scripts" / "voice-setup.py"))
# NEVER the real Claude Code settings, the real voice config or the real $TMPDIR.
os.environ["CLAUDE_SETTINGS"] = str(Path(tempfile.mkdtemp(prefix="voiceloop-test-settings-")) / "settings.json")
os.environ["CLAUDE_VOICE_DIR"] = tempfile.mkdtemp(prefix="voiceloop-test-")
spec = importlib.util.spec_from_file_location("voice_setup", SCRIPT)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)
vl = vs.vl
vl.log = lambda *a: None
TMP = Path(tempfile.mkdtemp(prefix="voiceloop-test-tmp-"))
vl.tmp_dir = lambda: TMP
CFG = {**vl.DEFAULTS, "silence_seconds": 0.3, "preroll": 0.1}
OTHER_HOOK = {"hooks": [{"type": "command", "command": "/somewhere/my-own-hook"}]}


def tone(secs: float, amp: int = 8000) -> bytes:
    return array.array("h", (int(amp * math.sin(2 * math.pi * 440 * i / 16000))
                             for i in range(int(16000 * secs)))).tobytes()


def silence(secs: float) -> bytes:
    return b"\0" * (int(16000 * secs) * 2)


SPEECH = silence(0.3) + tone(0.6) + silence(0.6)


def make(audio=SPEECH, heard="Open the settings file.", room=None, busy="", **kw):
    """A Setup whose outside world is fake. `s.log` collects what happened; `s.events` what the page got."""
    log, opened = [], []

    class Mic:
        def __init__(self, device=""):
            opened.append(device)
            self.stream = io.BytesIO(audio)

        def close(self):
            log.append("mic closed")
    texts = iter(heard) if isinstance(heard, list) else None
    folder = Path(tempfile.mkdtemp(prefix="voiceloop-test-set-"))
    s = vs.Setup(CFG, settings=folder / "settings.json", mic=Mic,
                 stt=lambda c, w: next(texts, "") if texts else heard,
                 say=lambda text: log.append(("said", text)),
                 room=lambda secs, device: room or {"ok": True, "median": 0.1, "max": 0.2, "threshold": "0.5%"},
                 devices=lambda: (["Desk Mic", "Headset"], "Desk Mic"),
                 busy=lambda: busy, cue=lambda event: log.append(("cue", event)), **kw)
    s.log, s.opened, s.events = log, opened, []
    return s


class Hooks(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="voiceloop-test-hooks-")) / "settings.json"

    def write(self, data):
        self.path.write_text(data if isinstance(data, str) else json.dumps(data))

    def test_adds_only_what_is_missing_and_keeps_everything_else(self):
        self.write({"model": "x", "hooks": {"Stop": [OTHER_HOOK], "PreToolUse": [OTHER_HOOK]}})
        r = vs.hooks_apply(self.path)
        self.assertEqual((r["ok"], r["added"]), (True, ["Stop", "UserPromptSubmit", "Notification"]))
        new = json.loads(self.path.read_text())
        self.assertEqual(new["model"], "x")
        self.assertEqual(new["hooks"]["PreToolUse"], [OTHER_HOOK])
        self.assertEqual(new["hooks"]["Stop"][0], OTHER_HOOK, "somebody else's hook stays first and unchanged")
        self.assertEqual(new["hooks"]["Notification"][0]["matcher"], "permission_prompt|elicitation_dialog")
        self.assertEqual(vs.hooks_missing(new), [])
        backup = self.path.with_name(r["backup"])
        self.assertEqual(json.loads(backup.read_text())["hooks"]["Stop"], [OTHER_HOOK], "the backup is the old file")

    def test_twice_is_once(self):
        self.write({})
        vs.hooks_apply(self.path)
        before = self.path.read_text()
        self.assertEqual(vs.hooks_apply(self.path), {"ok": True, "added": [], "backup": ""})
        self.assertEqual(self.path.read_text(), before)

    def test_no_settings_file_yet(self):
        r = vs.hooks_apply(self.path)
        self.assertEqual((r["ok"], r["backup"]), (True, ""))
        self.assertEqual(vs.hooks_missing(json.loads(self.path.read_text())), [])

    def test_a_file_it_cannot_understand_is_left_alone(self):
        for broken in ("{ not json", "[1, 2]"):
            self.write(broken)
            r = vs.hooks_apply(self.path)
            self.assertFalse(r["ok"])
            self.assertEqual(self.path.read_text(), broken)
            self.assertEqual([p.name for p in self.path.parent.iterdir()], ["settings.json"], "no backup, no temp file")

    def test_one_already_there(self):
        self.write({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/.claude/hooks/voice-speak.py"}]}]}})
        self.assertEqual(vs.hooks_missing(json.loads(self.path.read_text())), ["UserPromptSubmit", "Notification"])
        self.assertEqual(vs.hooks_apply(self.path)["added"], ["UserPromptSubmit", "Notification"])
        self.assertEqual(len(json.loads(self.path.read_text())["hooks"]["Stop"]), 1)

    def test_odd_shapes_do_not_break_it(self):
        self.write({"hooks": {"Stop": "nonsense", "Notification": [None, {"hooks": None}]}})
        self.assertEqual(vs.hooks_missing(json.loads(self.path.read_text())), ["Stop", "UserPromptSubmit", "Notification"])
        self.assertTrue(vs.hooks_apply(self.path)["ok"])


class Ready(unittest.TestCase):
    def test_missing_hooks_are_the_thing_to_fix_and_can_be_added(self):
        s = make()
        hooks = next(i for i in s.check()["items"] if i["key"] == "hooks")
        self.assertEqual((hooks["ok"], hooks["can_apply"], hooks["missing"]),
                         (False, True, ["Stop", "UserPromptSubmit", "Notification"]))
        vs.hooks_apply(s.settings)
        self.assertTrue(next(i for i in s.check()["items"] if i["key"] == "hooks")["ok"])

    def test_a_broken_settings_file_gets_no_button(self):
        s = make()
        s.settings.write_text("{ not json")
        hooks = next(i for i in s.check()["items"] if i["key"] == "hooks")
        self.assertEqual((hooks["ok"], hooks["can_apply"]), (False, False))

    def test_says_who_has_the_microphone(self):
        self.assertEqual(make(busy="listener").check()["mic_taken"], "listener")


class SaySomething(unittest.TestCase):
    def run_try(self, s):
        s.try_it(s.events.append)
        return [e for e in s.events if "level" not in e]

    def test_room_then_a_sentence_then_the_voice_answers(self):
        s = make()
        self.assertEqual(self.run_try(s), [{"step": "room"}, {"step": "speak"}, {"step": "thinking"},
                                           {"heard": "Open the settings file."}, {"step": "speaking"}, {"done": True}])
        self.assertIn(("said", "I heard you. You said: Open the settings file."), s.log)
        levels = [e for e in s.events if "level" in e]
        self.assertTrue(any(e["sound"] for e in levels) and any(not e["sound"] for e in levels), "the bar moved")
        self.assertIn("mic closed", s.log)

    def test_the_rooms_threshold_is_used_but_not_saved_yet(self):
        s = make(room={"ok": True, "median": 0.4, "max": 0.9, "threshold": "1.2%"})
        self.run_try(s)
        self.assertEqual((s.cfg["threshold"], s.pending()), ("1.2%", {"threshold": "1.2%"}))
        self.assertFalse((Path(os.environ["CLAUDE_VOICE_DIR"]) / "config.json").exists(), "nothing is saved without a click")

    def test_pure_silence_points_at_the_microphone_permission(self):
        s = make(audio=silence(1.0), room={"ok": False, "why": "no signal"})
        self.assertEqual(self.run_try(s)[-1], {"error": "silent"})
        self.assertNotIn(("said", "I heard you."), s.log)

    def test_a_room_but_no_speech(self):
        quiet = array.array("h", [40 if i % 2 else -40 for i in range(16000)]).tobytes()   # a faint hum, below the threshold
        s = make(audio=quiet, room={"ok": True, "median": 0.1, "max": 0.2, "threshold": "0.5%"})
        self.assertEqual(self.run_try(s)[-1], {"error": "nothing"})

    def test_sound_without_words(self):
        self.assertEqual(self.run_try(make(heard=""))[-1], {"error": "notext"})

    def test_never_opens_the_mic_while_the_listener_or_a_phone_has_it(self):
        for who in ("listener", "phone"):
            s = make(busy=who)
            self.assertEqual(self.run_try(s), [{"error": who}])
            self.assertEqual(s.opened, [])

    def test_one_test_at_a_time(self):
        s = make()
        s.lock.acquire()
        self.assertEqual(self.run_try(s), [{"error": "busy"}])
        s.lock.release()

    def test_a_crash_still_answers_the_page_and_frees_the_microphone(self):
        def boom(c, w):
            raise RuntimeError("whisper fell over")
        s = make()
        s.stt = boom
        self.assertEqual(self.run_try(s)[-1], {"error": "failed", "detail": "RuntimeError"})
        self.assertFalse(s.lock.locked())


class FineTune(unittest.TestCase):
    def test_wake_training_offers_only_what_it_heard_and_accepts_only_what_it_offered(self):
        s = make(heard=["Claude.", "Pod.", "Pod.", "Claude.", ""])
        s.train_wake(s.events.append)
        rounds = [e for e in s.events if "heard" in e]
        self.assertEqual([(e["heard"], e["ok"]) for e in rounds],
                         [("Claude.", True), ("Pod.", False), ("Pod.", False), ("Claude.", True), ("", False)])
        self.assertEqual(s.events[-1], {"new": ["pod"], "any": True})
        self.assertEqual(s.log.count(("cue", "listening")), 5)
        self.assertEqual(s.accept_wake(["pod", "computer"])["accepted"], ["pod"])
        self.assertEqual(s.pending(), {"wake_words": ["pod"]})

    def test_wake_training_with_a_dead_microphone(self):
        s = make(audio=silence(0.5), heard="")
        s.train_wake(s.events.append)
        self.assertEqual(s.events[-1], {"new": [], "any": False})

    def test_choosing_a_microphone(self):
        s = make()
        self.assertEqual(s.mics(), {"names": ["Desk Mic", "Headset"], "default": "Desk Mic", "current": ""})
        self.assertFalse(s.choose_mic("Not Plugged In")["ok"])
        self.assertTrue(s.choose_mic("Headset")["ok"])
        s.try_it(s.events.append)
        self.assertEqual(s.opened, ["Headset"])
        self.assertTrue(s.choose_mic("")["ok"])                    # back to "follow the Mac"
        self.assertEqual(s.pending()["device"], "")

    def test_settings_are_checked_before_they_are_taken(self):
        s = make()
        self.assertEqual(s.set_option("silence_seconds", "4"), {"ok": True, "value": 4})
        self.assertEqual(s.set_option("wake", "yes"), {"ok": True, "value": True})
        self.assertEqual(s.set_option("wake", False), {"ok": True, "value": False})
        for key, raw in (("silence_seconds", "0"), ("silence_seconds", "soon"), ("idle_minutes", "9999"),
                         ("model", "/tmp/evil.bin"), ("commands", "{}")):
            self.assertFalse(s.set_option(key, raw)["ok"], (key, raw))
        self.assertEqual(s.pending(), {"silence_seconds": 4, "wake": False})

    def test_finish_saves_only_then_and_keeps_a_backup(self):
        cfg = Path(os.environ["CLAUDE_VOICE_DIR"]) / "config.json"
        cfg.write_text(json.dumps({"engine": "kokoro", "listen": {"idle_minutes": 20}}))
        self.addCleanup(lambda: [p.unlink() for p in cfg.parent.glob("config.json*")])
        s = make()
        s.set_option("silence_seconds", "4")
        self.assertEqual(json.loads(cfg.read_text())["listen"], {"idle_minutes": 20})
        r = s.finish(save=True)
        self.assertEqual(r["saved"], {"silence_seconds": 4})
        self.assertEqual(json.loads(cfg.read_text()), {"engine": "kokoro", "listen": {"idle_minutes": 20, "silence_seconds": 4}})
        self.assertEqual(json.loads((cfg.parent / r["backup"]).read_text())["listen"], {"idle_minutes": 20})
        self.assertTrue(s.finished.is_set())

    def test_finish_without_saving(self):
        s = make()
        s.set_option("silence_seconds", "4")
        self.assertEqual(s.finish(save=False)["saved"], {})
        self.assertFalse((Path(os.environ["CLAUDE_VOICE_DIR"]) / "config.json").exists())


class Http(unittest.TestCase):
    def setUp(self):
        self.setup = make()
        self.srv = vs.make_server(self.setup, 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(c.close)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()

    def test_this_computer_and_its_own_page_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        self.assertEqual(self.req("GET", "/", headers={"Host": "evil.example"})[0], 403)
        for path in ("/api/try", "/api/hooks", "/api/finish", "/api/wake"):
            self.assertEqual(self.req("POST", path, {}, {"Origin": "https://example.com"})[0], 403, path)
        self.assertEqual(self.setup.opened, [], "no recording for another website")
        self.assertFalse(self.setup.settings.exists(), "and no settings change")
        self.assertFalse(self.setup.finished.is_set())

    def test_the_page_and_the_check(self):
        status, page = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Say something", page)
        self.assertNotIn(b"getUserMedia", page, "the page never uses the browser's own microphone")
        self.assertIn("items", json.loads(self.req("GET", "/api/check")[1]))

    def test_say_something_arrives_line_by_line(self):
        status, body = self.req("POST", "/api/try")
        events = [json.loads(line) for line in body.decode().splitlines()]
        self.assertEqual(status, 200)
        self.assertEqual(events[0], {"step": "room"})
        self.assertEqual(events[-1], {"done": True})
        self.assertIn({"heard": "Open the settings file."}, events)

    def test_the_hooks_button_changes_only_the_settings_file_it_was_given(self):
        self.assertTrue(str(self.setup.settings).startswith(tempfile.gettempdir()) or "voiceloop-test" in str(self.setup.settings))
        status, body = self.req("POST", "/api/hooks", {})
        self.assertEqual((status, json.loads(body)["added"]), (200, ["Stop", "UserPromptSubmit", "Notification"]))
        self.assertEqual(vs.hooks_missing(json.loads(self.setup.settings.read_text())), [])

    def test_bad_input_is_refused_not_guessed(self):
        self.assertEqual(self.req("POST", "/api/option", {"key": "model", "value": "/tmp/x"})[0], 400)
        self.assertEqual(self.req("POST", "/api/mic", {"name": "Not Plugged In"})[0], 400)
        self.assertEqual(self.req("POST", "/api/option", ["silence_seconds", 4])[0], 400)
        self.assertEqual(self.req("GET", "/api/nothing")[0], 404)

    def test_finish_ends_the_run(self):
        self.assertEqual(json.loads(self.req("POST", "/api/finish", {"save": True})[1])["ok"], True)
        self.assertTrue(self.setup.finished.is_set())


if __name__ == "__main__":
    unittest.main()
