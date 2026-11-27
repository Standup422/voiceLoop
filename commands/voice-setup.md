---
description: voiceLoop setup as a page in the browser — check, say something, done
argument-hint: ""
allowed-tools: Bash(voice:*), Bash(curl:*)
---

The user typed `/voice-setup` to set up voiceLoop (or to check that it works) with the setup
page: a page in their browser that checks what is missing, lets them say one sentence and hear
the answer, and offers optional fine-tuning. The page is the display; the recording happens on
this Mac, through the app this session runs in, so the test uses the same microphone permission
that talking to you will use.

1. Start it as a **background** command (run_in_background): `voice setup web`
   It opens the page in the browser by itself and prints its address
   (`http://localhost:8446/`). If it says the port is not free, a setup page is already open:
   say so and give that address.
2. Tell the user in one or two short lines: the page is open in their browser, it takes about
   two minutes, and they press **Finish** on the page when done. Then end your turn.

**When the background command ends**, it printed either "finished" (they pressed Finish) or
that nobody used the page. Say in one line that setup is done, and how to start talking: type
`/voice-app` here (or `/voice-app wake` in a shared room).

Things the page may ask the user to do, so you can help if they ask:
- **"Add them for me"** adds voiceLoop's three hook entries to Claude Code's settings (it shows
  what it adds and keeps a backup). Afterwards Claude Code must load them: restart it, or type
  `/hooks` once. Do not edit the settings file yourself.
- **Pure silence** means the app has no microphone permission: System Settings, Privacy and
  Security, Microphone, turn it on for this app, then **quit and reopen the app**.
- **"The microphone is already in use"**: a listener is running. `voice listen off` frees it.

The terminal wizard is still there for people who prefer it: `voice setup`.
