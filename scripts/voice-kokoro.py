#!/usr/bin/env -S uv run -q --python 3.13 --script
# /// script
# requires-python = ">=3.10,<3.14"
# dependencies = ["kokoro-onnx==0.6.1", "soundfile==0.14.0"]
# [tool.uv]
# exclude-newer = "2026-11-26T00:00:00Z"   # transitive deps: nothing published after this date
# ///
# Name:    voice-kokoro
# Purpose: voiceLoop's neural "mouth": speak text from stdin with a local Kokoro
#          voice, SENTENCE BY SENTENCE. Sentence 1 is synthesized and starts playing
#          while sentence 2 is synthesized, so the first sound waits for one short
#          sentence, not the whole paragraph (whole-paragraph synthesis took 2-3 s).
#          A long first sentence (> 12 words) is split at its first comma, too.
# Usage:   Started detached by voice-speak.py when config "engine": "kokoro":
#            echo "Hello there. Second sentence." | voice-kokoro.py --voice bf_emma
#          Options: --voice bf_emma  --speed 1.0  --fallback-voice Samantha
#          Benchmark clips (no playback, no `say` fallback; exit 1 if Kokoro can't run):
#            printf 'One.\nTwo.\n' | voice-kokoro.py --to-dir DIR --voice af_heart  → DIR/0.wav, 1.wav
# Deps:    uv; Python 3.13 (onnxruntime has no 3.15 wheels yet, so the shebang pins it);
#          kokoro-onnx + soundfile, pinned (uv installs them from PyPI into its cache on the
#          first run; exclude-newer freezes their dependencies too);
#          model files in ~/.local/share/voiceloop/kokoro/ (kokoro-v1.0.onnx,
#          voices-v1.0.bin from github.com/thewh1teagle/kokoro-onnx releases).
# Notes:   Any failure falls back to macOS `say`, so a reply is never silently lost.
#          Killed as a process group by voice-speak.py (new speech / voice stop / typing).
#          Writes "first audio after X.XXs" to $TMPDIR/claude-voice.timing when the hook
#          passes VOICELOOP_T0 (turn end). Lessons: [[A voice loop for Claude Code]].

import argparse
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

MODELS = Path(os.environ.get("VOICELOOP_KOKORO_DIR") or Path.home() / ".local/share/voiceloop/kokoro")


def sentences(text: str) -> list:
    """Split after . ! ? (keeps the punctuation, which drives Kokoro's intonation)."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


SPLIT_OVER = 12   # words: a longer FIRST sentence is split at a comma
MIN_HEAD = 2      # words before that comma, so a lone "Yes," never becomes its own chunk


def chunks(text: str) -> list:
    """sentences(), but a long FIRST sentence is split after its first comma that has at
    least MIN_HEAD words before it. The first sound waits for the whole first chunk to be
    synthesized (1.34 s short vs 2.47 s for 26 words, measured 2026-11-25); later chunks are
    synthesized while earlier ones play, so only the first one matters."""
    out = sentences(text)
    if not out or len(out[0].split()) <= SPLIT_OVER:
        return out
    words = out[0].split()
    for i, w in enumerate(words[:-1]):
        if w.endswith(",") and i + 1 >= MIN_HEAD:
            return [" ".join(words[:i + 1]), " ".join(words[i + 1:])] + out[1:]
    return out


def lang_for(voice: str) -> str:
    return "en-gb" if voice[:1] == "b" else "en-us"   # bf_/bm_ = British, af_/am_ = American


def say(text: str, voice: str) -> None:
    cmd = ["say"] + (["-v", voice] if voice else [])
    subprocess.run(cmd, input=text.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def log_first_audio() -> None:
    t0 = os.environ.get("VOICELOOP_T0")
    if not t0:
        return
    try:
        tmp = Path(os.environ.get("TMPDIR") or "/tmp")
        (tmp / "claude-voice.timing").write_text(
            f"{time.strftime('%H:%M:%S')} kokoro first audio after {time.time() - float(t0):.2f}s\n")
    except (OSError, ValueError):
        pass


def to_dir(folder: str, lines: list, voice: str, speed: float) -> int:
    """Synthesize each line to <folder>/<n>.wav (Kokoro's own rate, 24 kHz). The model loads
    once for all lines: a process per sentence would pay uv + model start every time."""
    try:
        import soundfile as sf
        from kokoro_onnx import Kokoro
        k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
        for i, line in enumerate(lines):
            if line.strip():
                samples, rate = k.create(line.strip(), voice=voice, speed=speed, lang=lang_for(voice))
                sf.write(os.path.join(folder, f"{i}.wav"), samples, rate)
    except Exception as e:
        print(f"voice-kokoro: {e}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="bf_emma")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--fallback-voice", default="Samantha")
    ap.add_argument("--to-dir", default="",
                    help="don't play: write one WAV per stdin LINE (0.wav, 1.wav, …) into this "
                         "folder (voice benchmark). No `say` fallback: exit 1 if Kokoro can't run")
    a = ap.parse_args()
    if a.to_dir:
        return to_dir(a.to_dir, sys.stdin.read().splitlines(), a.voice, a.speed)
    text = sys.stdin.read().strip()
    if not text:
        return 0
    try:
        import soundfile as sf
        from kokoro_onnx import Kokoro
        k = Kokoro(str(MODELS / "kokoro-v1.0.onnx"), str(MODELS / "voices-v1.0.bin"))
    except Exception:
        say(text, a.fallback_voice)          # no model / broken install: never go silent
        return 0

    q: queue.Queue = queue.Queue(maxsize=2)  # synthesize at most 2 sentences ahead
    lang = lang_for(a.voice)

    def produce(tmpdir: str) -> None:
        for i, s in enumerate(chunks(text)):
            try:
                samples, rate = k.create(s, voice=a.voice, speed=a.speed, lang=lang)
                path = os.path.join(tmpdir, f"{i}.wav")
                sf.write(path, samples, rate)
                q.put(("wav", path))
            except Exception:
                q.put(("say", s))            # one bad sentence: say just that one
        q.put(None)

    with tempfile.TemporaryDirectory(prefix="voiceloop-kokoro-") as td:
        threading.Thread(target=produce, args=(td,), daemon=True).start()
        first = True
        while (item := q.get()) is not None:
            kind, x = item
            if first:
                log_first_audio()
                first = False
            if kind == "wav":
                subprocess.run(["afplay", x], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                say(x, a.fallback_voice)
    return 0


if __name__ == "__main__":
    sys.exit(main())
