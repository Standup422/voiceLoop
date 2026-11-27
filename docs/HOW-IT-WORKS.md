# How voiceLoop works

## The pieces

| File | Role |
|---|---|
| `scripts/voice-speak.py` | **The mouth** + the `voice` command. Registered as Claude Code's `Stop`, `UserPromptSubmit` and `Notification` hooks. |
| `scripts/voice-listen.py` | **The ears.** Records, transcribes, decides what to send, and sends it. |
| `scripts/voice-kokoro.py` | Neural voice (Kokoro), sentence by sentence. Falls back to macOS `say`. |
| `scripts/voice-setup.py` | **The setup page** (`/voice-setup`, `voice setup web`): check, say something, done. The page is the display; the recording happens on the Mac, with the same microphone permission the voice loop uses. |
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

The `UserPromptSubmit` hook does two things: it stops the session's own speech if it is still playing when you send a message,
and when the message came from the listener, it adds a note asking Claude to open with 1–3 plain
sentences.

**One voice at a time.** A session's new reply replaces its own older speech. A reply from a
*different* session never cuts the speaker off: it waits in a line (`$TMPDIR/claude-voice.queue/`,
one waiting reply per session, the newest) and a small detached helper speaks the line in order
once the speaker has finished. The hook itself still returns at once. A reply that has waited
more than 60 seconds is dropped, since you have read it by then. When a different session spoke
last, a reply starts with "From *project*:". `voice stop` silences the speaker and empties the
line; sending a message in a session stops that session's own speech and drops its waiting reply,
and leaves another session's reply alone.

The `Notification` hook (matcher `permission_prompt|elicitation_dialog`) says one fixed line when
Claude is waiting on you: "Claude is asking for permission in *project*." Claude Code sends it
once the prompt has waited a few seconds. It speaks only where the session's voice is on, and
never reads the notification's own text aloud (that can name a command or a file).

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
- **"Hang on":** an utterance that ends with "hang on", "hold on", "one moment", "one second",
  "give me a second" or "let me think" is held instead of sent (the same mechanism as a sentence
  ending on "and…"), and what follows needs no wake word. Inside a sentence the words are just words.
- **Transcription:** `whisper-cli` with the `small.en` model (about 0.5–0.9 s per sentence on
  Apple silicon; in a test on real speech it got 2 of 44 words wrong, against 6 for `base.en`) plus
  **Silero VAD**. `./install.sh --whisper=base` uses the smaller, faster `base.en` instead. The VAD drops
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

Whisper hears a lone "Claude" differently for different voices and mics ("Cloud", "Bot", "Pod").
`voice setup` has you say it five times and offers to accept the spellings it heard as
`wake_words` **on that Mac**. Like "Cloud", they count only at the start of a sentence and
followed by punctuation, so "Pod racing is fun" stays an ordinary sentence.

### Local commands (never sent to Claude)

"switch to *project*", "which projects", "voice help", "stop listening".

### Spoken slash commands, on purpose

A `/` that happens to appear in a transcript never runs anything (it's stripped). To run a
command deliberately, say **"command *name*"** or **"slash *name*"**: "Claude, command compact",
"command wrap session". Only names in the `commands` allowlist run; anything else is sent to
Claude as an ordinary sentence. The default list is the built-ins `clear` and `compact`.
`clear` asks "Clear this conversation? Say yes." and runs only if your next words start with
yes (silence or anything else cancels). Add your own skills to the list:

```json
"commands": {"clear": {"confirm": true, "reply": false, "ask": "Clear this conversation?"},
             "compact": {"reply": false}, "my-skill": {}, "deploy": {"confirm": true}}
```

`reply: false` is for commands Claude doesn't answer, so the mic doesn't wait for a reply. Shell
commands (`!`) can never be spoken. In the desktop app, Claude can run skills from the list for
you, but built-ins like `/clear` have to be typed.

### Several sessions, one mic

Every `Stop` hook writes a small registry entry: project folder name → iTerm session id. "switch to
*name*" matches loosely ("voice loop" finds `voiceLoop`), types into that pane from then on, and
moves the speaking to that session.

## The desktop app: the session is its own ears

The Claude desktop app has no supported way for an outside program to type into a running
session. So the session listens **for itself**. `/voice-app` makes it run

```bash
voice listen once --json      # (--wake in a shared room); gives up after idle_minutes
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
    "device": "",             // mic: "" = the macOS default input, or an input's exact name
                              // (pick it in `voice setup`); unplugged = default until it's back
    "threshold": "0.5%",      // loudness that counts as sound (install.sh measures your room)
    "silence_seconds": 2.5,   // quiet this long = you're done
    "wake": false,            // true = always use the wake word
    "follow_up_seconds": 30,
    "wake_words": [],         // extra spellings of "Claude", learned by `voice setup`
    "commands": {"clear": {"confirm": true, "reply": false}, "compact": {"reply": false}},
    "idle_minutes": 20,       // mic off after this long with nothing sent
    "quiet_threshold": "",    // threshold while `voice quiet on` ("" = 0.6 x threshold)
    "snippets": {},           // say "snippet <name>" to send the stored text
    "log_hours": 24,          // the log of what you said is set aside after this long, then deleted (0 = keep)
    "prompt": "Claude, Claude Code, iTerm, GitHub, …",   // vocabulary hint for Whisper
    "replacements": {"claude code": "Claude Code"},     // spelling fixes, applied after
    "sounds": {"listening": "Tink", "accepted": "Purr", "ready": "", "stopped": "Basso"},
    "sound_volume": 0.3,
    "sound_volumes": {},      // per-cue override, e.g. {"listening": 0.6}
    // advanced: "preroll": 0.5, "max_seconds": 60, "cooldown": 0.6, "reply_timeout": 600,
    // "pane_check_seconds": 60, "stop_phrases": ["stop listening"], "confirm_seconds": 8,
    // "model", "vad_model"
  }
}
```

Sounds are macOS system sound names or paths to audio files. The installer sets `ready` and
`listening` to the two tones in `assets/sounds/`, plays `listening` at 0.6, and sets the
`threshold` from a 2-second measurement of your room.

**Your word list:** `~/.claude/voice/words.txt` holds the names and terms Whisper should
spell right, one per line. `heard = Right` also fixes a spelling. Manage it with:

```bash
voice words                          # show it, and how much of the hint it uses
voice words add Zephyrine, billbook  # add words (comma-separated)
voice words fix cube control = kubectl
voice words remove billbook
```

It's re-read on every sentence. Whisper's vocabulary hint holds about 600 characters: project
words first, then your list, then the config's `prompt`. `voice words` warns when it's full.

**Per-project vocabulary:** put a `.claude/voice.json` in a project (see
[`examples/voice.json`](../examples/voice.json)). When the listener targets that project, its
words go first in Whisper's prompt and its replacements win. It's re-read on every utterance.

**Vocabulary from the project itself:** `voice words scan` (in a project folder, or
`voice words scan <folder>`) collects the names Whisper is likely to spell wrong: the folder
name, file names, names defined in the code (`def`, `class`, `function`, `const` …) and words
in the docs that no dictionary knows. It keeps mixed-case names (`useEffect`) and unknown words
(`Kokoro`), skips tests, `node_modules` and hidden folders, and writes at most 300 characters
of them to `.claude/voice.json` as `"scanned"`. Your own `"vocabulary"` there is never touched
and comes first. `--dry` only shows the list. It is a rule of thumb, not a judgement: remove
names you never say, and scan again after big changes.

**Spoken snippets:** store a text once, then say **"snippet *name*"** to send it:

```bash
voice snippets add standup = Summarise what changed in this project since yesterday.
voice snippets                       # list them
voice snippets remove standup
```

"Claude, snippet standup" sends the text; it can also sit inside a sentence. The word "snippet"
is required, so a name that is an ordinary word never fires by accident. A project can carry its
own in `.claude/voice.json` (`"snippets": {"name": "text"}`); they win over yours. A snippet is
sent as one line and, like all speech, can never start a shell or slash command.

**A soft voice:** `voice quiet on` lowers the mic threshold (to 0.6 × the normal one, or to
`quiet_threshold` if you set it) for talking quietly; `voice quiet off` restores it. It applies
from the next recording, with no restart. If a recording never ends, the room is louder than the
quiet threshold: set `quiet_threshold` a little higher. Whisper is less accurate on very soft
speech, so try it with your own voice first.

**Benchmark, no microphone:** `voice benchmark` has macOS `say` speak known sentences into WAV
files (the built-in ones plus one per word in your vocabulary; with Kokoro installed, two of its
neural voices read them too, `kokoro:af_heart` and `kokoro:bm_george`), runs every installed Whisper
model on them with the word list **off** (plain Whisper) and **on** (what the listener sends),
and scores the result:

```text
model       word list   accuracy    wake  s/sentence
base.en     off            86.5%    8/22        0.33
base.en     on             97.2%   18/22        0.31
small.en    off            87.4%    9/22        0.56
small.en    on             99.2%   22/22        0.57
```

Accuracy is 1 − word error rate (wrong, missing and extra words ÷ words said). "wake" counts
how often a "Claude, …" sentence was recognised as addressed to Claude. Below the table it lists
the words Whisper got wrong: add those with `voice words add`. Options: `--models base,small`,
`--noise [0.02]` (mixes in pink noise), `--quick` (built-in sentences, one voice), `--show`
(every wrong sentence, said vs heard), `--json`. Computer voices are cleaner than a person in a
room, so use it to compare models and find missing words, not as your own accuracy.

## The Talk page: a browser or a phone as microphone and speaker

`voice talk` (or `/voice-talk` in a session) starts a small web server, `voice-talk.py`. Its page
streams the microphone to the Mac; the Mac runs the audio through the **same** endpointing,
Whisper, word list and gate as the desk listener, and delivers the text into the Claude session
that is active at the desk. The reply comes back as sound, one sentence at a time.

```
page: mic -> 16 kHz PCM, a chunk every 200 ms  --POST-->  voice-talk.py -> Whisper -> gate
page: plays the reply                          <--WAVs--  reply text from the Stop hook -> Kokoro / say
page: state, what was heard                    <--events- (server-sent events)
```

- **Two doors.** `http://localhost:8445/` for a browser on the Mac (always). `https://<mac>.local:8444/`
  for phones, only after `voice talk setup` has made a certificate. A phone's browser gives a page
  the microphone only over HTTPS it trusts, so the phone must trust your Mac's own certificate
  authority (`voice talk ca` shows how).
- **Two QR codes.** `voice talk qr` is only the page's address, so a paired phone never has to
  type it; it lets nobody in. The pairing code below is the one that holds a secret.
- **Pairing.** `voice talk pair` shows a QR code with a one-time secret (10 minutes, one phone).
  The phone trades it for a long random token in a cookie. Only hashes are stored on the Mac
  (`~/.local/share/voiceloop/talk/phones.json`). From the network, nothing but a text-free status
  is served without the token. `voice talk unpair <name>` ends a phone's access at once.
- **Which session.** First match: the session the desk listener points at; a session waiting in
  `voice listen once`; else the session that replied last. Once a conversation has a session it
  stays there (the page shows "Talking to: <project>"). `--project <name>` picks the start.
- **Delivery.** Into an iTerm pane the text is typed, after the same "is Claude still running
  there?" check as at the desk. A session in app mode gets it through `voice listen once`.
- **The reply.** For a turn that came from the page, the Stop hook hands the reply text to the
  Talk server and stays quiet on the Mac. If the page has gone, the Mac speaks it: a reply is
  never dropped.
- **One microphone at a time.** While a page streams, the desk microphone rests. With several
  pages open, the one tapped last talks.
- **Half-duplex.** Nothing is sent while Claude works or while the reply plays. While Claude
  works the page still listens for two things, "stop listening" and "stop"; anything else is
  shown as "Claude is still working" and dropped, so talk in the room can't queue up messages.
- **Stop Claude.** "Stop" as a whole utterance ("Claude, stop" in wake mode), or the page's
  button, presses Escape in the pane Claude is working in. It can only interrupt: one fixed key,
  never text, only while the page waits for a reply, and only into a pane that still runs
  Claude. A session in the desktop app has no pane, so it can't be stopped from the page.
  Claude Code runs no Stop hook for an interrupted turn, so the page stops waiting by itself.
- **Skip and "say that again".** The page can cut a reply short, and replay the last one (the
  text is kept in memory and made into audio again; no audio is stored). Neither starts a turn.
- **Claude is waiting on you.** During a phone turn the `Notification` hook's line is played
  on the page, and the page goes on waiting for the reply. The answer is given at the desk.
- **Settings:** `"talk": {"port": 8445, "tls_port": 8444, "threshold": ""}` in `config.json`
  (`threshold`: the phone is a different microphone, so it can have its own).

Security notes for the Talk page:

- A paired phone can type into a live Claude session. That is the feature, and the reason for
  pairing and HTTPS. Permission prompts still appear on the Mac.
- Requests are checked against the `Host` and `Origin` headers, so a web page open in another
  browser tab can't post audio to the server on `localhost`.
- The certificate authority's key (`mkcert -CAROOT`) can vouch for any site to a phone that
  trusts it. It never leaves your Mac; every user makes their own.
- No audio is kept. Heard text goes to the page and the local log, never to `/api/status`.

## Security model

- **Anyone within earshot can talk to Claude** while the listener runs: a person, or a video
  saying "Hey Claude, …". Use wake mode in shared spaces, and don't combine listening with
  permission-bypass modes. voiceLoop types messages, and doesn't grant anything a typed message
  wouldn't get, so Claude Code's permission prompts remain your safety net.
- What you say is logged in plain text to `$TMPDIR/claude-listen.log` (your user's temp folder) for
  troubleshooting. After `log_hours` (24) the log is set aside as `claude-listen.log.old`, and
  that file is deleted a further 24 hours after its last line, so text is kept for two days at
  most (`"log_hours": 0` keeps everything). The `voice status --json` output deliberately
  contains no text.
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
- Two voiced sessions that finish in the very same instant can still overlap (see "One voice at
  a time" above: the line is checked when a reply arrives, not locked).
