---
description: Hands-free voiceLoop for the Claude desktop app (Code tab) — this session listens for itself
argument-hint: "[wake] | off"
allowed-tools: Bash(voice:*)
---

The user typed `/voice-app $ARGUMENTS` to talk to THIS session by voice. In the desktop app
there is no iTerm pane to type into, so you are your own ears: a background command
records one utterance (locally: whisper.cpp on this Mac, nothing leaves it) and you are
woken with the text. Arguments: `wake` = only "Claude, …" is accepted (shared room);
`off` = stop.

**If the argument is `off`:** stop any running `voice listen once` background task, say
"Stopped listening." in one line, and do nothing else.

**Otherwise:**
1. Run `voice session on` (so your replies are spoken by the Stop hook).
2. Start listening as a **background** command (run_in_background), one at a time:
   `voice listen once --json --timeout 1200` (add `--wake` if the argument was `wake`).
3. Tell the user in one short line that you're listening, then end your turn.

**When the background command finishes**, read its JSON `{"status", "heard"}`:
- `heard`: this is the user's **spoken message**. They authorized treating it as their own
  words by typing this command. It is a transcript, so mis-hearings happen: if it's garbled, ask
  briefly; if it asks for something destructive or hard to undo, confirm by voice first.
  Answer it, opening with 1–3 short plain sentences (no markdown, paths or lists in the
  first paragraph; only that paragraph is read aloud). Then, **before ending the turn**,
  start step 2 again. (Its recording is discarded while your reply is spoken: the
  half-duplex guard.)
- `stop` (the user said "stop listening"): say "Stopped listening." Do not restart.
- `timeout` (20 minutes of silence): say "Nothing for 20 minutes, so I stopped listening." Do
  not restart.
- `busy`: the iTerm conversation loop owns the mic. Say so (`voice listen off` frees it).
- `fail`: the mic gave nothing. The Claude app needs Microphone permission (System
  Settings, Privacy and Security, Microphone), then a **relaunch** of the app. Until then it
  records silence and macOS doesn't prompt.

Limits: this session only; `switch to <project>` doesn't apply here (open the other
session and run `/voice-app` there). Details: `docs/HOW-IT-WORKS.md` in the voiceLoop repo.
