# Installing voiceLoop

Talk to Claude Code hands-free and hear it answer. Speech recognition (whisper.cpp) and the voice
(Kokoro) run **on your Mac**. Your audio never leaves it; only the text of what you said goes to
Claude, the same as if you'd typed it.

About 15 minutes, most of it downloads (about 850 MB; 500 MB without the neural voice).

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
- downloads the speech models (about 1 GB) and checks each one's checksum. Setting up several
  Macs? Put the models on a network share once, laid out like `~/.local/share/voiceloop`
  (`models/`, `kokoro/`), and run `VOICELOOP_MIRROR=/path/to/that/folder ./install.sh`: it copies
  from there instead and still checks every checksum;
- **listens to your room for 2 seconds** (stay quiet) to set the mic sensitivity;
- writes a default config and **runs its own tests**.

It ends with `Result: N ok, N need attention` and a short **"Still for you"** list. Safe to run again
at any time: it skips everything that's already right.

It runs `brew install` for missing tools without asking (use `--check` first to see what's
missing). Kokoro's first use also has `uv` download its Python packages (pinned versions) from
PyPI, and Python 3.13 if you don't have it.

Options: `--whisper=base` (the 148 MB `base.en` speech model instead of `small.en`: faster and
smaller, but more mistakes on names and jargon; for older Macs or a tight disk), `--no-kokoro` (skip the 350 MB neural voice and use the built-in macOS voice),
`--check` (test only, change nothing; it does open the mic for 2 seconds to measure the room),
`--no-measure` (skip that room measurement), `--help`.

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
it on for every app you'll talk through (**iTerm**, **Terminal** and/or **Claude**), then
**quit and reopen that app**.

> ⚠️ Without this the mic records pure silence and macOS **does not ask**. If voiceLoop never
> hears you, check this first.

**d) (iTerm only)** The first time voiceLoop types into your Claude session, macOS asks to let it
control iTerm (Automation). Click **OK**.

**e) Run the setup wizard** in a terminal window (not inside a Claude session: it needs your
keyboard). The terminal app needs Microphone permission too. **Using only the Claude app?**
Open its **Terminal panel** and run it there: it uses the Claude app's permission.
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
- Names it keeps getting wrong? `voice words add Zephyrine`, or `voice words fix cube control = kubectl`.
  Then `voice benchmark` (no mic needed) shows which words Whisper still misses.

### From your phone (optional)

A page on your phone can be the microphone and the speaker of the Claude session that is open
on your Mac. You talk from the couch, the Mac does the work, and the answer plays on the phone.
Nothing leaves your network. It is off until you start it.

**Try it on the Mac first** (no setup needed): in a Claude session type `/voice-talk`, open
`http://localhost:8445/` in a browser on the Mac, tap once and talk. What you say is typed into
that session; the reply plays in the page.

**Then the phone** (once per Mac, then once per phone):

```bash
brew install mkcert qrencode
voice talk setup     # makes an HTTPS certificate for this Mac (no sudo)
voice talk ca        # QR code + steps: the phone learns to trust this Mac
```

A phone's browser only gives a page the microphone over HTTPS it trusts, so the phone has to
trust your Mac's own certificate authority. `ca` shows how; on an iPhone the step that counts is
the last one (Settings > General > About > Certificate Trust Settings).

Now pair the phone. Pairing is what lets it in: being on your Wi-Fi is not enough.

1. In a Claude session: `/voice-talk` (starts the server), then `/voice-talk pair`.
2. Scan the QR code with the phone within 10 minutes. It works once, for one phone.
3. Tap once on the page, and talk. Add the page to the home screen for next time.

| Command | What it does |
|---|---|
| `/voice-talk` | start the server for this session's project (it stops when the session ends) |
| `/voice-talk pair` | QR code to pair one more phone |
| `/voice-talk phones` | list paired phones |
| `/voice-talk off` | stop the server |
| `voice talk unpair <name>` | forget a phone at once (`all` forgets every phone) |

How it behaves:

- **One microphone at a time.** While a page is open and listening, the Mac's microphone rests.
  The page you tapped last is the one that talks.
- **It stays with one session**, shown on the page as "Talking to: <project>".
- **Wake word:** the same as at the desk. If the session you talk to listens in wake mode, the
  phone needs "Claude, …" too.
- **The page must stay in front.** A phone browser stops the microphone when the page is
  hidden or the phone locks; the page keeps the screen on while it listens. Tap once to start again.
- **While Claude works, and while the reply plays, the page does not listen.** A beep tells
  you it's your turn.
- **Permission prompts appear on the Mac,** not on the phone.
- Say "stop listening" to end it.

> **Keep the certificate authority to yourself.** `mkcert -CAROOT` shows a folder with a
> `rootCA-key.pem`. Whoever has that file can pose as any website to a phone that trusts it.
> It never leaves your Mac: don't copy it, share it, or back it up to a shared place. Everyone
> makes their own. To undo the trust on a phone, remove the profile there.

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
| It hears the wrong mic (a webcam, AirPods) | Run `voice setup` and pick your mic in step 1; it stays chosen even when macOS switches the default. Then let the setup measure the room again: every mic needs its own threshold. |
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
rm -f ~/.claude/hooks/voice-{speak,listen,kokoro,talk}.py ~/.local/bin/voice \
      ~/.claude/commands/voice-{app,loop,talk}.md
rm -rf ~/.local/share/voiceloop ~/.claude/voice
```
Then remove the two `voice-speak.py` entries (under `Stop` and `UserPromptSubmit`) from
`~/.claude/settings.json`. If nothing else has changed there since you installed, restore the backup the
installer had you make (if you had a settings file before): `cp ~/.claude/settings.json.bak-voiceloop ~/.claude/settings.json`.

---

**What leaves your Mac:** only the text of what you said, sent to Claude like a typed message.
Audio is recorded to a temporary file, transcribed locally and deleted. The models download once
from Hugging Face and GitHub, and then everything runs offline.
