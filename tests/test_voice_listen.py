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
import time
import unittest
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


class SuggestThreshold(unittest.TestCase):
    """Room calibration (install.sh): RMS levels per 30 ms frame, 0..1."""

    def test_quiet_laptop_mic(self):
        # measured 2026-11-25: median 0.43 %, max 0.55 %; recordings never ended at 0.5 %
        levels = [0.0043] * 60 + [0.0050] * 5 + [0.0055] * 2
        r = vl.suggest_threshold(levels)
        self.assertTrue(r["ok"])
        self.assertEqual(r["threshold"], "0.9%")

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


class ListenOnce(unittest.TestCase):
    """App mode (desktop app Code tab): listen_once with a fake recorder + transcriber.
    Each script step is (record_status, transcript)."""

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

    def run_once(self, steps, wake=False, timeout=5):
        it = iter(steps)
        self.current = ""

        def rec(cfg, wav, deadline=0.0):
            try:
                status, self.current = next(it)
            except StopIteration:
                return "timeout"
            return status

        return vl.listen_once(self.cfg, timeout, wake=wake, rec=rec, stt=lambda c, w: self.current)

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


if __name__ == "__main__":
    unittest.main()
