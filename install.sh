#!/bin/bash
# Name:    install.sh (voiceLoop)
# Purpose: Set up voiceLoop on a Mac in one run, then prove it works: brew tools,
#          the three scripts in ~/.claude/hooks, the `voice` command, the slash commands
#          (/voice-loop, /voice-app), models (checksummed), sound cues, a default config,
#          and a self-test. Safe to re-run: every step skips what is already right.
# Usage:   cd ~/Projects/voiceLoop && ./install.sh            # install + test
#          ./install.sh --check                                # test only, change nothing
#          (Without --check it runs `brew install` for missing tools without asking.)
#          ./install.sh --no-kokoro                            # skip the 350 MB neural voice (use `say`)
#          ./install.sh --quiet                                # no test sound
#          ./install.sh --no-measure                           # skip the 2 s room-noise check
#          ./install.sh --whisper=base                         # the 148 MB base.en model instead of
#                                                                small.en (466 MB): for old Macs / tight disks
# Deps:    macOS, Homebrew. Scripts come from this repo's scripts/. Developers who keep the
#          canonical copies elsewhere point $VOICELOOP_SRC (or an untracked .dev-source file)
#          at that folder; then it installs from there and warns if scripts/ has drifted.
#          Models: from $VOICELOOP_MIRROR first if set (offline / LAN installs: a folder laid
#          out like ~/.local/share/voiceloop), else
#          the internet (Hugging Face / GitHub); every file is SHA-256 checked either way.
# Notes:   It never edits ~/.claude/settings.json. If the hooks aren't registered it builds
#          a merged copy and prints the one `cp` for you to run. It never overwrites an
#          existing ~/.claude/voice/config.json. Docs: README.md, docs/INSTALL.md.
set -uo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DEV_SRC=${VOICELOOP_SRC:-$(cat "$HERE/.dev-source" 2>/dev/null || true)}
HOOKS=$HOME/.claude/hooks
SHARE=$HOME/.local/share/voiceloop
MIRROR=${VOICELOOP_MIRROR:-/nonexistent}
SETTINGS=$HOME/.claude/settings.json
CHECK=0; KOKORO=1; QUIET=0; MEASURE=1; WHISPER=small
for a in "$@"; do case $a in
  --check) CHECK=1 ;; --no-kokoro) KOKORO=0 ;; --quiet) QUIET=1 ;; --no-measure) MEASURE=0 ;;
  --whisper=*) WHISPER=${a#*=} ;;
  -h|--help) sed -n "2,/^set -uo/{/^set -uo/d;p;}" "$0"; exit 0 ;;
  *) echo "unknown option: $a" >&2; exit 2 ;; esac; done

ok=0; bad=0; todo=()
pass() { printf '  \033[32mok\033[0m    %s\n' "$*"; ok=$((ok+1)); }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; bad=$((bad+1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
do_() { if (( CHECK )); then return 1; fi; "$@"; }   # act only when installing

# name|path|sha256|url   (SHA-256 of the files as verified 2026-11-25)
BASE="whisper base.en|models/ggml-base.en.bin|a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin"
MODELS=(
  "Silero VAD|models/ggml-silero-v5.1.2.bin|29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf|https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin"
)
SMALL="whisper small.en|models/ggml-small.en.bin|c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d|https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin"
KMODELS=(
  "Kokoro model|kokoro/kokoro-v1.0.onnx|7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5|https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"
  "Kokoro voices|kokoro/voices-v1.0.bin|bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d|https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"
)

[[ $(uname) == Darwin ]] || { echo "voiceLoop is macOS-only (say, afplay, iTerm)." >&2; exit 1; }
(( CHECK )) && echo "voiceLoop: checking only (nothing will be changed)" || echo "voiceLoop: installing"

step "1. Tools (Homebrew)"
if ! command -v brew >/dev/null; then
  fail "Homebrew missing: install it from https://brew.sh, then re-run"; exit 1; fi
for pair in whisper-cli:whisper-cpp rec:sox uv:uv jq:jq; do
  bin=${pair%%:*}; pkg=${pair#*:}
  if command -v "$bin" >/dev/null; then pass "$bin"
  elif do_ brew install "$pkg" >/dev/null && command -v "$bin" >/dev/null; then pass "$bin (installed $pkg)"
  else fail "$bin missing (brew install $pkg)"; fi
done

step "2. Scripts -> $HOOKS"
SRC=$HERE/scripts; where="this repo"
if [[ -n $DEV_SRC && -f "$DEV_SRC/voice-speak.py" ]]; then
  SRC=$DEV_SRC; where="the developer source"
  for f in voice-speak.py voice-listen.py voice-kokoro.py; do   # the repo copy is what others install
    cmp -s "$SRC/$f" "$HERE/scripts/$f" || fail "scripts/$f differs from $SRC: cp -p \"$SRC/$f\" scripts/ and commit"
  done
fi
do_ mkdir -p "$HOOKS" 2>/dev/null
for f in voice-speak.py voice-listen.py voice-kokoro.py; do
  if [[ -f "$SRC/$f" ]] && ! cmp -s "$SRC/$f" "$HOOKS/$f"; then
    if do_ cp -p "$SRC/$f" "$HOOKS/$f" && chmod +x "$HOOKS/$f"; then pass "$f (copied from $where)"
    elif [[ -e "$HOOKS/$f" ]]; then fail "$f differs from the copy in $where"
    else fail "$f not installed in $HOOKS"; fi
    continue
  fi
  if [[ -x "$HOOKS/$f" ]]; then pass "$f (== $where)"
  elif [[ -f "$HOOKS/$f" ]] && do_ chmod +x "$HOOKS/$f"; then pass "$f (made executable)"
  else fail "$f missing (not in $SRC)"; fi
done

step "3. Commands"
link=$HOME/.local/bin/voice
if [[ -L $link && $(readlink "$link") == "$HOOKS/voice-speak.py" ]]; then pass "voice -> $HOOKS/voice-speak.py"
elif do_ mkdir -p "$HOME/.local/bin" && do_ ln -sfn "$HOOKS/voice-speak.py" "$link"; then pass "voice (symlink created)"
else fail "$link missing or wrong"; fi
# Check the user's LOGIN shell, not ours: run from Claude Code, ~/.local/bin is on our PATH
# even when no startup file adds it, and `voice` is then missing in a normal terminal.
login_path=$("${SHELL:-/bin/zsh}" -lic 'print -r -- "VLPATH=$PATH" 2>/dev/null || echo "VLPATH=$PATH"' </dev/null 2>/dev/null | sed -n 's/^VLPATH=//p' | tail -1)
if [[ ":${login_path:-$PATH}:" == *":$HOME/.local/bin:"* ]]; then pass "~/.local/bin is on your login PATH"
else
  echo "        ~/.local/bin is not on your login shell's PATH: \`voice\` won't be found in a new terminal"
  todo+=("Add ~/.local/bin to your PATH, then open a new terminal window:
          echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc")
fi
do_ mkdir -p "$HOME/.claude/commands" 2>/dev/null
for c in "$HERE"/commands/*.md; do
  n=$(basename "$c"); dst=$HOME/.claude/commands/$n
  if cmp -s "$c" "$dst"; then pass "/${n%.md}"
  elif do_ cp -p "$c" "$dst"; then pass "/${n%.md} (installed)"
  else fail "/${n%.md} missing or outdated"; fi
done

step "4. Models -> $SHARE (SHA-256 checked)"
fetch() {   # $1 rel path, $2 url -> $SHARE/$1.part
  local out=$SHARE/$1.part; mkdir -p "$(dirname "$out")"
  if [[ -f "$MIRROR/$1" ]]; then echo "        from $MIRROR…"; cp "$MIRROR/$1" "$out"
  else echo "        downloading $(basename "$1")…"; curl -fL --progress-bar -o "$out" "$2"; fi
}
[[ $WHISPER == base || $WHISPER == small ]] || { echo "--whisper must be base or small" >&2; exit 2; }
want=("${MODELS[@]}"); (( KOKORO )) && want+=("${KMODELS[@]}")
[[ $WHISPER == small ]] && want+=("$SMALL") || want+=("$BASE")
for m in "${want[@]}"; do
  IFS='|' read -r name rel sha url <<<"$m"; f=$SHARE/$rel
  have=$( [[ -f $f ]] && shasum -a 256 "$f" | awk '{print $1}')
  if [[ $have == "$sha" ]]; then pass "$name"; continue; fi
  if (( CHECK )); then fail "$name ${have:+checksum mismatch}${have:-missing} ($f)"; continue; fi
  if fetch "$rel" "$url" && [[ $(shasum -a 256 "$f.part" | awk '{print $1}') == "$sha" ]]; then
    mv "$f.part" "$f"; pass "$name (fetched, checksum ok)"
  else rm -f "$f.part"; fail "$name: download failed or checksum mismatch"; fi
done

step "5. Sound cues + config"
do_ mkdir -p "$SHARE/sounds" 2>/dev/null
for s in "$HERE"/assets/sounds/*.wav; do
  d=$SHARE/sounds/$(basename "$s")
  if cmp -s "$s" "$d"; then pass "$(basename "$s")"
  elif do_ cp -p "$s" "$d"; then pass "$(basename "$s") (installed)"
  else fail "$(basename "$s") missing"; fi
done
# Room noise -> threshold. The 0.5 % default is fine for a headset but too low for a quiet
# laptop mic (measured: median 0.43 %, recordings never ended). Measure, don't guess.
thr="0.5%"
if (( MEASURE )) && [[ -x $HOOKS/voice-listen.py ]]; then
  echo "        measuring room noise for 2 s: please stay quiet…"
  room=$("$HOOKS/voice-listen.py" --measure-room 2 2>/dev/null)
  if [[ $(jq -r '.ok' <<<"$room" 2>/dev/null) == true ]]; then
    thr=$(jq -r .threshold <<<"$room")
    pass "room noise: median $(jq -r .median <<<"$room")%, peak $(jq -r .max <<<"$room")% -> threshold $thr"
  else
    thr="0.5%"; echo "        room check: $(jq -r '.why // "could not record"' <<<"$room" 2>/dev/null || echo 'could not record')"
    todo+=("Room check heard nothing. This terminal may lack Microphone permission (that's fine if you only use the Claude app). Re-run ./install.sh --check from a terminal with mic access to measure.")
    room=""
  fi
fi
cfg=$HOME/.claude/voice/config.json
if [[ -f $cfg ]]; then
  jq -e . "$cfg" >/dev/null 2>&1 && pass "config.json exists and is valid (left as is)" || fail "config.json is not valid JSON"
  cur=$(jq -r '.listen.threshold // "0.5%"' "$cfg" 2>/dev/null)
  curm=$(jq -r '.listen.model // ""' "$cfg" 2>/dev/null)
  if [[ $WHISPER == small && -n $curm && $curm != *small* ]]; then
    todo+=("Your config pins another speech model. To use small.en (more accurate), set:
          jq '.listen.model = \"$SHARE/models/ggml-small.en.bin\"' $cfg > $cfg.new && mv $cfg.new $cfg")
  fi
  if [[ -n ${room:-} ]] && awk -v a="${thr%\%}" -v b="${cur%\%}" 'BEGIN{exit !(a > b)}'; then
    todo+=("Your threshold ($cur) is below what this room needs ($thr): recordings may never end. Set it with:
          jq '.listen.threshold = \"$thr\"' $cfg > $cfg.new && mv $cfg.new $cfg")
  fi
elif (( CHECK )); then pass "no config.json (defaults: say + the system voice)"
else
  mkdir -p "$(dirname "$cfg")"
  engine=$( (( KOKORO )) && echo kokoro || echo say )
  model=$SHARE/models/ggml-$WHISPER.en.bin
  jq -n --arg e "$engine" --arg s "$SHARE/sounds" --arg t "$thr" --arg m "$model" '{
    engine: $e, kokoro_voice: "bf_emma", voice: "Samantha", rate: 200,
    mode: "first_paragraph", max_chars: 600,
    listen: { threshold: $t, model: $m,
      sounds: { accepted: "Purr", ready: ($s + "/soft-chime.wav"), stopped: "Basso",
                listening: ($s + "/pluck-one.wav") },
      sound_volume: 0.3, sound_volumes: { listening: 0.6 } } }' > "$cfg" && pass "config.json written (engine $engine, $( (( KOKORO )) && echo Emma || echo Samantha))"
fi

step "6. Hooks (Stop + UserPromptSubmit)"
missing=()
for ev in Stop UserPromptSubmit; do
  cmd=$(jq -r --arg ev "$ev" '[.hooks[$ev][]?.hooks[]?.command | select(test("voice-speak.py"))][0] // empty' "$SETTINGS" 2>/dev/null)
  if [[ -z $cmd ]]; then missing+=("$ev"); continue; fi
  hp=${cmd%% *}; hp=${hp//\$HOME/$HOME}; hp=${hp/#\~/$HOME}   # the hook's own command, expanded
  if [[ -x $hp ]]; then pass "$ev hook registered ($hp)"
  else fail "$ev hook registered, but its script $hp is missing or not executable (a stale hook)"; fi
done
if (( ${#missing[@]} )) && (( CHECK )); then
  fail "hooks not registered: ${missing[*]}"
  todo+=("Run ./install.sh (without --check): it prepares the settings change for you to apply.")
elif (( ${#missing[@]} )); then
  merged=$HERE/.capture/settings.merged.json; mkdir -p "$HERE/.capture"
  [[ -f $SETTINGS ]] || echo '{}' > "$merged.in"
  jq '.hooks.Stop = ((.hooks.Stop // []) + (if any(.hooks.Stop[]?.hooks[]?; .command|test("voice-speak")) then [] else
        [{"hooks":[{"type":"command","command":"$HOME/.claude/hooks/voice-speak.py","timeout":10}]}] end))
    | .hooks.UserPromptSubmit = ((.hooks.UserPromptSubmit // []) + (if any(.hooks.UserPromptSubmit[]?.hooks[]?; .command|test("voice-speak")) then [] else
        [{"hooks":[{"type":"command","command":"$HOME/.claude/hooks/voice-speak.py","timeout":5}]}] end))' \
    "$( [[ -f $SETTINGS ]] && echo "$SETTINGS" || echo "$merged.in")" > "$merged" && rm -f "$merged.in"
  fail "hooks not registered: ${missing[*]}"
  if [[ -f $SETTINGS ]]; then
    todo+=("Register the hooks (this script never edits settings.json). Check the diff, then copy:
          diff <(jq -S . $SETTINGS) <(jq -S . $merged)
          cp $SETTINGS $SETTINGS.bak-voiceloop && cp $merged $SETTINGS
        then run /hooks in Claude Code (or restart it).")
  else
    todo+=("Register the hooks: there is no settings.json yet, so just copy the prepared one:
          cp $merged $SETTINGS
        then restart Claude Code.")
  fi
fi

step "7. Self-test"
tlog=$(mktemp -t voiceloop-tests)
# Test the INSTALLED copies (proves what will actually run).
if VOICE_SPEAK=$HOOKS/voice-speak.py VOICE_LISTEN=$HOOKS/voice-listen.py VOICE_KOKORO=$HOOKS/voice-kokoro.py \
   python3 -m unittest discover -s "$HERE/tests" >"$tlog" 2>&1; then
  pass "test suite: $(grep -Eo 'Ran [0-9]+ tests' "$tlog")"
else fail "test suite failed:"; tail -15 "$tlog" | sed 's/^/        /'; fi
rm -f "$tlog"
if command -v voice >/dev/null && voice status >/dev/null 2>&1; then pass "voice status runs"
else fail "voice status failed"; fi
if (( ! QUIET )) && (( ! CHECK )); then
  if (( KOKORO )); then
    echo "        speaking a test sentence (Kokoro's first run installs its Python packages; ~30 s)…"
    echo "voiceLoop is installed on this Mac." | "$HOOKS/voice-kokoro.py" --voice bf_emma \
      && pass "Kokoro spoke (if you heard Say's Samantha instead, Kokoro fell back)" || fail "Kokoro test failed"
  else say "voiceLoop is installed on this Mac." && pass "say spoke"; fi
fi

todo+=("Microphone permission: each app that records needs it (iTerm, Terminal, and/or the Claude app)
        in System Settings > Privacy & Security > Microphone, then a RELAUNCH of that app.
        Without it the mic records silence and macOS never asks. Claude-app-only? Run \`voice setup\`
        in the app's Terminal panel: it uses the Claude app's permission.")
todo+=("iTerm conversation mode also needs Automation permission for iTerm (asked on first send).")

printf '\n\033[1mResult: %d ok, %d need attention\033[0m\n' "$ok" "$bad"
printf '\nStill for you:\n'; for t in "${todo[@]}"; do printf '  - %s\n' "$t"; done
printf '\nNext: run `voice setup` in a terminal tab (mic check, wake-word training, settings).\n'
printf 'Then: in iTerm, `voice session` + `voice listen wake`; in the Claude app, /voice-app.\n'
(( bad == 0 ))
