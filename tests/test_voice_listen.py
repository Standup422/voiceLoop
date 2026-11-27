"""Tests for voice-listen.py — no microphone needed.
Audio is made with `say` + `sox`, so the pipeline is tested on real speech."""

import importlib.util
import sys
sys.dont_write_bytecode = True  # never drop __pycache__ next to the scripts under test (~/.claude/hooks)
import io
import json
import os
import wave
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DEV = os.environ.get("VOICELOOP_SRC") or ((_ROOT / ".dev-source").read_text().strip()
                                           if (_ROOT / ".dev-source").exists() else "")
_DEV_COPY = Path(_DEV).expanduser() / "voice-listen.py" if _DEV else None
SCRIPT = Path(os.environ.get("VOICE_LISTEN") or   # a developer's canonical copy if set, else scripts/
              (_DEV_COPY if _DEV_COPY and _DEV_COPY.exists() else _ROOT / "scripts" / "voice-listen.py"))
spec = importlib.util.spec_from_file_location("voice_listen", SCRIPT)
vl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vl)
# Never read the tester's own config.json / words.txt: same results on every Mac.
os.environ["CLAUDE_VOICE_DIR"] = tempfile.mkdtemp(prefix="voiceloop-test-")
CFG = dict(vl.DEFAULTS)   # never the tester's own config.json: same results on every Mac
HAVE_AUDIO = all(shutil.which(x) for x in ("say", "sox", "whisper-cli")) and Path(CFG["model"]).exists()


class Clean(unittest.TestCase):
    def c(self, s):
        return vl.clean(s, CFG)

    def test_hallucinations_dropped(self):
        for junk in ["", " you", "Thanks for watching!", "[BLANK_AUDIO]", "(music)", " . ", "Subscribe."]:
            self.assertEqual(self.c(junk), "", junk)

    def test_real_sentences_kept(self):
        self.assertEqual(self.c(" Thank you."), "Thank you.")  # bare "thank you" is real speech, not a Whisper invention
        self.assertEqual(self.c("Thanks for watching the build."), "Thanks for watching the build.")

    def test_never_a_command_even_after_the_wake_word(self):
        # review 2026-11-25: the wake word came off AFTER clean(), so a "/" survived
        for said, sent in [("Claude, /clear", "clear"), ("Hey Claude, !rm -rf x", "rm -rf x"),
                           ("Claude, # remember this", "remember this")]:
            self.assertEqual(vl.Gate(True, 30).feed(self.c(said), 0), ("send", sent), said)
        self.assertEqual(self.c("! /clear the screen"), "clear the screen")

    def test_never_a_command(self):
        self.assertEqual(self.c("!rm -rf stuff"), "rm -rf stuff")
        self.assertEqual(self.c("/clear the screen"), "clear the screen")
        self.assertEqual(self.c("# heading"), "heading")

    def test_replacements_and_control_chars(self):
        cfg = {**CFG, "replacements": {"post gress": "Postgres"}}
        self.assertEqual(vl.clean("check the post gress script\nnow", cfg), "check the Postgres script now")

    def test_thinking_pause_is_incomplete(self):
        for t in ["and", "And.", "I want to check the logs and", "So, um", "because"]:
            self.assertTrue(vl.is_incomplete(t), t)
        for t in ["What mic are we using?", "Yes.", "Check the logs and the config."]:
            self.assertFalse(vl.is_incomplete(t), t)

    def test_real_sentence_ends_are_not_pauses(self):
        # 2026-11-25 15:51: "…Can we work on that?" was held as unfinished and never sent
        for t in ["I would like that wizard first. Can we work on that?", "Yes, do that.",
                  "I like that.", "Is it and?", "Do it now!", "Should we fix the one for?"]:
            self.assertFalse(vl.is_incomplete(t), t)
        for t in ["I think that", "Check the logs and.", "Move it to", "Because of the"]:
            self.assertTrue(vl.is_incomplete(t), t)

    def test_switch_command_parsing(self):
        for said, want in [("Switch to voice loop.", "voice loop"),
                           ("Okay, switch over to the Acme CRM project", "Acme CRM"),
                           ("switch to project home lab", "home lab")]:
            m = vl.SWITCH.match(said)
            self.assertTrue(m, said)
            self.assertEqual(m.group(1), want)
        self.assertIsNone(vl.SWITCH.match("Should we switch to the other branch first?"))
        self.assertTrue(vl.LIST.match("Which projects are open?"))
        self.assertTrue(vl.HELP.match("Voice help."))

    def test_project_matching(self):
        panes = [{"project": "voiceLoop"}, {"project": "acme-crm"}, {"project": "webShop"}]
        self.assertEqual(vl.match_project("voice loop", panes)["project"], "voiceLoop")
        self.assertEqual(vl.match_project("Acme CRM", panes)["project"], "acme-crm")
        self.assertEqual(vl.match_project("web shop", panes)["project"], "webShop")
        self.assertIsNone(vl.match_project("the other branch", panes))  # falls through to Claude

    def test_wake_word(self):
        for said, rest in [("Claude, what time is it?", "what time is it?"),
                           ("Hey Claude, Switch to Voice Loop", "Switch to Voice Loop"),
                           ("OK Claude check the logs", "check the logs"),
                           ("Okay, Claude, check the logs", "check the logs"),
                           ("Claude. What time is this?", "What time is this?"),
                           ("Claude?", "")]:
            self.assertEqual(vl.strip_wake(said), (rest, True), said)
        # a real voice, from a log: Whisper wrote "Cloud" as a vocative
        for said, rest in [("Cloud, do you see me?", "do you see me?"), ("Cloud.", ""),
                           ("Hey cloud, switch to Acme", "switch to Acme"), ("Cloud?", "")]:
            self.assertEqual(vl.strip_wake(said), (rest, True), said)
        for said in ["Cloud sync is broken", "cloud storage is full.", "I asked Claude yesterday",
                     "Claudette is here", "what time is it", "Cloudflare is down."]:
            self.assertEqual(vl.strip_wake(said), (said, False), said)

    def test_has_claude_parser(self):
        with_claude = " 4936 /usr/bin/login\n 4937 -zsh\n 5400 claude\n 5498 /x/helper-darwin-arm64\n"
        bare_shell = " 4936 /usr/bin/login\n 4937 -zsh\n"
        self.assertTrue(vl._has_claude(with_claude))
        self.assertFalse(vl._has_claude(bare_shell))
        self.assertFalse(vl._has_claude(" 1 /usr/local/bin/claude-doctor.sh\n"))

    @unittest.skipUnless(os.environ.get("ITERM_SESSION_ID"), "needs to run inside iTerm")
    def test_claude_in_pane_live(self):
        # run from Claude Code's own shell: this pane IS running claude
        self.assertTrue(vl.claude_in_pane(os.environ["ITERM_SESSION_ID"]))
        self.assertFalse(vl.claude_in_pane("w9t9p9:00000000-0000-0000-0000-000000000000"))

    def project(self, data):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / ".claude").mkdir()
        if data is not None:
            (Path(d.name) / ".claude" / "voice.json").write_text(
                data if isinstance(data, str) else json.dumps(data))
        return d.name

    def test_project_vocabulary_merges(self):
        cwd = self.project({"vocabulary": ["Zephyrine", "billbook", "Claude"],
                            "replacements": {"cube control": "kubectl", "claude code": "CLAUDE CODE"}})
        e = vl.effective(CFG, cwd)
        self.assertTrue(e["prompt"].startswith("Zephyrine, billbook, Claude"))       # project first
        self.assertEqual(e["prompt"].split(", ").count("Claude"), 1)                  # no duplicates
        self.assertEqual(e["replacements"]["cube control"], "kubectl")
        self.assertEqual(e["replacements"]["claude code"], "CLAUDE CODE")             # project wins
        self.assertEqual(vl.clean("run cube control now", e), "run kubectl now")

    def test_project_vocabulary_missing_or_broken(self):
        for data in (None, "{not json", '["a list"]', {"vocabulary": "not a list"}):
            e = vl.effective(CFG, self.project(data))
            self.assertEqual(e["prompt"], ", ".join(t.strip() for t in CFG["prompt"].split(",")))
        self.assertEqual(vl.effective(CFG, "")["replacements"], CFG["replacements"])

    def test_prompt_is_capped(self):
        e = vl.effective(CFG, self.project({"vocabulary": [f"term{i:03d}" for i in range(300)]}))
        self.assertLessEqual(len(e["prompt"]), vl.PROMPT_MAX)
        self.assertTrue(e["prompt"].startswith("term000"))

    def test_user_sounds_merge_with_defaults(self):
        """A user "sounds" list must not hide cues added later (2026-11-25: 'listening')."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        Path(d.name, "config.json").write_text(json.dumps({"listen": {"sounds": {"accepted": "Purr", "ready": "x.wav"}}}))
        old = os.environ.get("CLAUDE_VOICE_DIR")
        os.environ["CLAUDE_VOICE_DIR"] = d.name
        try:
            c = vl.load_config()
        finally:
            os.environ.pop("CLAUDE_VOICE_DIR") if old is None else os.environ.__setitem__("CLAUDE_VOICE_DIR", old)
        self.assertEqual(c["sounds"]["ready"], "x.wav")            # user value kept
        self.assertEqual(c["sounds"]["listening"], vl.DEFAULTS["sounds"]["listening"])  # new default present

    def test_cues_never_fail(self):
        quiet = {"sounds": {"accepted": "Pop", "ready": "", "stopped": "NoSuchSound"}, "sound_volume": 0}
        for ev in ("accepted", "ready", "stopped", "unknown"):
            vl.cue(quiet, ev, block=True)   # off / missing / unknown: silently nothing
        self.assertIn(CFG["sounds"]["accepted"], ("Purr", "Pop"))

    def gate(self, wake=True):
        return vl.Gate(wake, 30, stop=lambda t: vl.is_stop(t, CFG))

    def test_gate_regression_2026_11_25(self):
        """The real log: unwoken chatter ending in 'that', then 'Claude, yes, ...'.
        Old order: chatter held, glued in front, wake check failed, both lost."""
        g = self.gate()
        t = 1000.0   # well past any follow-up window
        self.assertEqual(g.feed("okay so share it on the wiki on that", t)[0], "ignore")
        v, text = g.feed("Claude, yes, we're going to hold on the desktop app.", t + 3)
        self.assertEqual(v, "send")
        self.assertEqual(text, "yes, we're going to hold on the desktop app.")

    def test_gate_wake_and_window(self):
        g = self.gate()
        self.assertEqual(g.feed("what time is it", 100)[0], "ignore")
        self.assertEqual(g.feed("Claude?", 101), ("yes", ""))
        self.assertEqual(g.feed("what time is it", 110), ("send", "what time is it"))   # window
        g.replied(200)
        self.assertEqual(g.feed("the date and", 220)[0], "hold")                          # unfinished
        # 40 s later: the held text expired (>30 s), the window closed (230), no wake word
        self.assertEqual(g.feed("also tomorrow", 260)[0], "ignore")

    def test_gate_hold_continues_past_window(self):
        g = self.gate()
        g.replied(0)
        self.assertEqual(g.feed("check the logs and", 25)[0], "hold")      # inside window
        v, text = g.feed("the config", 40)                                 # window over, but continuing
        self.assertEqual((v, text), ("send", "check the logs and the config"))

    def test_gate_stop_always_and_plain_mode(self):
        g = self.gate()
        self.assertEqual(g.feed("stop listening", 999)[0], "send")          # stop works without the name
        p = self.gate(wake=False)
        self.assertEqual(p.feed("anything at all", 999), ("send", "anything at all"))

    def test_stop_phrase(self):
        self.assertTrue(vl.is_stop("Stop listening.", CFG))
        self.assertTrue(vl.is_stop("Okay, stop listening!", CFG))
        self.assertFalse(vl.is_stop("Don't stop listening to the logs yet", CFG))


class MicDevice(unittest.TestCase):
    """Choosing the mic ("device" in config): sox command, unplugged fallback, setup parsing."""

    PROFILER = """Audio:

    Devices:

        Studio Display:
          Output Channels: 2
          Transport: DisplayPort

        USB Headset:
          Default Input Device: Yes
          Input Channels: 1
          Transport: USB

        USB Headset:
          Default Output Device: Yes
          Output Channels: 2
          Transport: USB

        Webcam Mic:
          Input Channels: 2
          Transport: USB

        Built-in Speakers:
          Output Channels: 2
          Transport: Built-in
"""

    def test_default_device_uses_rec(self):
        cmd = vl.rec_cmd("")
        self.assertEqual(cmd[:2], ["rec", "-q"])
        self.assertNotIn("coreaudio", cmd)
        self.assertEqual(cmd[-1], "-")

    def test_named_device_uses_coreaudio(self):
        cmd = vl.rec_cmd("Webcam Mic", "trim", "0", "5")
        self.assertEqual(cmd[:5], ["sox", "-q", "-t", "coreaudio", "Webcam Mic"])
        self.assertEqual(cmd[-3:], ["trim", "0", "5"])
        self.assertIn("16000", cmd)   # same format as the default path

    def test_device_missing_only_when_sox_failed_by_itself(self):
        self.assertTrue(vl.device_missing("Webcam Mic", "fail", 2))       # can't open device
        self.assertFalse(vl.device_missing("Webcam Mic", "fail", -15))    # we stopped it
        self.assertFalse(vl.device_missing("Webcam Mic", "ok", 2))        # got speech: not missing
        self.assertFalse(vl.device_missing("Webcam Mic", "timeout", -15))
        self.assertFalse(vl.device_missing("", "fail", 2))                # default: nothing to fall back to

    def test_parse_inputs_and_default(self):
        names, default = vl.parse_input_devices(self.PROFILER)
        self.assertEqual(names, ["USB Headset", "Webcam Mic"])   # outputs left out, no duplicate
        self.assertEqual(default, "USB Headset")
        self.assertEqual(vl.parse_input_devices(""), ([], ""))

    def test_pick_device(self):
        names = ["USB Headset", "Webcam Mic"]
        self.assertEqual(vl.pick_device("", names, "Webcam Mic"), "Webcam Mic")   # Enter keeps
        self.assertEqual(vl.pick_device("2", names, ""), "Webcam Mic")
        self.assertEqual(vl.pick_device(" 0 ", names, "Webcam Mic"), "")          # follow default
        self.assertIsNone(vl.pick_device("3", names, ""))
        self.assertIsNone(vl.pick_device("webcam", names, ""))

    def test_unplugged_mic_falls_back_to_default(self):
        """A chosen mic that can't be opened must not end listening: record() retries
        once on the default input."""
        calls = []

        class Proc:
            def __init__(self, cmd, **kw):
                calls.append(cmd)
                self.stdout = io.BytesIO(b"")          # sox gave nothing
                self.returncode = 2 if "coreaudio" in cmd else 0

            def poll(self):
                return self.returncode

            def terminate(self):
                pass

            def wait(self):
                return self.returncode

        warned, logged = [], []
        with unittest.mock.patch.object(vl.subprocess, "Popen", Proc), \
             unittest.mock.patch.object(vl, "speaking", lambda: False), \
             unittest.mock.patch.object(vl, "log", logged.append), \
             unittest.mock.patch.object(vl, "warn_missing_mic", warned.append):
            r = vl.record({**CFG, "device": "Webcam Mic"}, os.path.join(tempfile.gettempdir(), "x.wav"))
        self.assertEqual(r, "fail")                    # the default gave nothing either
        self.assertEqual(warned, ["Webcam Mic"])       # the user is told (spoken, throttled)
        self.assertEqual(len(logged), 1)
        self.assertEqual(len(calls), 2)                # tried the chosen mic, then the default once
        self.assertIn("coreaudio", calls[0])
        self.assertEqual(calls[1][0], "rec")

    def test_missing_mic_notice_is_throttled(self):
        """App mode starts a new process per utterance: the notice must not repeat each time."""
        self.assertTrue(vl.should_warn(0.0, 1000.0))                 # never warned
        self.assertFalse(vl.should_warn(1000.0, 1000.0 + 30))        # warned 30 s ago
        self.assertTrue(vl.should_warn(1000.0, 1000.0 + vl.MIC_WARN_EVERY))
        said = []
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        with unittest.mock.patch.object(vl, "tmp_dir", lambda: Path(d.name)), \
             unittest.mock.patch.object(vl, "say_local", said.append):
            vl.warn_missing_mic("Webcam Mic")
            vl.warn_missing_mic("Webcam Mic")
        self.assertEqual(len(said), 1)
        self.assertIn("Webcam Mic", said[0])

    def test_device_setting_loads(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        Path(d.name, "config.json").write_text(json.dumps({"listen": {"device": "Webcam Mic"}}))
        old = os.environ.get("CLAUDE_VOICE_DIR")
        os.environ["CLAUDE_VOICE_DIR"] = d.name
        try:
            c = vl.load_config()
        finally:
            os.environ.pop("CLAUDE_VOICE_DIR") if old is None else os.environ.__setitem__("CLAUDE_VOICE_DIR", old)
        self.assertEqual(c["device"], "Webcam Mic")
        self.assertEqual(vl.DEFAULTS["device"], "")    # out of the box: the macOS default


class SpokenCommands(unittest.TestCase):
    """Deliberate slash commands: trigger word + allowlist (+ spoken yes for /clear)."""
    ALLOWED = {"clear": {"confirm": True, "reply": False}, "compact": {"reply": False},
               "wrap-session": {}, "session-digest": {}, "voice-loop": {}}

    def test_trigger_phrases(self):
        for said, name in [("command clear", "clear"), ("Command, compact.", "compact"),
                           ("run command wrap session", "wrap-session"),
                           ("slash session digest", "session-digest"),
                           ("Please run the command voice loop.", "voice-loop"),
                           ("command: wrap-session", "wrap-session")]:
            self.assertEqual((vl.parse_command(said, self.ALLOWED) or [None])[0], name, said)

    def test_only_the_allowlist_runs(self):
        for said in ["command delete everything", "slash model opus", "the command line is broken",
                     "clear the screen", "I ran the compact command yesterday", "command"]:
            self.assertIsNone(vl.parse_command(said, self.ALLOWED), said)
        self.assertIsNone(vl.parse_command("command clear", {}))

    def test_options_come_along(self):
        self.assertEqual(vl.parse_command("command clear", self.ALLOWED),
                         ("clear", {"confirm": True, "reply": False}))

    def test_public_default_is_builtins_only(self):
        self.assertEqual(sorted(vl.DEFAULTS["commands"]), ["clear", "compact"])
        self.assertTrue(vl.DEFAULTS["commands"]["clear"]["confirm"])

    def test_wake_word_then_command(self):
        verdict, text = vl.Gate(True, 30).feed("Hey Claude, command clear.", 0)
        self.assertEqual(verdict, "send")
        self.assertEqual(vl.parse_command(text, self.ALLOWED)[0], "clear")

    def _confirm(self, status, answer):
        saved = vl.log
        vl.log = lambda *a: None
        self.addCleanup(setattr, vl, "log", saved)
        cfg = {**CFG, "confirm_seconds": 1}
        return vl.confirm_by_voice(cfg, "/nonexistent.wav", rec=lambda c, w, deadline=0: status,
                                   stt=lambda c, w: answer)

    def test_confirmation(self):
        self.assertTrue(self._confirm("ok", "Yes."))
        self.assertTrue(self._confirm("ok", "Yeah, do it."))
        self.assertTrue(self._confirm("ok", "Claude, yes"))
        self.assertFalse(self._confirm("ok", "No."))
        self.assertFalse(self._confirm("ok", "Wait, what?"))
        self.assertFalse(self._confirm("ok", ""))
        self.assertFalse(self._confirm("timeout", "yes"))   # silence is never a yes
        self.assertFalse(self._confirm("spoke", "yes"))


class SetupWizard(unittest.TestCase):
    """voice setup: wake-word training + settings input (the parts without a mic)."""

    def test_learns_this_macs_spellings(self):
        heard = ["Bot.", "Claude.", "Pod.", "Bot.", "Vlog,"]      # real mishearings of a lone "Claude"
        self.assertEqual(vl.wake_variants(heard), ["bot", "pod", "vlog"])   # most frequent first

    def test_never_learns_everyday_words_or_sentences(self):
        self.assertEqual(vl.wake_variants(["Yes.", "Okay.", "", "Thank you.", "Hello?",
                                           "Pass the salt.", "Cloud.", "Claude?"]), [])

    def test_learned_words_wake_only_when_punctuated(self):
        extra = ("bot", "pod")
        self.assertEqual(vl.strip_wake("Pod, run the tests.", extra), ("run the tests.", True))
        self.assertEqual(vl.strip_wake("Bot.", extra), ("", True))
        self.assertEqual(vl.strip_wake("Hey pod, hello", extra), ("hello", True))
        self.assertFalse(vl.strip_wake("Pod racing is fun.", extra)[1])
        self.assertFalse(vl.strip_wake("Pod, run the tests.")[1])        # not learned: no wake

    def test_gate_uses_learned_words(self):
        g = vl.Gate(True, 30, extra_wake=("pod",))
        self.assertEqual(g.feed("Pod.", 0), ("yes", ""))
        self.assertEqual(vl.Gate(True, 30, extra_wake=("pod",)).feed("Pod, what's next?", 0),
                         ("send", "what's next?"))

    def test_setup_refuses_without_a_keyboard(self):
        # found 2026-11-25 on a second Mac: piped, it took every default and recorded from the mic
        r = subprocess.run([sys.executable, str(SCRIPT), "--setup"], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 2)
        self.assertIn("interactive", r.stderr)

    def test_parse_setting(self):
        P = vl.parse_setting
        self.assertEqual(P("", 2.5, "seconds"), 2.5)
        self.assertEqual(P("3", 2.5, "seconds"), 3)
        self.assertEqual(P("1.5", 2.5, "seconds"), 1.5)
        self.assertEqual(P("y", False, "yesno"), True)
        self.assertEqual(P("no", True, "yesno"), False)
        self.assertEqual(P("0.9", "0.5%", "percent"), "0.9%")
        for bad, kind in [("0.1", "seconds"), ("abc", "minutes"), ("maybe", "yesno"), ("50", "percent")]:
            with self.assertRaises(ValueError, msg=bad):
                P(bad, 1, kind)

    def test_save_listen_merges_and_backs_up(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        saved = vl.voice_dir
        self.addCleanup(setattr, vl, "voice_dir", saved)
        vl.voice_dir = lambda: Path(d.name)
        (Path(d.name) / "config.json").write_text(json.dumps({"engine": "kokoro", "listen": {"threshold": "0.5%"}}))
        b = vl.save_listen({"wake_words": ["pod"], "silence_seconds": 2})
        cfg = json.loads((Path(d.name) / "config.json").read_text())
        self.assertEqual(cfg["engine"], "kokoro")                      # untouched
        self.assertEqual(cfg["listen"], {"threshold": "0.5%", "wake_words": ["pod"], "silence_seconds": 2})
        self.assertIn('"0.5%"', b.read_text())                         # backup = the old file


class WordList(unittest.TestCase):
    """~/.claude/voice/words.txt + `voice words`."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        saved = vl.voice_dir
        self.addCleanup(setattr, vl, "voice_dir", saved)
        vl.voice_dir = lambda: self.dir

    def test_parse(self):
        w = vl.parse_words("# comment\nZephyrine\n\ncube control = kubectl  # fix\n  billbook  \n")
        self.assertEqual(w["vocabulary"], ["Zephyrine", "kubectl", "billbook"])
        self.assertEqual(w["replacements"], {"cube control": "kubectl"})

    def test_merge_order_and_precedence(self):
        (self.dir / "words.txt").write_text("Zephyrine\npost gress = Postgres\n")
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd)
        (Path(cwd) / ".claude").mkdir()
        (Path(cwd) / ".claude" / "voice.json").write_text(json.dumps(
            {"vocabulary": ["Acme"], "replacements": {"post gress": "POSTGRES"}}))
        e = vl.effective({**CFG, "prompt": "Claude", "replacements": {"post gress": "pg"}}, cwd)
        self.assertEqual(e["prompt"], "Acme, Zephyrine, Postgres, Claude")       # project, list, config
        self.assertEqual(e["replacements"]["post gress"], "POSTGRES")             # project wins
        e = vl.effective({**CFG, "prompt": "Claude", "replacements": {"post gress": "pg"}}, "")
        self.assertEqual(e["replacements"]["post gress"], "Postgres")             # list beats config

    def test_cli_add_fix_remove(self):
        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            self.assertEqual(vl.words_cli(["add", "Zephyrine,", "billbook"]), 0)
            vl.words_cli(["add", "zephyrine"])                                   # no duplicate
            vl.words_cli(["fix", "cube", "control", "=", "kubectl"])
            vl.words_cli(["remove", "billbook"])
        w = vl.load_words()
        self.assertEqual(w["vocabulary"], ["Zephyrine", "kubectl"])
        self.assertEqual(w["replacements"], {"cube control": "kubectl"})
        self.assertTrue((self.dir / "words.txt").read_text().startswith("# voiceLoop word list"))
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(vl.words_cli(["bogus"]), 2)


class SuggestThreshold(unittest.TestCase):
    """Room calibration (install.sh): RMS levels per 30 ms frame, 0..1."""

    def test_quiet_laptop_mic(self):
        # measured 2026-11-25: median 0.43 %, max 0.55 %; recordings never ended at 0.5 %
        levels = [0.0043] * 60 + [0.0050] * 5 + [0.0055] * 2
        r = vl.suggest_threshold(levels)
        self.assertTrue(r["ok"])
        self.assertEqual(r["threshold"], "1.1%")   # that room's peak later rose to 0.70 %

    def test_quiet_headset_keeps_the_default(self):
        r = vl.suggest_threshold([0.0013] * 66)       # RIG WX1 noise floor 0.13 %
        self.assertEqual(r["threshold"], "0.5%")

    def test_one_loud_click_does_not_drive_it(self):
        r = vl.suggest_threshold([0.001] * 99 + [0.2])   # a single 20 % click
        self.assertEqual(r["threshold"], "0.5%")

    def test_digital_silence_is_not_a_room(self):
        self.assertFalse(vl.suggest_threshold([0.0] * 66)["ok"])
        self.assertFalse(vl.suggest_threshold([])["ok"])


class WaitForReply(unittest.TestCase):
    """2026-11-25 bug: after a send, the listener waited up to reply_timeout (600 s) for a
    spoken reply and ignored "switch to …" requests (CLI/control file) the whole time."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        saved = (vl.tmp_dir, vl.speech_on)
        self.addCleanup(lambda: (setattr(vl, "tmp_dir", saved[0]), setattr(vl, "speech_on", saved[1])))
        vl.tmp_dir = lambda: self.tmp
        vl.speech_on = lambda sid="": True

    def test_pending_switch_ends_the_wait(self):
        vl.CONTROL().write_text("switch voiceLoop")
        t0 = time.time()
        vl.wait_for_reply({"reply_timeout": 5, "quiet_seconds": 0}, time.time(), "sid")
        self.assertLess(time.time() - t0, 1.0)
        self.assertTrue(vl.CONTROL().exists(), "the loop must still find the request")

    def test_no_request_still_waits(self):
        t0 = time.time()
        vl.wait_for_reply({"reply_timeout": 0.6, "quiet_seconds": 0}, time.time(), "sid")
        self.assertGreaterEqual(time.time() - t0, 0.5)

    # Known limit until 2026-11-25: one shared claude-voice.last.txt, so ANOTHER voiced
    # session's reply ended this listener's wait and opened the mic while ours was coming.
    def test_other_sessions_reply_does_not_end_the_wait(self):
        sent = time.time() - 5
        vl.LAST_SPOKEN().write_text("another session's reply")
        vl.spoke_file("other-session").write_text("")
        t0 = time.time()
        vl.wait_for_reply({"reply_timeout": 0.6, "cooldown": 0}, sent, "my-session")
        self.assertGreaterEqual(time.time() - t0, 0.5)

    def test_own_reply_ends_the_wait(self):
        sent = time.time() - 5
        vl.spoke_file("my-session").write_text("")
        t0 = time.time()
        vl.wait_for_reply({"reply_timeout": 5, "cooldown": 0}, sent, "my-session")
        self.assertLess(time.time() - t0, 1.0)

    def test_without_a_session_id_the_shared_file_still_works(self):
        sent = time.time() - 5
        vl.LAST_SPOKEN().write_text("reply")
        t0 = time.time()
        vl.wait_for_reply({"reply_timeout": 5, "cooldown": 0}, sent, "")
        self.assertLess(time.time() - t0, 1.0)


class OnceCase(unittest.TestCase):
    """Shared setup for listen_once tests: a temp $TMPDIR, no sounds, a fake recorder."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.tmp = Path(d.name)
        names = ("tmp_dir", "cue", "say_local", "wait_quiet", "listener_running")
        saved = {n: getattr(vl, n) for n in names}
        self.addCleanup(lambda: [setattr(vl, n, f) for n, f in saved.items()])
        vl.tmp_dir = lambda: self.tmp
        vl.cue = lambda *a, **k: None
        vl.say_local = lambda *a, **k: None
        vl.wait_quiet = lambda cfg: None
        vl.listener_running = lambda: False
        self.cfg = dict(vl.DEFAULTS)

    def run_once(self, steps, wake=False, timeout=5, sid=""):
        it = iter(steps)
        self.current = ""

        def rec(cfg, wav, deadline=0.0):
            try:
                status, self.current = next(it)
            except StopIteration:
                return "timeout"
            return status

        return vl.listen_once(self.cfg, timeout, wake=wake, rec=rec, stt=lambda c, w: self.current,
                              claude_sid=sid)


class ListenOnce(OnceCase):
    """App mode (desktop app Code tab): listen_once with a fake recorder + transcriber.
    Each script step is (record_status, transcript)."""

    def test_returns_first_usable_utterance(self):
        r = self.run_once([("ok", ""), ("ok", "What time is it?")])
        self.assertEqual(r, {"status": "heard", "heard": "What time is it?"})

    def test_own_speech_is_discarded_not_returned(self):
        r = self.run_once([("spoke", "Emma reading the reply"), ("ok", "Thanks, next step.")])
        self.assertEqual(r["heard"], "Thanks, next step.")

    def test_wake_mode_ignores_chatter_then_takes_addressed_sentence(self):
        r = self.run_once([("ok", "Pass the salt."), ("ok", "Claude, run the tests.")], wake=True)
        self.assertEqual(r, {"status": "heard", "heard": "run the tests."})

    def test_wake_mode_follow_up_window_after_a_spoken_reply(self):
        vl.LAST_SPOKEN().write_text("reply")          # a reply was just spoken
        r = self.run_once([("ok", "Yes, go ahead.")], wake=True)
        self.assertEqual(r["heard"], "Yes, go ahead.")

    def test_follow_up_window_only_for_own_sessions_reply(self):
        vl.LAST_SPOKEN().write_text("another session's reply")
        vl.spoke_file("other-session").write_text("")
        r = self.run_once([("ok", "Yes, go ahead.")], wake=True, sid="my-session")
        self.assertEqual(r["status"], "timeout")     # not addressed, and no reply of OURS just now
        vl.spoke_file("my-session").write_text("")
        r = self.run_once([("ok", "Yes, go ahead.")], wake=True, sid="my-session")
        self.assertEqual(r["heard"], "Yes, go ahead.")

    def test_unfinished_sentence_is_joined(self):
        r = self.run_once([("ok", "Check the logs and"), ("ok", "the config.")])
        self.assertEqual(r["heard"], "Check the logs and the config.")

    def test_stop_phrase(self):
        self.assertEqual(self.run_once([("ok", "Stop listening.")])["status"], "stop")

    def test_timeout(self):
        self.assertEqual(self.run_once([]), {"status": "timeout", "heard": ""})

    def test_mic_failure(self):
        self.assertEqual(self.run_once([("fail", "")])["status"], "fail")

    def test_command_is_reported_not_typed(self):
        self.cfg["commands"] = {"wrap-session": {}}
        r = self.run_once([("ok", "command wrap session")])
        self.assertEqual(r, {"status": "command", "heard": "command wrap session", "command": "wrap-session"})

    def test_busy_when_the_iterm_loop_owns_the_mic(self):
        vl.listener_running = lambda: True
        self.assertEqual(self.run_once([("ok", "hello")])["status"], "busy")


class PhoneInbox(OnceCase):
    """The Talk page (voice-talk.py) as a second source for `listen once`."""

    def test_phone_live_follows_the_markers_age(self):
        self.assertFalse(vl.phone_live())
        vl.TALK_LIVE().touch()
        self.assertTrue(vl.phone_live())
        old = time.time() - 10
        os.utime(vl.TALK_LIVE(), (old, old))
        self.assertFalse(vl.phone_live())                 # a crashed server can't mute the desk

    def test_inbox_round_trip_is_read_once(self):
        vl.put_inbox("s1", {"status": "heard", "heard": "Open the settings file."})
        self.assertEqual(vl.take_inbox("s1"), {"status": "heard", "heard": "Open the settings file.", "via": "phone"})
        self.assertIsNone(vl.take_inbox("s1"))

    def test_inbox_text_is_defanged_and_checked(self):
        vl.put_inbox("s1", {"status": "heard", "heard": "! /clear\nnow"})
        self.assertEqual(vl.take_inbox("s1")["heard"], "clear now")
        for bad in ({"status": "stop", "heard": "x"}, {"status": "heard", "heard": "  "}, ["heard"],
                    {"status": "command", "heard": "x", "command": "../../x"}):
            vl.put_inbox("s1", bad)
            self.assertIsNone(vl.take_inbox("s1"), bad)
        vl.inbox_file("s1").write_text("{not json")
        self.assertIsNone(vl.take_inbox("s1"))
        self.assertFalse(vl.inbox_file("s1").exists())

    def test_phone_utterance_is_returned_without_recording(self):
        vl.put_inbox("s1", {"status": "heard", "heard": "From the couch."})
        r = self.run_once([("ok", "From the desk.")], sid="s1")
        self.assertEqual(r, {"status": "heard", "heard": "From the couch.", "via": "phone"})

    def test_phone_command_keeps_its_name(self):
        vl.put_inbox("s1", {"status": "command", "heard": "Command wrap session.", "command": "wrap-session"})
        self.assertEqual(self.run_once([], sid="s1")["command"], "wrap-session")

    def test_another_sessions_inbox_is_left_alone(self):
        vl.put_inbox("s2", {"status": "heard", "heard": "For someone else."})
        self.assertEqual(self.run_once([("ok", "From the desk.")], sid="s1")["heard"], "From the desk.")
        self.assertTrue(vl.inbox_file("s2").exists())

    def test_waiting_marker_exists_only_while_listening(self):
        seen = []

        def rec(cfg, wav, deadline=0.0):
            seen.append(vl.waiting_sessions())
            return "timeout"
        vl.listen_once(self.cfg, 5, wake=True, cwd="/p/gamma", rec=rec, stt=lambda c, w: "", claude_sid="s1")
        self.assertEqual([(w["claude_session"], w["cwd"], w["wake"]) for w in seen[0]], [("s1", "/p/gamma", True)])
        self.assertEqual(vl.waiting_sessions(), [])

    def test_marker_of_a_dead_process_is_removed(self):
        vl.once_file("gone").write_text(json.dumps({"pid": 2 ** 22 + 12345, "cwd": "/p", "wake": False, "ts": 1}))
        self.assertEqual(vl.waiting_sessions(), [])
        self.assertFalse(vl.once_file("gone").exists())

    def test_mic_stays_closed_while_the_phone_streams(self):
        vl.TALK_LIVE().touch()
        calls = []

        def rec(cfg, wav, deadline=0.0):
            calls.append(1)
            return "timeout"
        threading.Timer(0.4, lambda: vl.put_inbox("s1", {"status": "heard", "heard": "From the couch."})).start()
        r = vl.listen_once(self.cfg, 5, rec=rec, stt=lambda c, w: "", claude_sid="s1")
        self.assertEqual((r["heard"], calls), ("From the couch.", []))

    def test_recording_cut_off_by_the_phone_is_not_our_reply(self):
        """'spoke' normally opens the follow-up window; a phone take-over must not."""
        def rec(cfg, wav, deadline=0.0):
            vl.put_inbox("s1", {"status": "heard", "heard": "From the couch."})
            return "spoke"
        r = vl.listen_once(self.cfg, 5, wake=True, rec=rec, stt=lambda c, w: "", claude_sid="s1")
        self.assertEqual(r["heard"], "From the couch.")


class Benchmark(unittest.TestCase):
    """voice benchmark: score Whisper on synthetic speech. The scoring is pure and tested here;
    the audio run is tested once at the end (BenchmarkAudio)."""

    def test_word_errors_identical_ignores_case_and_punctuation(self):
        self.assertEqual(vl.word_errors("Claude, run the tests.", "claude run the Tests"), (0, 4))

    def test_word_errors_counts_substitution_deletion_insertion(self):
        self.assertEqual(vl.word_errors("check the Zephyrine bus", "check the zefferine bus"), (1, 4))
        self.assertEqual(vl.word_errors("check the Zephyrine bus", "check Zephyrine bus"), (1, 4))
        self.assertEqual(vl.word_errors("check the Zephyrine bus", "check the zeffer in bus"), (2, 4))

    def test_british_and_american_spelling_score_the_same(self):
        """2026-11-26: small.en wrote 'summarise' for Kokoro bm_george saying 'summarize'."""
        self.assertEqual(vl.word_errors("summarize the last hour", "summarise the last hour"), (0, 4))
        self.assertEqual(vl.word_errors("analyze the color", "analyse the colour"), (0, 3))
        self.assertEqual(vl.word_errors("the organization", "the organisation"), (0, 2))
        self.assertEqual(vl.word_errors("four tests", "for tests"), (1, 2))      # not a spelling variant
        self.assertEqual(vl.word_errors("rise", "rise"), (0, 1))                  # short words untouched
        self.assertEqual(vl.us_spelling("promise"), vl.us_spelling("promise"))    # consistent both ways

    def test_word_errors_empty(self):
        self.assertEqual(vl.word_errors("run the tests", ""), (3, 3))
        self.assertEqual(vl.word_errors("", ""), (0, 0))

    def test_has_term_whole_words_only(self):
        self.assertTrue(vl.has_term("ProofKit", "run the Proofkit deploy."))
        self.assertTrue(vl.has_term("whisper.cpp", "build whisper.cpp again"))
        self.assertTrue(vl.has_term("Home Assistant", "open home assistant"))
        self.assertFalse(vl.has_term("ProofKit", "run the proof kit deploy"))
        self.assertFalse(vl.has_term("sox", "soxx"))
        self.assertFalse(vl.has_term("", "anything"))

    def test_sentences_add_only_new_terms_once(self):
        s = vl.bench_sentences(["Zephyrine", "zephyrine", "GitHub", "Claude", "Zigbee"], limit=12)
        self.assertEqual(s[:len(vl.BENCH_SENTENCES)], vl.BENCH_SENTENCES)
        extra = s[len(vl.BENCH_SENTENCES):]
        self.assertEqual(len(extra), 2, extra)   # GitHub and Claude are in the built-ins already
        self.assertTrue(vl.has_term("Zephyrine", extra[0]))
        self.assertTrue(vl.has_term("Zigbee", extra[1]))

    def test_sentences_limit(self):
        s = vl.bench_sentences([f"Term{i}" for i in range(30)], limit=5)
        self.assertEqual(len(s), len(vl.BENCH_SENTENCES) + 5)

    def test_builtin_sentences_include_wake_and_plain(self):
        wake = [s for s in vl.BENCH_SENTENCES if vl.strip_wake(s)[1]]
        self.assertTrue(0 < len(wake) < len(vl.BENCH_SENTENCES))

    def rows(self):
        r = lambda model, hint, ref, heard, secs: dict(model=model, hint=hint, voice="V", ref=ref,
                                                       heard=heard, secs=secs)
        return [r("base.en", "off", "Claude, check the Zephyrine bus.", "Cloud, check the Zeffer in bus.", 0.3),
                r("base.en", "off", "Commit the changes.", "Commit the changes.", 0.5),
                r("base.en", "on", "Claude, check the Zephyrine bus.", "Claude, check the Zephyrine bus.", 0.4),
                r("base.en", "on", "Commit the changes.", "Commit the change.", 0.4)]

    def test_summary_scores_each_model_and_hint(self):
        s = {(g["model"], g["hint"]): g for g in vl.bench_summary(self.rows(), ["Zephyrine"])}
        off, on = s[("base.en", "off")], s[("base.en", "on")]
        self.assertEqual((off["errors"], off["words"]), (3, 8))   # Cloud, Holland, +at
        self.assertEqual((on["errors"], on["words"]), (1, 8))
        self.assertEqual((off["wake"], off["wake_total"]), (1, 1))   # "Cloud," counts as the wake word
        self.assertEqual(dict(off["missed"]), {"Zephyrine": 1})
        self.assertEqual(dict(on["missed"]), {})
        self.assertAlmostEqual(off["secs"], 0.4)

    def test_summary_wake_missed(self):
        rows = [dict(model="m", hint="on", voice="V", ref="Claude, run it.", heard="Run it.", secs=1)]
        g = vl.bench_summary(rows, [])[0]
        self.assertEqual((g["wake"], g["wake_total"]), (0, 1))

    def test_report_lists_rows_and_missed_terms(self):
        out = vl.bench_report(vl.bench_summary(self.rows(), ["Zephyrine"]))
        self.assertIn("base.en", out)
        self.assertIn("62.5%", out)      # 3 errors in 8 words
        self.assertIn("87.5%", out)
        self.assertIn("Zephyrine ×1", out)

    def test_report_accuracy_never_negative(self):
        rows = [dict(model="m", hint="on", voice="V", ref="hi", heard="a b c d", secs=1)]
        self.assertIn(" 0.0%", vl.bench_report(vl.bench_summary(rows, [])))

    def test_say_voice_names(self):
        listing = ("Daniel (English (UK))   en_GB    # Hello! My name is Daniel.\n"
                   "Samantha            en_US    # Hello! My name is Samantha.\n"
                   "Eddy (German (Germany)) de_DE    # Hallo!\n")
        self.assertEqual(vl.say_voices(listing), {"Daniel", "Samantha", "Eddy"})

    def test_model_names(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for n in ("ggml-base.en.bin", "ggml-small.en.bin", "ggml-silero-v5.1.2.bin"):
            (Path(d.name) / n).write_bytes(b"")
        names = lambda ms: [m.name for m in ms]   # noqa: E731
        self.assertEqual(names(vl.bench_models(d.name)), ["ggml-base.en.bin", "ggml-small.en.bin"])
        self.assertEqual(names(vl.bench_models(d.name, "small")), ["ggml-small.en.bin"])
        self.assertEqual(names(vl.bench_models(d.name, "nope")), [])


class BenchmarkRecord(unittest.TestCase):
    """voice benchmark --record: read sentences once, keep the clips, score them every run.
    Fake ask/rec/stt, so no microphone."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.said = []

    def run_record(self, answers, statuses=None, sentences=("One two.", "Three four.", "Five six.")):
        answers, statuses = iter(answers), iter(statuses or [])

        def ask(prompt):
            return next(answers)

        def rec(cfg, wav, deadline=0.0):
            Path(wav).write_bytes(b"RIFF")
            return next(statuses, "ok")

        out = io.StringIO()
        with unittest.mock.patch("sys.stdout", out):
            n = vl.bench_record(CFG, list(sentences), self.dir, ask=ask, rec=rec,
                                stt=lambda c, w: "heard it")
        return n, out.getvalue()

    def test_keep_skip_quit(self):
        n, _ = self.run_record(["", "", "s", "q"])       # record+keep #1, skip #2, quit at #3
        self.assertEqual(n, 1)
        clips = vl.recorded_clips(self.dir)
        self.assertEqual([t for t, _ in clips], ["One two."])
        self.assertTrue(Path(clips[0][1]).exists())

    def test_redo_then_keep(self):
        n, out = self.run_record(["", "r", "", "", "q"], sentences=("One two.",))
        self.assertEqual(n, 1)
        self.assertEqual(out.count("heard: heard it"), 2)

    def test_already_recorded_are_skipped(self):
        self.run_record(["", "", "q"])
        n, out = self.run_record(["", "", "", "", "q"])  # #1 is done: starts at #2
        self.assertEqual(n, 2)
        self.assertEqual(sorted(t for t, _ in vl.recorded_clips(self.dir)),
                         ["Five six.", "One two.", "Three four."])

    def test_nothing_heard_asks_again(self):
        n, out = self.run_record(["", "", "", "q"], statuses=["timeout", "ok"], sentences=("One two.",))
        self.assertEqual(n, 1)
        self.assertIn("nothing heard", out)

    def test_recorded_clips_ignores_missing_files_and_bad_index(self):
        self.assertEqual(vl.recorded_clips(self.dir), [])
        (self.dir / "clips.json").write_text("not json")
        self.assertEqual(vl.recorded_clips(self.dir), [])
        (self.dir / "a.wav").write_bytes(b"x")
        (self.dir / "clips.json").write_text(json.dumps({"a.wav": "Hello.", "gone.wav": "Bye."}))
        self.assertEqual(vl.recorded_clips(self.dir), [("Hello.", str(self.dir / "a.wav"))])

    def test_summary_keeps_your_voice_separate(self):
        rows = [dict(model="m", hint="on", voice="Samantha", ref="Run it.", heard="Run it.", secs=1),
                dict(model="m", hint="on", voice="you", ref="Run it.", heard="Ran it.", secs=1)]
        s = {g["speaker"]: g for g in vl.bench_summary(rows, [])}
        self.assertEqual(s["computer"]["errors"], 0)
        self.assertEqual(s["you"]["errors"], 1)
        self.assertIn("you", vl.bench_report(list(s.values())))


@unittest.skipUnless(HAVE_AUDIO, "needs say, sox, whisper-cli and the model")
class BenchmarkAudio(unittest.TestCase):
    def test_one_sentence_end_to_end(self):
        model = Path(CFG["model"])
        rows = vl.run_benchmark(CFG, ["Claude, run the tests."], ["Samantha"], [model], cwd="")
        self.assertEqual(len(rows), 2)                     # word list off and on
        self.assertEqual({r["hint"] for r in rows}, {"off", "on"})
        for r in rows:
            self.assertIn("run the tests", r["heard"].lower())
            self.assertGreater(r["secs"], 0)

    def test_recorded_clips_are_scored_as_you(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        wav = os.path.join(d.name, "mine.wav")
        vl.make_clip("Commit the changes.", "Samantha", wav)   # stands in for a real recording
        rows = vl.run_benchmark(CFG, [], ["Samantha"], [Path(CFG["model"])], cwd="",
                                recorded=[("Commit the changes.", wav)])
        self.assertEqual({r["voice"] for r in rows}, {"you"})
        self.assertIn("commit", rows[0]["heard"].lower())

    def _kokoro_stand_in(self, text):
        """A 24 kHz clip, like Kokoro writes (made with `say`, so no model is needed)."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        raw = os.path.join(d.name, "0.wav")
        subprocess.run(["say", "-v", "Samantha", "-o", raw, "--data-format=LEI16@24000", text], check=True)
        return raw

    def test_kokoro_voice_uses_its_presynthesized_clip(self):
        raw = self._kokoro_stand_in("Commit the changes.")
        with unittest.mock.patch.object(vl, "kokoro_clips", lambda s, v, f: [raw] * len(s)):
            rows = vl.run_benchmark(CFG, ["Commit the changes."], ["kokoro:af_heart"],
                                    [Path(CFG["model"])], cwd="")
        self.assertEqual({r["voice"] for r in rows}, {"kokoro:af_heart"})
        self.assertIn("commit", rows[0]["heard"].lower())   # 24 kHz was resampled to 16 kHz

    def test_failed_kokoro_voice_is_left_out(self):
        with unittest.mock.patch.object(vl, "kokoro_clips", lambda s, v, f: []):
            rows = vl.run_benchmark(CFG, ["Commit the changes."], ["kokoro:af_heart", "Samantha"],
                                    [Path(CFG["model"])], cwd="")
        self.assertEqual({r["voice"] for r in rows}, {"Samantha"})   # no crash, Samantha still scored

    def test_noise_still_transcribes(self):
        rows = vl.run_benchmark(CFG, ["Commit the changes."], ["Samantha"], [Path(CFG["model"])],
                                noise=0.02, cwd="")
        self.assertIn("commit", rows[0]["heard"].lower())


@unittest.skipUnless(HAVE_AUDIO, "needs say, sox, whisper-cli and the model")
class Pipeline(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.p = lambda n: os.path.join(self.d.name, n)

    def speech(self, name, text):
        subprocess.run(["say", "-v", "Samantha", "-o", self.p(name), "--data-format=LEI16@16000", text], check=True)

    def pcm(self, *wavs):
        """Concatenate wavs and return raw PCM, as `rec -t raw -` would stream it."""
        subprocess.run(["sox", *[self.p(w) for w in wavs], self.p("joined.wav")], check=True)
        with wave.open(self.p("joined.wav")) as w:
            return w.readframes(w.getnframes())

    def silence(self, name, secs):
        subprocess.run(["sox", "-n", "-r", "16000", "-c", "1", "-b", "16", self.p(name),
                        "trim", "0", str(secs)], check=True)

    def test_endpointing_stops_at_the_pause(self):
        """capture() on a stream instead of the mic:
        speech A + 3 s silence + speech B  ->  only A survives."""
        self.speech("a.wav", "First part of the message.")
        self.speech("b.wav", "This should be cut off.")
        self.silence("s.wav", 3)
        status, pcm = vl.capture(io.BytesIO(self.pcm("a.wav", "s.wav", "b.wav")), CFG)
        self.assertEqual(status, "ok")
        vl.write_wav(self.p("out.wav"), pcm)
        text = vl.clean(vl.transcribe(CFG, self.p("out.wav")), CFG)
        self.assertIn("first part", text.lower())
        self.assertNotIn("cut off", text.lower())

    def test_preroll_keeps_audio_before_trigger(self):
        """1 s silence + speech + 3 s silence: the capture must start about
        `preroll` seconds before the voice, not exactly at the trigger."""
        self.speech("a.wav", "Hello there.")
        self.silence("lead.wav", 1)
        self.silence("tail.wav", 3)
        full = self.pcm("lead.wav", "a.wav", "tail.wav")
        status, pcm = vl.capture(io.BytesIO(full), CFG)
        self.assertEqual(status, "ok")
        fb = vl.FRAME * 2
        start = full.find(pcm[:fb * 4]) / fb                       # where the capture begins (frames)
        thr = vl._threshold(CFG)
        onset = next(i for i in range(len(full) // fb)              # first loud frame = the voice
                     if vl._rms(full[i * fb:(i + 1) * fb]) > thr)
        lead_s = (onset - start) * vl.FRAME_S
        self.assertGreaterEqual(lead_s, CFG["preroll"] - 0.15, f"only {lead_s:.2f}s before the voice")

    def test_project_vocabulary_fixes_real_audio(self):
        """Measured: without the project's words -> 'Did Zepharinge sync to Buildbook yet?';
        with them -> 'Did Zephyrine sink to billbook yet?'. Vocabulary nudges, it doesn't
        guarantee: a replacement catches a near-miss you saw once in the log."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / ".claude").mkdir()
        (Path(d.name) / ".claude" / "voice.json").write_text(
            json.dumps({"vocabulary": ["Zephyrine", "billbook"],
                        "replacements": {"zepharinge": "Zephyrine"}}))
        self.speech("v.wav", "Did Zephyrine sync to billbook yet?")
        e = vl.effective(CFG, d.name)
        text = vl.clean(vl.transcribe(e, self.p("v.wav")), e)
        self.assertIn("Zephyrine", text)
        self.assertIn("billbook", text)

    def test_deadline_times_out_while_idle(self):
        self.silence("s.wav", 3)
        with wave.open(self.p("s.wav")) as w:
            pcm = w.readframes(w.getnframes())
        status, out = vl.capture(io.BytesIO(pcm), CFG, deadline=time.time() - 1)
        self.assertEqual((status, out), ("timeout", b""))

    def test_interrupt_discards(self):
        self.speech("a.wav", "Some words here.")
        status, pcm = vl.capture(io.BytesIO(self.pcm("a.wav")), CFG, interrupted=lambda: True)
        self.assertEqual((status, pcm), ("spoke", b""))

    def test_silence_transcribes_to_nothing(self):
        subprocess.run(["sox", "-n", "-r", "16000", "-c", "1", "-b", "16", self.p("s.wav"), "trim", "0", "4"], check=True)
        self.assertEqual(vl.clean(vl.transcribe(CFG, self.p("s.wav")), CFG), "")


class _VoiceDir(unittest.TestCase):
    """A private ~/.claude/voice and $TMPDIR for each test."""

    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        saved = (vl.voice_dir, vl.tmp_dir)
        self.addCleanup(lambda: (setattr(vl, "voice_dir", saved[0]), setattr(vl, "tmp_dir", saved[1])))
        vl.voice_dir = lambda: self.dir
        vl.tmp_dir = lambda: self.dir

    def run_cli(self, fn, args):
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stdout", out), unittest.mock.patch("sys.stderr", err):
            code = fn(args)
        return code, out.getvalue() + err.getvalue()


class Snippets(_VoiceDir):
    """ "snippet <name>" becomes the stored text."""
    S = {"standup": "Summarise what changed since yesterday.", "standup long": "The long one.",
         "risky": "/clear\nand !rm"}

    def c(self, s, snippets=None):
        return vl.clean(s, {**CFG, "snippets": self.S if snippets is None else snippets})

    def test_expands_with_whispers_punctuation(self):
        for said in ["snippet standup", "Snippet, standup.", "snippet: Standup", "Snippet - standup"]:
            self.assertTrue(self.c(said).startswith("Summarise what changed since yesterday."), said)
        self.assertEqual(self.c("Claude, snippet standup and be brief"),
                         "Claude, Summarise what changed since yesterday. and be brief")

    def test_needs_the_word_snippet_and_a_whole_name(self):
        self.assertEqual(self.c("the standup went well"), "the standup went well")
        self.assertEqual(self.c("snippet standups"), "snippet standups")
        self.assertEqual(self.c("snippet unknown"), "snippet unknown")

    def test_longest_name_first_and_hyphen(self):
        self.assertEqual(self.c("snippet standup long"), "The long one.")
        self.assertEqual(self.c("snippet stand-up", {"stand up": "ok then"}), "ok then")

    def test_a_snippet_cannot_become_a_command_or_send_early(self):
        self.assertEqual(self.c("snippet risky"), "clear and !rm")           # one line, no leading /
        self.assertEqual(vl.Gate(True, 30).feed(self.c("Claude, snippet risky"), 0), ("send", "clear and !rm"))

    def test_bad_settings_are_ignored(self):
        self.assertEqual(self.c("snippet standup", "nonsense"), "snippet standup")
        self.assertEqual(self.c("snippet a", {"a": 5, "": "x", 3: "y"}), "snippet a")

    def test_project_snippets_win(self):
        cwd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, cwd)
        (Path(cwd) / ".claude").mkdir()
        (Path(cwd) / ".claude" / "voice.json").write_text(json.dumps({"snippets": {"standup": "Project one."}}))
        e = vl.effective({**CFG, "snippets": self.S}, cwd)
        self.assertEqual(vl.clean("snippet standup", e), "Project one.")
        self.assertEqual(vl.clean("snippet standup long", e), "The long one.")

    def test_cli(self):
        code, out = self.run_cli(vl.snippets_cli, ["add", "Stand", "Up", "=", "Tell", "me = what changed"])
        self.assertEqual(code, 0)
        self.assertEqual(vl.load_config()["snippets"], {"stand up": "Tell me = what changed"})
        self.assertEqual(self.run_cli(vl.snippets_cli, ["remove", "nope"])[0], 1)
        self.assertEqual(self.run_cli(vl.snippets_cli, ["remove", "stand up"])[0], 0)
        self.assertEqual(vl.load_config()["snippets"], {})
        self.assertEqual(self.run_cli(vl.snippets_cli, ["add", "= text"])[0], 2)
        self.assertEqual(self.run_cli(vl.snippets_cli, ["bogus"])[0], 2)


class QuietMode(_VoiceDir):
    """`voice quiet on`: a lower threshold, from the next recording."""

    def test_factor_and_own_value(self):
        cfg = {**CFG, "threshold": "1.0%"}
        self.assertAlmostEqual(vl._threshold(cfg), 0.01)
        self.assertEqual(self.run_cli(vl.quiet_cli, ["on"])[0], 0)
        self.assertAlmostEqual(vl._threshold(cfg), 0.006)
        self.assertAlmostEqual(vl._threshold({**cfg, "quiet_threshold": "0.4%"}), 0.004)
        self.assertAlmostEqual(vl._threshold({**cfg, "quiet_threshold": "rubbish"}), 0.006)
        self.assertEqual(self.run_cli(vl.quiet_cli, ["off"])[0], 0)
        self.assertAlmostEqual(vl._threshold({**cfg, "quiet_threshold": "0.4%"}), 0.01)
        self.assertEqual(self.run_cli(vl.quiet_cli, ["off"])[0], 0)          # twice is fine
        self.assertEqual(self.run_cli(vl.quiet_cli, ["bogus"])[0], 2)

    def test_capture_uses_it(self):
        # 0.7 % "speech" is under the normal 1 % threshold and over the quiet 0.6 %
        import array as _a
        soft = _a.array("h", [int(0.007 * 32768)] * vl.FRAME).tobytes()
        still = bytes(vl.FRAME * 2)
        cfg = {**CFG, "threshold": "1.0%", "silence_seconds": 0.3, "max_seconds": 5}
        audio = soft * 20 + still * 20
        self.assertEqual(vl.capture(io.BytesIO(audio), cfg)[0], "fail")       # never started: stream ended
        self.run_cli(vl.quiet_cli, ["on"])
        status, pcm = vl.capture(io.BytesIO(audio), cfg)
        self.assertEqual(status, "ok")
        self.assertGreater(len(pcm), len(soft) * 15)


class LogRotation(_VoiceDir):
    """The plain-text log of what was said is set aside, then deleted."""

    def test_rotates_then_deletes(self):
        log, old = vl.LOG(), vl.LOG().with_name("claude-listen.log.old")
        log.write_text("10:00:00 heard: a secret\n")
        now = time.time()
        vl.rotate_log(24, now + 3600)                    # one hour old: untouched
        self.assertTrue(log.exists() and not old.exists())
        vl.rotate_log(24, now + 25 * 3600)               # older than a day: set aside
        self.assertFalse(log.exists())
        self.assertIn("a secret", old.read_text())
        vl.rotate_log(24, now + 30 * 3600)               # .old written 30 h ago: gone
        self.assertFalse(old.exists())

    def test_zero_keeps_everything_and_missing_files_are_fine(self):
        vl.rotate_log(24)                                # nothing there: no error
        vl.LOG().write_text("x\n")
        vl.rotate_log(0, time.time() + 10 ** 7)
        self.assertTrue(vl.LOG().exists())

    def test_log_checks_at_most_hourly(self):
        calls = []
        with unittest.mock.patch.object(vl, "rotate_log", lambda h, now=0.0: calls.append(h)), \
             unittest.mock.patch("sys.stderr", io.StringIO()):
            vl._LOG_CHECKED = 0.0
            vl.log("one")
            vl.log("two")
        self.assertEqual(calls, [24.0])
        self.assertEqual(len(vl.LOG().read_text().splitlines()), 2)


class ProjectScan(_VoiceDir):
    """`voice words scan`: a project's own names for Whisper's vocabulary hint."""
    WORDS = {"service", "schedule", "customer", "time", "out", "file", "name", "open", "the", "and",
             "check", "setting", "record", "stop", "report", "credit"}

    def project(self):
        root = Path(tempfile.mkdtemp()) / "wasteTrack"
        self.addCleanup(shutil.rmtree, root.parent)
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "node_modules" / "zzPackage").mkdir(parents=True)
        (root / ".git").mkdir()
        (root / "src" / "service_schedule.py").write_text(
            "class ServiceSchedule:\n    def customerID(self): pass\n"
            "def open_file(): pass\ndef zephyrine_report(): pass\nTIMEOUT = 0xffe0\n")
        (root / "tests" / "test_it.py").write_text("class ScheduleCase: pass\n")
        (root / "node_modules" / "zzPackage" / "index.js").write_text("function leftPad() {}\n")
        (root / ".git" / "notes.md").write_text("Gitzilla Gitzilla Gitzilla\n")
        (root / "README.md").write_text(
            "Open the Kokoro settings. Kokoro records. Oncely mentioned. The timeout and filename.\n"
            "It doesn't stop. Stopped recordings. README TODO ffe ffe ffe\n")
        return root

    def test_picks_the_right_names(self):
        root = self.project()
        got = vl.scan_terms(root, self.WORDS)
        self.assertEqual(got[0], "wasteTrack")                                   # the folder name leads
        for want in ("ServiceSchedule", "customerID", "zephyrine", "Kokoro"):
            self.assertIn(want, got)
        for junk in ("service", "schedule", "open", "file", "timeout", "filename", "Oncely", "doesn",
                     "Stopped", "recordings", "README", "TODO", "ffe", "ScheduleCase", "leftPad",
                     "Gitzilla", "TIMEOUT"):
            self.assertNotIn(junk, got, junk)

    def test_limits_have_and_no_dictionary(self):
        root = self.project()
        self.assertNotIn("Kokoro", vl.scan_terms(root, self.WORDS, have=["kokoro"]))
        self.assertLessEqual(len(", ".join(vl.scan_terms(root, self.WORDS, max_chars=25))), 25)
        self.assertEqual(len(vl.scan_terms(root, self.WORDS, max_terms=2)), 2)
        mixed = vl.scan_terms(root, set())                                       # no dictionary:
        self.assertIn("ServiceSchedule", mixed)                                  # mixedCase only
        self.assertNotIn("Kokoro", mixed)

    def test_cli_writes_scanned_and_keeps_your_own(self):
        root = self.project()
        (root / ".claude").mkdir()
        vj = root / ".claude" / "voice.json"
        vj.write_text(json.dumps({"vocabulary": ["customerID", "Acme"], "replacements": {"a": "b"}}))
        with unittest.mock.patch.object(vl, "load_dictionary", lambda path=None: self.WORDS):
            code, out = self.run_cli(vl.scan_cli, [str(root), "--dry"])
            self.assertEqual((code, json.loads(vj.read_text()).get("scanned")), (0, None))
            code, out = self.run_cli(vl.scan_cli, [str(root)])
        d = json.loads(vj.read_text())
        self.assertEqual(code, 0)
        self.assertEqual((d["vocabulary"], d["replacements"]), (["customerID", "Acme"], {"a": "b"}))
        self.assertIn("ServiceSchedule", d["scanned"])
        self.assertNotIn("customerID", d["scanned"])                             # already yours
        prompt = vl.effective({**CFG, "prompt": ""}, str(root))["prompt"]
        self.assertTrue(prompt.startswith("customerID, Acme, "))                 # yours first
        self.assertIn("ServiceSchedule", prompt)

    def test_cli_refuses_broken_json_and_a_missing_folder(self):
        root = self.project()
        (root / ".claude").mkdir()
        (root / ".claude" / "voice.json").write_text("{not json")
        self.assertEqual(self.run_cli(vl.scan_cli, [str(root)])[0], 1)
        self.assertEqual((root / ".claude" / "voice.json").read_text(), "{not json")
        self.assertEqual(self.run_cli(vl.scan_cli, [str(root / "nope")])[0], 2)


if __name__ == "__main__":
    unittest.main()
