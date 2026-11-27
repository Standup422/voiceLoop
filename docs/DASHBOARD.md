# Add voiceLoop to your own dashboard

voiceLoop has no dashboard of its own. It gives you one command that reports its whole state
and a handful of commands that change it, so any dashboard can show a voice card: a web page you
wrote, an Obsidian note, a status bar, or a page that a Claude session maintains for you.

A complete working example is in [`examples/voice-dashboard.py`](../examples/voice-dashboard.py)
(one file, Python standard library). Run it, look at it, then build the same thing where you
want it:

```bash
python3 examples/voice-dashboard.py      # then open http://localhost:8790/
```

> **Asking Claude to build it?** Point the session at this file and the example: "Read
> docs/DASHBOARD.md and examples/voice-dashboard.py in the voiceLoop folder, then add a voice
> card to my dashboard." Everything it needs is on this page.

## Reading the state: one command

```bash
voice status --json
```

It prints one JSON object. It **never contains text**: not what you said, not what Claude
answered. Only names, times and outcomes. That is deliberate, because a dashboard is often
visible to other people.

```json
{
  "listening": { "on": true, "mode": "wake", "target": "myProject",
                 "last": { "at": "15:50:43", "kind": "sent" } },
  "speaking":  { "global": false, "sessions": ["myProject"], "engine": "kokoro", "voice": "bf_emma",
                 "playingNow": false, "lastSpokenAt": "2026-11-27T15:50:11", "lastLatency": "1.55s",
                 "speaker": null, "waiting": [] },
  "projects":  ["myProject", "otherProject"],
  "hooks":     { "stop": true, "userPromptSubmit": true, "notification": true },
  "commands":  [ { "name": "clear", "confirm": true }, { "name": "compact", "confirm": false } ]
}
```

| Field | Meaning |
| --- | --- |
| `listening.on` | The desk microphone loop is running. |
| `listening.mode` | `"wake"`: sentences must start with "Claude". `"plain"`: every sentence is sent. |
| `listening.target` | The project whose Claude session gets what you say. |
| `listening.last` | The newest log entry's time and outcome: `sent`, `ignored` (no wake word), `held` (unfinished sentence), `switched`, `nothing`, `stopped`, `heard`. `null` if there is none. |
| `speaking.global` | Replies are spoken in every session (`voice on`). |
| `speaking.sessions` | Projects whose session speaks its replies (`voice session`). A project can appear twice: two sessions in one folder. |
| `speaking.playingNow` | A reply is being spoken right now. |
| `speaking.speaker` | Whose reply that is, or `null`. |
| `speaking.waiting` | Projects whose reply waits in line until the speaker has finished. |
| `speaking.lastLatency` | Time from the end of Claude's turn to the first sound, last time. |
| `projects` | Sessions you can switch to. A session appears after its first reply. |
| `hooks` | Whether voiceLoop's three hooks are registered. `false` means spoken replies won't work. |
| `commands` | Slash commands that may be spoken ("command compact"); `confirm` = it asks for a yes. |

The phone page has its own status, also without text, while its server runs:

```bash
curl -s http://127.0.0.1:8445/api/status
# {"running": true, "project": "myProject", "paired": 1, "state": "listening", "last_turn_at": 1795804902}
```

`state` is one of `idle` (nobody connected), `listening`, `transcribing`, `thinking`, `talking`,
`stopped` (paused: tap the page). When the server is not running, the request fails; treat that
as "off".

## Changing things: four commands

| Button | Command | What it does |
| --- | --- | --- |
| Silence now | `voice stop` | Stops the voice and drops replies waiting to speak. Turns nothing off. |
| Turn replies off | `voice off all` | No spoken replies in any session. |
| Stop listening | `voice listen off` | Ends the microphone loop. |
| Talk to this one | `voice listen switch <project>` | Moves the microphone to that project's session. Works while Claude is busy. |

A change takes effect within a second or two, so read the status again after a button press.

## Four rules worth keeping

1. **Stop and redirect, never start.** Don't add a button that turns a microphone on. A
   dashboard is easy to reach; a microphone should take a deliberate step at the keyboard
   (`voice listen wake`, `/voice-app`, `/voice-talk`).
2. **Fixed commands only.** Map each button to a fixed argument list, as the example's `ACTIONS`
   table does. Never build a shell command from text that came from the page.
3. **Check the project name.** Before `voice listen switch`, make sure the name is one of
   `projects` from the status and contains only letters, digits, spaces, dots, dashes and
   underscores.
4. **Mind who can reach it.** If the dashboard listens on your network and not only on
   `127.0.0.1`, everyone on that network can press its buttons. The example binds to this
   computer only and checks the `Host` and `Origin` headers, so another website open in your
   browser cannot press them either.

Also: run the status command at most every few seconds and share the result between open
pages (the example keeps it for 3 seconds). It starts a process each time.

## An Obsidian dashboard

A note cannot run a command by itself. Two ways that work:

- **A small file, refreshed on a timer.** Let a scheduled job write the status into your vault,
  and read that file from a Dataview or Bases view:
  `voice status --json > "/path/to/vault/voice-status.json"` every minute (launchd or cron).
  Buttons are then links or Shell-commands-plugin entries that run the four commands above.
- **Embed the example page.** Run `examples/voice-dashboard.py` and show
  `http://localhost:8790/` in a web-view pane. You get the live card and the buttons without
  writing anything.

## What a good voice card answers

In this order: is it listening and to whom; which sessions are there and which one has the
microphone; what can I do right now. Name each button by its effect ("Silence now"), and show
the command to start listening when it is off, since the page itself can't.
