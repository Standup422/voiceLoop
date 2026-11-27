---
description: voiceLoop Talk page — use a browser or a paired phone as the mic and speaker of this session — [pair | phones | off]
argument-hint: "[pair | phones | off]"
allowed-tools: Bash(voice:*), Bash(curl -s -m 2 http://localhost:8445/api/status), Bash(pkill -TERM -f hooks/voice-talk.py)
---

The user typed `/voice-talk $ARGUMENTS` to use voiceLoop's Talk page with THIS session: a
browser on this Mac, or a paired phone on the same network, becomes the microphone and the
speaker. Everything stays on the local network. The server runs only while it is wanted: it is
started by hand (here) and never at login.

**If the argument is `off`:** stop the server (stop the background command you started, or run
`pkill -TERM -f hooks/voice-talk.py`), say "Talk server stopped." in one line, nothing else.

**If the argument is `pair`:** make sure the server is running (see below), then run
`voice talk pair --open`. It opens a QR code in a window. Tell the user in
one line to scan it with the phone within 10 minutes, and that it pairs one phone, once. If it
says there is no certificate, tell the user to run `voice talk setup` first.

**If the argument is `phones`:** run `voice talk phones` and report the list.

**Otherwise (no argument):**
1. Check whether it already runs: `curl -s -m 2 http://localhost:8445/api/status`. If that
   answers, say it is already running (and for which project, from `"project"`), then do
   step 4 and stop.
2. Start it as a **background** command (run_in_background), pinned to this session's project:
   `voice talk --project "$(basename "$PWD")"`
3. Read its first lines of output and tell the user, briefly: the address for this Mac
   (`http://localhost:8445/`), the address for paired phones if it printed one, that one tap on
   the page starts it, and that the server stops when this session ends or with `/voice-talk off`.
4. Show the phone its address as a QR code: if a phone is paired (`"paired"` in the status is
   above 0, or the output said so), run `voice talk qr --open`. It opens the code in a window;
   say in one line that a paired phone can scan it to open the page. It holds only the address,
   no secret. If no phone is paired, say `/voice-talk pair` instead: that QR code pairs the
   phone and opens the page in one scan.

What arrives from the page is typed into this session as an ordinary user message (the user
authorized that by typing this command). It is a transcript, so mis-hearings happen: if it is
garbled, ask briefly; if it asks for something destructive or hard to undo, confirm first.
Open each reply with 1–3 short plain sentences (no markdown, paths or lists in the first
paragraph): that paragraph is what gets spoken on the page.

**When the background command ends** on its own, say "The Talk server stopped." in one line.

While you work, the user can say "stop" (or tap **Stop Claude**) on the page: that presses
Escape in this pane, exactly as if they had pressed it at the desk. Treat it as that: stop, and
wait for what they say next. "Say that again" and **Skip** only replay or cut the audio; you
are not involved.

Limits: one page talks at a time (the one tapped last). The page is deaf while you work and
while the reply plays, except for "stop" and "stop listening". Details: `docs/HOW-IT-WORKS.md` in the voiceLoop repo.
