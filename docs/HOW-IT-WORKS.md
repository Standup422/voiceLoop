# How voiceLoop works

## The pieces

| File | Role |
|---|---|
| `scripts/voice-speak.py` | **The mouth** + the `voice` command. Registered as Claude Code's `Stop` and `UserPromptSubmit` hooks. |
| `scripts/voice-listen.py` | **The ears.** Records, transcribes, decides what to send, and sends it. |
| `scripts/voice-kokoro.py` | Neural voice (Kokoro), sentence by sentence. Falls back to macOS `say`. |
| `commands/voice-app.md` | The `/voice-app` slash command (desktop app mode). |
| `install.sh` | Idempotent installer and self-check (`--check`). |

Installed copies live in `~/.claude/hooks/`; `~/.local/bin/voice` is a symlink to
`voice-speak.py`. Settings: `~/.claude/voice/config.json`. Models: `~/.local/share/voiceloop/`.
Live state (logs, PID files) lives in the per-user temp dir (`$TMPDIR`).

## The mouth: a Stop hook that gets out of the way

`Stop` fires when Claude finishes a turn, and its payload's `last_assistant_message` holds the reply.
(The transcript file is written about 200 ms *later*, so the hook doesn't read it.) The hook:

1. records which iTerm pane and project this session lives in (for "switch to");
2. exits unless speech is on (`voice on` = everywhere; `voice session` = this session only);
3. **cleans the markdown for the ear**: code blocks become "Code block omitted.", tables
   "Table omitted.", URLs "a link", `path/to/file.py:12` becomes "file.py", list items get pauses;
4. keeps the **first paragraph** (a lead-in ending in ":" is joined to what follows);
5. starts the speech **detached**, with output to `/dev/null`, and returns in about 50 ms.

> If speech inherited the hook's output pipe, Claude Code would wait for the pipe to close,
> which means it would wait for the speech to end. Detaching lets the turn end at once.

The `UserPromptSubmit` hook does two things: it stops speech still playing when you send a message,
and when the message came from the listener, it adds a note asking Claude to open with 1–3 plain
sentences.

## The ears: half-duplex conversation (iTerm2)

`voice listen` (or `voice listen wake`) starts `voice-listen.py` in the background:

```
record ─► transcribe ─► clean + decide ─► type into the pane ─► mic closed until the reply
  ▲          (local)                         (iTerm, by           has been spoken ─┐
  └──────────────────────────────────────────  session id)  ◄──────────────────────┘
```

- **Recording:** its own endpointing on 30 ms frames. A recording starts when the loudness passes the
  `threshold`, keeps 0.5 s of *pre-roll* (so soft first syllables aren't clipped), and ends after
  `silence_seconds` (2.5) of quiet. The installer measures your room to pick the threshold.
- **Transcription:** `whisper-cli` with the `base.en` model and **Silero VAD**. The VAD drops
  non-speech, which stops Whisper inventing words from silence. A stop list catches the classic
  inventions ("thanks for watching").
- **Half-duplex:** the mic is closed while Claude thinks and speaks. A recording is also thrown
  away if speech starts while it's running. Otherwise it would transcribe Claude's own voice and send it
  back.
- **Thinking pauses:** text ending in "and", "so", "because", "um" … is held and joined to what
  you say next.
- **Sending:** AppleScript `write text` into the target iTerm session by its unique id, then a
  separate Enter. The text is passed as an argument, never spliced into the script.

### Wake word, without a wake-word model

Whisper already transcribes everything locally, so the wake word is a check on the **text**:
"Claude", "Hey Claude", and common mis-hearings ("Claud", "Clod"; "Cloud" only when punctuation
follows it, since "cloud" is a real word). For `follow_up_seconds` (30) after any spoken reply,
no name is needed. Saying just "Claude?" gets a "Yes?". All of this lives in one pure class
(`Gate`) with unit tests.

### Local commands (never sent to Claude)

"switch to *project*", "which projects", "voice help", "stop listening".

### Several sessions, one mic

Every `Stop` hook writes a small registry entry: project folder name → iTerm session id. "switch to
*name*" matches loosely ("voice loop" finds `voiceLoop`), types into that pane from then on, and
moves the speaking to that session.

## The desktop app: the session is its own ears

The Claude desktop app has no supported way for an outside program to type into a running
session. So the session listens **for itself**. `/voice-app` makes it run

```bash
voice listen once --json --timeout 1200      # (--wake in a shared room)
```

as a **background command** and end its turn. Claude Code wakes the session when a background
command finishes. The session gets `{"status": "heard", "heard": "…"}`, answers (the Stop hook
speaks), and starts listening again before ending the turn. Statuses: `heard`, `stop`
("stop listening"), `timeout`, `busy` (the iTerm listener already owns the mic) and `fail` (no
mic access).

> The heard text arrives as tool output, which Claude treats as data, not instructions. Typing
> `/voice-app` is what tells the session to treat that command's output as your words. Destructive or
> hard-to-undo requests still get a spoken confirmation first.

## Settings

`~/.claude/voice/config.json`. Everything is optional; these are the defaults. (The comments
are only here for explanation: the real file is plain JSON.)

```jsonc
{
  "engine": "say",            // say | kokoro | none   (the installer sets "kokoro")
  "voice": "",                // macOS say voice, e.g. "Samantha" ("" = system default)
  "rate": 200,                // say: words per minute
  "kokoro_voice": "bf_emma",  // Kokoro voice: bf_/bm_ British, af_/am_ American
  "kokoro_speed": 1.0,
  "mode": "first_paragraph",  // or "full"
  "max_chars": 600,
  "listen": {
    "threshold": "0.5%",      // loudness that counts as sound (install.sh measures your room)
    "silence_seconds": 2.5,   // quiet this long = you're done
    "wake": false,            // true = always use the wake word
    "follow_up_seconds": 30,
    "idle_minutes": 20,       // mic off after this long with nothing sent
    "prompt": "Claude, Claude Code, iTerm, GitHub, …",   // vocabulary hint for Whisper
    "replacements": {"claude code": "Claude Code"},     // spelling fixes, applied after
    "sounds": {"listening": "Tink", "accepted": "Purr", "ready": "", "stopped": "Basso"},
    "sound_volume": 0.3,
    "sound_volumes": {},      // per-cue override, e.g. {"listening": 0.6}
    // advanced: "preroll": 0.5, "max_seconds": 60, "cooldown": 0.6, "reply_timeout": 600,
    // "pane_check_seconds": 60, "stop_phrases": ["stop listening"], "model", "vad_model"
  }
}
```

Sounds are macOS system sound names or paths to audio files. The installer sets `ready` and
`listening` to the two tones in `assets/sounds/`, plays `listening` at 0.6, and sets the
`threshold` from a 2-second measurement of your room.

**Per-project vocabulary:** put a `.claude/voice.json` in a project (see
[`examples/voice.json`](../examples/voice.json)). When the listener targets that project, its
words go first in Whisper's prompt and its replacements win. It's re-read on every utterance.

## Security model

- **Anyone within earshot can talk to Claude** while the listener runs: a person, or a video
  saying "Hey Claude, …". Use wake mode in shared spaces, and don't combine listening with
  permission-bypass modes. voiceLoop types messages, and doesn't grant anything a typed message
  wouldn't get, so Claude Code's permission prompts remain your safety net.
- What you say is logged in plain text to `$TMPDIR/claude-listen.log` (your user's temp folder) for
  troubleshooting. The `voice status --json` output deliberately contains no text.
- Leading `!`, `/` and `#` are stripped, also after the wake word ("Claude, /clear" sends
  "clear"), so speech can't become a shell command, slash command or memory note.
- Before **every** send (and every 60 s while idle) the listener checks that Claude is still running
  in the target pane. If Claude has exited, the pane is a plain shell, and typed speech plus Enter
  would run as a command.
- The installer never edits `~/.claude/settings.json`; it prepares a merged copy for you to review.

## Limits

- macOS only. The terminal mode needs iTerm2 (it relies on iTerm's AppleScript); the desktop app
  works via `/voice-app`.
- English (`base.en`). Other languages would need a multilingual Whisper model.
- In the desktop app, voice reaches only the session that ran `/voice-app` (no "switch to").
- One "last spoken" marker is shared by all sessions: if two voiced sessions finish at the same time,
  the newer reply cuts off the older one.
