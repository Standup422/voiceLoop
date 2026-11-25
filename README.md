# voiceLoop

**Talk to [Claude Code](https://claude.com/claude-code) hands-free and hear it answer, with speech
recognition and voice running entirely on your Mac.**

You speak; a local [whisper.cpp](https://github.com/ggml-org/whisper.cpp) transcribes it and types
it into your Claude Code session. Claude answers; a local neural voice
([Kokoro](https://huggingface.co/hexgrad/Kokoro-82M)) reads the first paragraph aloud. Then it
listens again. No cloud speech service, no push-to-talk: **your audio never leaves the Mac**. Only
the transcribed text goes to Claude, exactly as if you'd typed it. (Claude Code's built-in
`/voice` dictation streams audio to the cloud and needs a key press. It also has no spoken replies.)

```
 you ──► mic ──► whisper.cpp (local) ──► Claude Code ──► Stop hook ──► Kokoro (local) ──► you
            ▲                                                                       │
            └─────────── mic stays closed while Claude thinks and speaks ◄──────────┘
```

## Features

- **Hands-free conversation.** A 2.5 s pause sends what you said; the mic closes while Claude
  thinks and speaks (so it never hears itself), then reopens.
- **Wake word without an extra model:** in wake mode only "Claude, …" / "Hey Claude, …" is sent,
  and for 30 s after each reply you can answer without the name.
- **Works in the terminal (iTerm2) and in the Claude desktop app** (Code tab, via `/voice-app`).
- **Several sessions, one mic:** "switch to *project*" moves the mic to another open Claude Code
  session; the voice follows.
- **Short spoken answers.** When you speak, Claude is asked to open with 1–3 plain sentences;
  only the first paragraph is read aloud, with code, tables and links left out.
- **Per-project vocabulary** so Whisper spells your project's words right.
- **Safety:** speech can't become a shell or slash command; nothing is typed into a pane where Claude
  isn't running; the mic turns off after 20 quiet minutes.
- **One-command, self-testing install** that measures your room's noise to set the mic sensitivity.

## Requirements

- macOS (tested on macOS 26, Apple silicon) with [Homebrew](https://brew.sh)
- Claude Code (tested with 2.1.326), in [iTerm2](https://iterm2.com) and/or the Claude desktop app
- A mic (a headset works best). About 500 MB of downloads (150 MB with `--no-kokoro`).

## Quick start

```bash
git clone https://github.com/Standup422/voiceLoop.git ~/Projects/voiceLoop
cd ~/Projects/voiceLoop && ./install.sh
```

The installer installs its tools (whisper-cpp, sox, uv, jq), puts the scripts in `~/.claude/hooks/`,
downloads and checksums the models, writes a default config and runs the test suite. It **never
edits your Claude Code settings**. Instead it prepares the change and prints the command to apply it.
Then give iTerm and/or Claude **Microphone** permission and relaunch the app.
Full walkthrough and troubleshooting: **[docs/INSTALL.md](docs/INSTALL.md)**.

Then, in a Claude Code session:

| Where | Start | Stop |
|---|---|---|
| Claude desktop app | type `/voice-app` (or `/voice-app wake`) | say "stop listening" |
| iTerm2 | `! voice session` then `! voice listen wake` | say "stop listening" |

Other commands: `voice stop` (silence now), `voice off all`, `voice listen status`, `voice status`.

## How it works

Three Python scripts and Claude Code hooks. The mouth and ears use only the standard library;
the neural voice uses `kokoro-onnx`.

- **`voice-speak.py`**, the mouth. A `Stop` hook takes the reply, cleans the markdown for the ear
  and speaks it in the background; a `UserPromptSubmit` hook asks for a short spoken opening and
  stops any speech when you type. It's also the `voice` command.
- **`voice-listen.py`**, the ears. It records with its own endpointing, transcribes with
  whisper.cpp + Silero VAD (about 0.35 s), and types into the iTerm pane by session id, or, in the
  desktop app, hands the text back to the session that is listening for itself.
- **`voice-kokoro.py`** speaks sentence by sentence, so the first words come about 1 s after
  Claude finishes.

Details, settings and the security model: **[docs/HOW-IT-WORKS.md](docs/HOW-IT-WORKS.md)**.

## Privacy

Audio is recorded to a temporary file, transcribed on the Mac and deleted. The transcribed text is
also written to a local log (`$TMPDIR/claude-listen.log`) for troubleshooting. Downloads happen once:
the models (Hugging Face, GitHub) at install, and the voice's Python packages (PyPI) on first use.
After that, speech recognition and the voice run offline. What leaves the Mac is what Claude Code
already sends: your messages, now typed by voice.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
No audio plays. Tests that need real speech use `say` + whisper.cpp and are skipped when the models
aren't installed.

## Credits

[whisper.cpp](https://github.com/ggml-org/whisper.cpp) (MIT) with OpenAI's Whisper `base.en`
model (MIT) · [Silero VAD](https://github.com/snakers4/silero-vad) (MIT) ·
[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) (Apache-2.0) via
[kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (MIT) ·
[SoX](https://sourceforge.net/projects/sox/) · [uv](https://github.com/astral-sh/uv).
The sound cues were synthesized with SoX for this project.

## License

[MIT](LICENSE)
