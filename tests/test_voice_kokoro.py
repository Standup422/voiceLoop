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
    def test_to_dir_fails_loudly_without_kokoro(self):
        """voice benchmark needs a clear failure (exit 1), never the `say` fallback that
        playback uses: a benchmark speaker must be the voice it claims to be."""
        import tempfile
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        old = vk.MODELS
        vk.MODELS = Path(d.name) / "missing"
        try:
            self.assertEqual(vk.to_dir(d.name, ["Hello there."], "af_heart", 1.0), 1)
        finally:
            vk.MODELS = old
        self.assertEqual(os.listdir(d.name), [])

    def test_sentences_keep_punctuation(self):
        self.assertEqual(vk.sentences("Yes, I see it. Say switch to Acme! Ready?  "),
                         ["Yes, I see it.", "Say switch to Acme!", "Ready?"])
        self.assertEqual(vk.sentences(""), [])
        self.assertEqual(vk.sentences("No end punctuation"), ["No end punctuation"])

    # First sound waits for the whole FIRST chunk to be synthesized (measured 2026-11-25:
    # 1.34 s for a short first sentence, 2.47 s for a 26-word one). So a long first
    # sentence is split at its first comma; everything after is synthesized while it plays.
    LONG = ("I looked at the listener log, and the problem was that the shared file made every "
            "session's reply look like yours. The rest is on screen.")

    def test_long_first_sentence_splits_at_first_comma(self):
        self.assertEqual(vk.chunks(self.LONG), [
            "I looked at the listener log,",
            "and the problem was that the shared file made every session's reply look like yours.",
            "The rest is on screen."])

    def test_short_first_sentence_is_not_split(self):
        self.assertEqual(vk.chunks("Yes, I see it. Ready?"), ["Yes, I see it.", "Ready?"])

    def test_only_the_first_sentence_is_split(self):
        later = "Done. " + self.LONG
        self.assertEqual(vk.chunks(later), ["Done."] + vk.sentences(self.LONG))

    def test_no_comma_or_comma_too_early_stays_whole(self):
        no_comma = "This sentence is long enough to count but it has no comma anywhere in it at all."
        self.assertEqual(vk.chunks(no_comma), [no_comma])
        early = "Yes, this sentence is long enough to count and its only comma comes after one word."
        self.assertEqual(vk.chunks(early), [early])

    def test_chunks_lose_no_words(self):
        for t in (self.LONG, "Yes, I see it.", "", "a, b, c d e f g h i j k l m n o p, q."):
            self.assertEqual(" ".join(vk.chunks(t)).split(), t.split())

    def test_lang_from_voice_prefix(self):
        self.assertEqual(vk.lang_for("bf_emma"), "en-gb")
        self.assertEqual(vk.lang_for("bm_george"), "en-gb")
        self.assertEqual(vk.lang_for("af_heart"), "en-us")


if __name__ == "__main__":
    unittest.main()
