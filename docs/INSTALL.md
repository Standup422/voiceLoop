# Installing voiceLoop

Talk to Claude Code hands-free and hear it answer. Speech recognition (whisper.cpp) and the voice
(Kokoro) run **on your Mac**. Your audio never leaves it; only the text of what you said goes to
Claude, the same as if you'd typed it.

About 15 minutes, most of it downloads (about 500 MB, or 150 MB without the neural voice).

---

## 1. What you need

- A Mac with a recent macOS (tested on macOS 26, Apple silicon).
- [Homebrew](https://brew.sh). Check: `brew --version`.
- [Claude Code](https://claude.com/claude-code), in the terminal and/or the Claude desktop app.
- **iTerm2** if you want hands-free in the terminal (`brew install --cask iterm2`). The regular
  Terminal app can speak replies but can't be listened into. The Claude desktop app works without iTerm.
- A headset or a decent mic. A headset works best: the mic doesn't hear the speakers.

## 2. Get the code

```bash
git clone https://github.com/Standup422/voiceLoop.git ~/Projects/voiceLoop
```
Any folder works; `~/Projects/voiceLoop` is just the example used below.

## 3. Install

```bash
cd ~/Projects/voiceLoop
./install.sh
```

The installer:
- installs the tools it needs (whisper-cpp, sox, uv, jq);
- copies the scripts into `~/.claude/hooks/` and the `voice` command into `~/.local/bin/`;
- adds the slash commands `/voice-loop` and `/voice-app`;
- downloads the speech models and checks each one's checksum;
- **listens to your room for 2 seconds** (stay quiet) to set the mic sensitivity;
- writes a default config and **runs its own tests**.

It ends with `Result: N ok, N need attention` and a short **"Still for you"** list. Safe to run again
at any time: it skips everything that's already right.

It runs `brew install` for missing tools without asking (use `--check` first to see what's
missing). Kokoro's first use also has `uv` download its Python packages (pinned versions) from
PyPI, and Python 3.13 if you don't have it.

Options: `--no-kokoro` (skip the 350 MB neural voice and use the built-in macOS voice),
`--check` (test only, change nothing), `--help`.

## 4. The steps the installer leaves to you

**a) Register the hooks.** The installer never edits your Claude Code settings itself. If it
reports `hooks not registered`, its "Still for you" list shows what to do:
- with an existing settings file: a `diff` to review (it should show only two added hook
  entries), then one line that backs up your settings and copies in the prepared version;
- with no settings file yet: one `cp`.

Then restart Claude Code.

**b) If it says `~/.local/bin` isn't on your PATH:** add this line to `~/.zshrc` and open a new
terminal tab:
```bash
export PATH="$HOME/.local/bin:$PATH"
```

**c) Microphone permission.** In System Settings → Privacy & Security → **Microphone**, turn
it on for **iTerm** and/or **Claude**, then **quit and reopen that app**.

> ⚠️ Without this the mic records pure silence and macOS **does not ask**. If voiceLoop never
> hears you, check this first.

**d) (iTerm only)** The first time voiceLoop types into your Claude session, macOS asks to let it
control iTerm (Automation). Click **OK**.

**e) Run the setup wizard** in a terminal tab (not inside Claude Code: it needs your keyboard):
```bash
voice setup
```
About 3 minutes:
1. A mic check with a live level meter.
2. A room-noise measurement.
3. **Wake-word training:** you say "Claude" five times, and it learns how Whisper hears *your*
   voice ("Cloud", "Bot", …).
4. A speak-and-hear test.
5. The main settings, each explained.
6. A cheat sheet.

Nothing is saved without a yes, and your config is backed up first.

Check that everything's green:
```bash
cd ~/Projects/voiceLoop && ./install.sh --check
```

## 5. Use it

### In the Claude desktop app (Code tab)
Type **`/voice-app`** in the session. The session listens for itself; just talk.
- In a shared room, type `/voice-app wake` instead: only sentences starting with "Claude" or
  "Hey Claude" are taken.
- To stop, say **"stop listening"**, or type `/voice-app off`. It also stops after 20 quiet minutes.

### In the terminal (Claude Code in iTerm)
In a Claude Code session:
```bash
! voice session         # read this session's replies aloud
! voice listen wake     # hands-free, wake word "Claude" / "Hey Claude"
```
(or `voice listen` with no wake word, if you're alone). Then talk. A 2.5-second pause sends what you said.
- "**switch to** *project*" moves the mic to another open Claude session; "**which projects**"
  lists them. Projects show up after they've finished one reply.
- "**command** *name*" runs an allowed slash command: "command compact", or "command clear",
  which asks you to say yes first. Add your own skills to `"commands"` in the config.
- "**stop listening**" turns the mic off.

### Good to know
- **Sounds:** a soft **pluck** means it's listening, a **purr** means your message was sent, and a
  **chime** means it's your turn again.
- Only the **first paragraph** of each reply is read aloud; the rest is on screen.
- **"Hey Claude"** is recognized more reliably than "Claude" on its own.
- After any reply, you have **30 seconds** to answer without saying the name.
- Typing in the session stops the voice. Other commands: `voice stop` (silence now), `voice off all` (all
  speaking off), `voice listen off`, `voice status`.

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| It never hears me | Microphone permission + **relaunch** the app (step 4c). Look at `voice listen status`. |
| It never stops recording | The room is louder than the sensitivity setting. Re-run `./install.sh` in a quiet moment, or raise `"threshold"` (e.g. `"1%"`) under `"listen"` in `~/.claude/voice/config.json`. |
| It ignores me in wake mode | Say **"Hey Claude, …"**. `voice listen status` shows what it heard. |
| No voice at all | `voice status`: is it on for this session? `voice session` turns it on. Were the hooks registered (step 4a), and was Claude Code restarted? |
| `voice: command not found` | Step 4b (PATH). |
| `/voice-app` says "busy" | The terminal listener owns the mic: run `voice listen off`. |
| Something else | Run `./install.sh --check` and open an issue with its output. |

## 7. Update

```bash
cd ~/Projects/voiceLoop && git pull && ./install.sh
```

## 8. Uninstall

```bash
voice listen off; voice off all
rm -f ~/.claude/hooks/voice-{speak,listen,kokoro}.py ~/.local/bin/voice \
      ~/.claude/commands/voice-{app,loop}.md
rm -rf ~/.local/share/voiceloop ~/.claude/voice
```
Then remove the two `voice-speak.py` entries (under `Stop` and `UserPromptSubmit`) from
`~/.claude/settings.json`. If nothing else has changed there since you installed, restore the backup the
installer had you make (if you had a settings file before): `cp ~/.claude/settings.json.bak-voiceloop ~/.claude/settings.json`.

---

**What leaves your Mac:** only the text of what you said, sent to Claude like a typed message.
Audio is recorded to a temporary file, transcribed locally and deleted. The models download once
from Hugging Face and GitHub, and then everything runs offline.
