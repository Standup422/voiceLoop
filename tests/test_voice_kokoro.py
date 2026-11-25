"""Tests for voice-kokoro.py's pure helpers (no model, no audio). The kokoro imports
live inside main(), so the module loads under any Python."""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
_ROOT = Path(__file__).resolve().parent.parent
_DEV = os.environ.get("VOICELOOP_SRC") or ((_ROOT / ".dev-source").read_text().strip()
                                           if (_ROOT / ".dev-source").exists() else "")
_DEV_COPY = Path(_DEV).expanduser() / "voice-kokoro.py" if _DEV else None
SCRIPT = Path(os.environ.get("VOICE_KOKORO") or   # a developer's canonical copy if set, else scripts/
              (_DEV_COPY if _DEV_COPY and _DEV_COPY.exists() else _ROOT / "scripts" / "voice-kokoro.py"))
spec = importlib.util.spec_from_file_location("voice_kokoro", SCRIPT)
vk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vk)


class Helpers(unittest.TestCase):
    def test_sentences_keep_punctuation(self):
        self.assertEqual(vk.sentences("Yes, I see it. Say switch to Acme! Ready?  "),
                         ["Yes, I see it.", "Say switch to Acme!", "Ready?"])
        self.assertEqual(vk.sentences(""), [])
        self.assertEqual(vk.sentences("No end punctuation"), ["No end punctuation"])

    def test_lang_from_voice_prefix(self):
        self.assertEqual(vk.lang_for("bf_emma"), "en-gb")
        self.assertEqual(vk.lang_for("bm_george"), "en-gb")
        self.assertEqual(vk.lang_for("af_heart"), "en-us")


if __name__ == "__main__":
    unittest.main()
