"""Tests for voice-speak.py — run: python3 -m unittest discover -s tests -v
No audio is played: hook-mode tests use engine "none", which only writes
$TMPDIR/claude-voice.last.txt. Set VOICE_SPEAK to test another copy
(e.g. the runtime one in ~/.claude/hooks)."""

import importlib.util
import sys
sys.dont_write_bytecode = True  # never drop __pycache__ next to the scripts under test (~/.claude/hooks)
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
_ROOT = Path(__file__).resolve().parent.parent
_DEV = os.environ.get("VOICELOOP_SRC") or ((_ROOT / ".dev-source").read_text().strip()
                                           if (_ROOT / ".dev-source").exists() else "")
_DEV_COPY = Path(_DEV).expanduser() / "voice-speak.py" if _DEV else None
SCRIPT = Path(os.environ.get("VOICE_SPEAK") or   # a developer's canonical copy if set, else scripts/
              (_DEV_COPY if _DEV_COPY and _DEV_COPY.exists() else _ROOT / "scripts" / "voice-speak.py"))
FIXTURE = HERE / "fixtures" / "stop-payload.json"

spec = importlib.util.spec_from_file_location("voice_speak", SCRIPT)
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)


class Cleaner(unittest.TestCase):
    def full(self, md):
        return vs.clean_for_speech(md, "full", 10_000)

    def test_code_block_omitted(self):
        out = self.full("Before.\n\n```bash\nrm -rf /\n```\n\nAfter.")
        self.assertEqual(out, "Before. Code block omitted. After.")

    def test_unclosed_fence_dropped(self):
        self.assertEqual(self.full("Hi.\n\n```py\nprint(1)\n"), "Hi. Code block omitted.")

    def test_table_omitted(self):
        out = self.full("Intro.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nOutro.")
        self.assertEqual(out, "Intro. Table omitted. Outro.")

    def test_urls_and_links(self):
        self.assertEqual(self.full("See https://code.claude.com/docs/en/hooks."), "See a link.")
        self.assertEqual(self.full("Read [the docs](https://x.y/z) now."), "Read the docs now.")

    def test_paths_and_file_line(self):
        self.assertEqual(self.full("Edit `~/.claude/settings.json` now."), "Edit settings.json now.")
        self.assertEqual(self.full("In /Users/x/Projects/a/main.py:42 there."), "In main.py there.")
        self.assertEqual(self.full("See tools/capture-stop.sh:12."), "See capture-stop.sh.")
        self.assertEqual(self.full("Both docs/PHASE0.md and and/or stay sane."),
                         "Both PHASE0.md and and/or stay sane.")

    def test_markup_stripped(self):
        self.assertEqual(self.full("## Gate 0\n\n**Bold** and *it* and `code`."),
                         "Gate 0. Bold and it and code.")

    def test_lists_get_pauses(self):
        self.assertEqual(self.full("Steps:\n\n- one\n- two\n1. three"), "Steps: one. two. three.")

    def test_snake_case_and_symbols(self):
        self.assertEqual(self.full("Use last_assistant_message → done ✅"),
                         "Use last assistant message to done")

    def test_empty(self):
        self.assertEqual(vs.clean_for_speech(""), "")
        self.assertEqual(vs.clean_for_speech("```\nx\n```"), "Code block omitted.")


class Selection(unittest.TestCase):
    def test_first_paragraph_plus_rest(self):
        out = vs.clean_for_speech("First para.\n\nSecond para.")
        self.assertEqual(out, "First para. The rest is on screen.")

    def test_single_paragraph_no_rest(self):
        self.assertEqual(vs.clean_for_speech("Only one."), "Only one.")

    def test_heading_joined_with_next(self):
        out = vs.clean_for_speech("## Result\n\nIt passed.\n\nDetails.")
        self.assertEqual(out, "Result. It passed. The rest is on screen.")

    def test_leadin_colon_joined(self):
        out = vs.clean_for_speech("Two things:\n\n- a\n- b\n\nMore.")
        self.assertEqual(out, "Two things: a. b. The rest is on screen.")

    def test_skips_leading_code_block(self):
        out = vs.clean_for_speech("```\nx\n```\n\nExplanation here.")
        self.assertEqual(out, "Explanation here. The rest is on screen.")

    def test_cap_at_sentence(self):
        md = " ".join(f"Sentence number {i} is here." for i in range(60))
        out = vs.clean_for_speech(md, max_chars=200)
        self.assertTrue(out.endswith("is here. The rest is on screen."), out)
        self.assertLessEqual(len(out), 200 + len(" The rest is on screen."))


class Transcript(unittest.TestCase):
    def write(self, entries):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in entries:
            f.write(json.dumps(e) + "\n")
        f.write("not json\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def test_list_content(self):
        p = self.write([{"type": "user", "message": {"content": "q"}},
                        {"type": "assistant", "message": {"content": [
                            {"type": "thinking", "thinking": "hmm"},
                            {"type": "text", "text": "Answer."}]}}])
        self.assertEqual(vs.text_from_transcript(p, wait=0), "Answer.")

    def test_string_content(self):
        p = self.write([{"type": "assistant", "message": {"content": "Plain."}}])
        self.assertEqual(vs.text_from_transcript(p, wait=0), "Plain.")

    def test_not_flushed_yet_is_silent(self):
        # last entry is the user's: the reply isn't written yet -> don't speak the OLD one
        p = self.write([{"type": "assistant", "message": {"content": "Old reply."}},
                        {"type": "user", "message": {"content": "new question"}}])
        self.assertEqual(vs.text_from_transcript(p, wait=0), "")

    def test_subagent_and_missing(self):
        self.assertEqual(vs.text_from_transcript("/x/subagents/a.jsonl", wait=0), "")
        self.assertEqual(vs.text_from_transcript("/nonexistent.jsonl", wait=0), "")


class HookMode(unittest.TestCase):
    """Runs the real script as Claude Code would: payload on stdin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        (t / "voice").mkdir()
        (t / "voice" / "config.json").write_text(json.dumps({"engine": "none"}))
        self.env = {**os.environ, "TMPDIR": str(t), "CLAUDE_VOICE_DIR": str(t / "voice")}
        self.last = t / "claude-voice.last.txt"
        self.payload = json.loads(FIXTURE.read_text())

    def run_hook(self, stdin):
        return subprocess.run([str(SCRIPT)], input=stdin, capture_output=True,
                              text=True, env=self.env, timeout=20)

    def assertSilentExit0(self, r):
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")  # stdout from a Stop hook could be read as a decision

    def test_disabled_by_default(self):
        r = self.run_hook(json.dumps(self.payload))
        self.assertSilentExit0(r)
        self.assertFalse(self.last.exists())

    def test_global_flag_speaks(self):
        (Path(self.env["CLAUDE_VOICE_DIR"]) / "enabled").touch()
        r = self.run_hook(json.dumps(self.payload))
        self.assertSilentExit0(r)
        self.assertTrue(self.last.read_text().startswith("It worked."))

    def test_marks_which_session_spoke(self):
        """The listener waits for ITS session's reply, not any reply (claude-voice.spoke.<sid>)."""
        (Path(self.env["CLAUDE_VOICE_DIR"]) / "enabled").touch()
        self.run_hook(json.dumps(self.payload))
        mark = Path(self.env["TMPDIR"]) / f"claude-voice.spoke.{self.payload['session_id']}"
        self.assertTrue(mark.exists())
        self.assertEqual(mark.read_text(), "", "the marker holds no reply text")

    def phone_turn(self, live=True):
        t, sid = Path(self.env["TMPDIR"]), self.payload["session_id"]
        (t / f"claude-voice.{sid}").touch()
        (t / f"claude-talk.turn.{sid}").write_text("0")
        if live:
            (t / "claude-talk.live").touch()
        return t, sid

    def test_phone_turn_is_answered_on_the_phone_not_the_mac(self):
        t, sid = self.phone_turn()
        self.assertSilentExit0(self.run_hook(json.dumps(self.payload)))
        self.assertTrue((t / f"claude-talk.reply.{sid}").read_text().startswith("It worked."))
        self.assertFalse((t / f"claude-voice.spoke.{sid}").exists(), "nothing was spoken on the Mac")
        self.assertFalse((t / f"claude-talk.turn.{sid}").exists(), "one reply per phone turn")
        self.run_hook(json.dumps(self.payload))                       # the next turn was typed at the desk
        self.assertTrue((t / f"claude-voice.spoke.{sid}").exists())

    def test_phone_turn_with_the_page_gone_is_spoken_on_the_mac(self):
        t, sid = self.phone_turn(live=False)
        self.run_hook(json.dumps(self.payload))
        self.assertFalse((t / f"claude-talk.reply.{sid}").exists())
        self.assertTrue((t / f"claude-voice.spoke.{sid}").exists())

    def test_phone_turn_gets_the_spoken_note(self):
        self.phone_turn()
        r = self.run_hook(json.dumps({**self.payload, "hook_event_name": "UserPromptSubmit"}))
        self.assertIn("SPOKEN", r.stdout)

    def test_says_the_project_only_when_another_session_spoke_last(self):
        (Path(self.env["CLAUDE_VOICE_DIR"]) / "enabled").touch()
        mine = json.dumps(self.payload)
        other = json.dumps({**self.payload, "session_id": "ffffffff-0000", "cwd": "/somewhere/otherProject"})
        said = []
        for who in (mine, mine, other, other, mine):
            self.assertSilentExit0(self.run_hook(who))
            said.append(self.last.read_text().split(":")[0] if self.last.read_text().startswith("From ") else "")
        self.assertEqual(said, ["", "", "From otherProject", "", "From voiceLoop"])

    def test_a_phone_turn_never_gets_the_project_prefix(self):
        t, sid = self.phone_turn()
        (t / "claude-voice.speaker").write_text("ffffffff-0000")
        self.run_hook(json.dumps(self.payload))
        self.assertTrue((t / f"claude-talk.reply.{sid}").read_text().startswith("It worked."))

    def notification(self, kind="permission_prompt", **more):
        return json.dumps({"hook_event_name": "Notification", "session_id": self.payload["session_id"],
                           "cwd": "/somewhere/myProject", "message": "Claude needs your permission to use Bash",
                           "notification_type": kind, **more})

    def test_notification_is_silent_when_voice_is_off(self):
        self.assertSilentExit0(self.run_hook(self.notification()))
        self.assertEqual([f.name for f in Path(self.env["TMPDIR"]).glob("claude-*")], [])

    def test_notification_in_a_phone_turn_goes_to_the_phone(self):
        t, sid = self.phone_turn()
        self.assertSilentExit0(self.run_hook(self.notification()))
        self.assertEqual((t / f"claude-talk.notice.{sid}").read_text(), "Claude is asking for permission in myProject.")
        self.assertTrue((t / f"claude-talk.turn.{sid}").exists(), "the reply is still to come, on the phone")
        self.assertFalse((t / f"claude-talk.reply.{sid}").exists(), "a notice is not the reply")

    def test_notification_at_the_desk_is_not_a_reply(self):
        t, sid = Path(self.env["TMPDIR"]), self.payload["session_id"]
        (t / f"claude-voice.{sid}").touch()
        self.assertSilentExit0(self.run_hook(self.notification()))
        self.assertFalse(self.last.exists(), "not 'the last reply'")
        self.assertFalse((t / f"claude-voice.spoke.{sid}").exists(), "the listener still waits for the real reply")
        self.assertFalse((t / f"claude-talk.notice.{sid}").exists())

    def test_notification_says_a_fixed_line_never_the_message(self):
        r = subprocess.run([str(SCRIPT), "--dry-run"], input=self.notification(), capture_output=True,
                           text=True, env=self.env, timeout=20)
        self.assertEqual(r.stdout.strip(), "Claude is asking for permission in myProject.")
        r = subprocess.run([str(SCRIPT), "--dry-run"], input=self.notification("elicitation_dialog", cwd="/x/a;b`c"),
                           capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(r.stdout.strip(), "Claude has a question for you in a b c.")

    def test_other_notifications_say_nothing(self):
        t, sid = self.phone_turn()
        for kind in ("idle_prompt", "auth_success", "something_new", None):
            self.assertSilentExit0(self.run_hook(self.notification(kind)))
        self.assertFalse((t / f"claude-talk.notice.{sid}").exists())

    def test_session_flag_speaks_only_that_session(self):
        (Path(self.env["TMPDIR"]) / f"claude-voice.{self.payload['session_id']}").touch()
        self.run_hook(json.dumps(self.payload))
        self.assertTrue(self.last.exists())
        self.last.unlink()
        other = {**self.payload, "session_id": "ffffffff-0000-0000-0000-000000000000"}
        self.run_hook(json.dumps(other))
        self.assertFalse(self.last.exists())

    def test_stop_hook_active_is_silent(self):
        (Path(self.env["CLAUDE_VOICE_DIR"]) / "enabled").touch()
        r = self.run_hook(json.dumps({**self.payload, "stop_hook_active": True}))
        self.assertSilentExit0(r)
        self.assertFalse(self.last.exists())

    def test_registers_pane_even_when_voice_off(self):
        env = {**self.env, "ITERM_SESSION_ID": "w0t0p1:ABCDEF00-1111-2222-3333-444455556666"}
        subprocess.run([str(SCRIPT)], input=json.dumps(self.payload), capture_output=True,
                       text=True, env=env, timeout=20)
        reg = Path(self.env["TMPDIR"]) / "claude-voice-panes" / f"{self.payload['session_id']}.json"
        entry = json.loads(reg.read_text())
        self.assertEqual(entry["project"], "voiceLoop")
        self.assertEqual(entry["iterm_session"], env["ITERM_SESSION_ID"])
        self.assertFalse(self.last.exists())  # registered, but voice is off: silent

    def test_prompt_submit_adds_context_only_when_listening_to_this_session(self):
        t = Path(self.env["TMPDIR"])
        sid = self.payload["session_id"]
        msg = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": sid, "prompt": "hi"})
        self.assertEqual(self.run_hook(msg).stdout, "")            # no listener: nothing added
        # fake a running listener: a real process whose args contain "voice-listen"
        fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "voice-listen"])
        self.addCleanup(fake.kill)
        (t / "claude-listen.pid").write_text(str(fake.pid))
        (t / "claude-listen.target").write_text(sid)
        out = json.loads(self.run_hook(msg).stdout)
        self.assertIn("SPOKEN", out["hookSpecificOutput"]["additionalContext"])
        other = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "ffffffff-0000"})
        self.assertEqual(self.run_hook(other).stdout, "")          # another session: untouched

    def test_status_json_never_contains_text(self):
        """Dashboards may be visible to others: the snapshot may carry times and
        outcomes, never what was heard or spoken."""
        t = Path(self.env["TMPDIR"])
        secret = "PINEAPPLE-7731 my private sentence"
        (t / "claude-listen.log").write_text(f"10:00:00 heard (0.40s): {secret}\n"
                                             "10:00:01 sent; mic closed until the reply has been spoken\n")
        (t / "claude-voice.last.txt").write_text(f"Reply about {secret}")
        r = subprocess.run([str(SCRIPT), "status", "--json"], capture_output=True, text=True,
                           env=self.env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("PINEAPPLE", r.stdout)
        snap = json.loads(r.stdout)
        self.assertEqual(snap["listening"]["last"], {"at": "10:00:01", "kind": "sent"})  # time + outcome only
        self.assertIn("engine", snap["speaking"])

    def test_garbage_input_never_fails(self):
        (Path(self.env["CLAUDE_VOICE_DIR"]) / "enabled").touch()
        for junk in ["", "not json", "[1,2]", '{"session_id":"../../etc/passwd"}']:
            self.assertSilentExit0(self.run_hook(junk))


if __name__ == "__main__":
    unittest.main()
