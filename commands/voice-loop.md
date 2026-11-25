---
description: Toggle spoken replies (voiceLoop) — on | off | session [on|off] | stop | status | test
argument-hint: "[on|off|session|stop|status|test]"
allowed-tools: Bash(voice:*)
---

Run exactly this and report its output in one short line, nothing else:

`voice $ARGUMENTS`

(If no arguments were given, it runs `voice status`. `session` turns voice on for this chat
only. Do not change any files.)
