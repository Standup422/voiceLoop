"""Tests for examples/voice-dashboard.py: the example may only ever run its four fixed commands."""

import importlib.util
import sys
sys.dont_write_bytecode = True
import http.client
import json
import threading
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "examples" / "voice-dashboard.py"
spec = importlib.util.spec_from_file_location("voice_dashboard", SCRIPT)
vd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vd)

STATUS = {"listening": {"on": True, "mode": "wake", "target": "myProject", "last": None},
          "speaking": {"global": False, "sessions": ["myProject"], "playingNow": False, "speaker": None, "waiting": []},
          "projects": ["myProject", "otherProject"], "hooks": {}, "commands": []}


class Result:
    returncode, stderr = 0, ""

    def __init__(self, stdout=""):
        self.stdout = stdout


class Example(unittest.TestCase):
    def setUp(self):
        self.ran = []
        saved = (vd.run_voice, vd.talk_status)
        self.addCleanup(lambda: [setattr(vd, n, f) for n, f in zip(("run_voice", "talk_status"), saved)])

        def run_voice(args):
            self.ran.append(args)
            return Result(json.dumps(STATUS) if args == ["status", "--json"] else "")
        vd.run_voice, vd.talk_status = run_voice, lambda port=8445: {"running": False}
        vd._cache.update(at=0.0, data=None)
        self.srv = vd.make_server(0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read()

    def commands(self):
        return [a for a in self.ran if a != ["status", "--json"]]

    def test_status_is_passed_on_and_shared_between_pages(self):
        for _ in range(3):
            status, body = self.req("GET", "/api/voice")
            self.assertEqual((status, json.loads(body)["listening"]["target"]), (200, "myProject"))
        self.assertEqual(self.ran, [["status", "--json"]], "three pages, one process")

    def test_each_button_runs_its_one_fixed_command(self):
        for do, argv in (("silence", ["stop"]), ("replies-off", ["off", "all"]), ("stop-listening", ["listen", "off"])):
            self.assertEqual(self.req("POST", "/api/act", {"do": do})[0], 200, do)
            self.assertEqual(self.commands()[-1], argv)
        self.assertEqual(self.req("POST", "/api/act", {"do": "switch", "project": "otherProject"})[0], 200)
        self.assertEqual(self.commands()[-1], ["listen", "switch", "otherProject"])

    def test_nothing_else_can_be_run(self):
        for body in ({"do": "listen"}, {"do": "on"}, {"do": "switch", "project": "x; rm -rf ~"},
                     {"do": "switch", "project": "notOpen"}, {"do": "switch"}, {"do": ["stop"]}, {}, "stop"):
            self.assertEqual(self.req("POST", "/api/act", body)[0], 400, body)
        self.assertEqual(self.commands(), [], "no microphone can be started, no text reaches a command")

    def test_only_its_own_page_may_press_the_buttons(self):
        self.assertEqual(self.req("POST", "/api/act", {"do": "silence"}, {"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.req("GET", "/api/voice", headers={"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.commands(), [])
        self.assertEqual(self.srv.server_address[0], "127.0.0.1", "this computer only")

    def test_no_action_can_start_a_microphone_or_turn_replies_on(self):
        self.assertEqual(set(vd.ACTIONS), {"silence", "replies-off", "stop-listening", "switch"})
        for argv in vd.ACTIONS.values():
            self.assertIn(argv[:2], (["stop"], ["off", "all"], ["listen", "off"], ["listen", "switch"]), argv)

if __name__ == "__main__":
    unittest.main()
