#!/usr/bin/env python3
# Name:    voice-dashboard
# Purpose: A small, complete example of a dashboard for voiceLoop: one page that shows
#          whether it is listening and to which Claude session, lists the sessions, and
#          has buttons to switch the microphone and to make it quiet. Meant to be read,
#          copied and rebuilt inside your own dashboard (see docs/DASHBOARD.md).
# Usage:   python3 examples/voice-dashboard.py [--port 8790]
#          then open http://localhost:8790/   ·   Ctrl-C stops it
# Deps:    Python standard library; voiceLoop installed (the `voice` command on the PATH
#          or in ~/.local/bin).
# Notes:   Everything it knows comes from ONE command, `voice status --json`, which never
#          contains what was said or spoken. Everything it does is one of FOUR fixed
#          commands. It can stop and redirect; it can never turn a microphone on.
#          It answers only on this computer (127.0.0.1) and only to its own page (Host
#          and Origin are checked), because its buttons change what your sessions do.

import argparse
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

VOICE = shutil.which("voice") or os.path.expanduser("~/.local/bin/voice")
PROJECT = re.compile(r"[A-Za-z0-9 ._-]{1,60}")     # a project name is a folder name; nothing else gets through
# The whole control surface. A button sends the key; the command is fixed here.
ACTIONS = {
    "silence": ["stop"],                 # stop the voice and drop replies waiting to speak
    "replies-off": ["off", "all"],       # no spoken replies in any session
    "stop-listening": ["listen", "off"],
    "switch": ["listen", "switch", "{project}"],
}
_cache = {"at": 0.0, "data": None}


def run_voice(args: list) -> subprocess.CompletedProcess:
    return subprocess.run([VOICE, *args], capture_output=True, text=True, timeout=15)


def talk_status(port: int = 8445) -> dict:
    """The phone page's own status (also text-free), if its server is running."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return {"running": False}


def status(max_age: float = 3.0) -> dict:
    """`voice status --json`, kept for a few seconds so several open pages don't each start a process."""
    if _cache["data"] is not None and time.time() - _cache["at"] < max_age:
        return _cache["data"]
    try:
        data = json.loads(run_voice(["status", "--json"]).stdout)
        data["talk"] = talk_status()
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        data = {"error": f"could not read the voice status: {type(e).__name__}"}
    _cache.update(at=time.time(), data=data)
    return data


def act(do: str, project: str = "") -> dict:
    """One button press -> one fixed command. Returns {"ok": bool, "error": str}."""
    argv = ACTIONS.get(do)
    if not argv:
        return {"ok": False, "error": "unknown action"}
    if "{project}" in argv:
        if not PROJECT.fullmatch(project or "") or project not in (status(0).get("projects") or []):
            return {"ok": False, "error": "not a project you can switch to"}
        argv = [project if a == "{project}" else a for a in argv]
    try:
        r = run_voice(argv)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "error": type(e).__name__}
    _cache["data"] = None
    return {"ok": r.returncode == 0, "error": "" if r.returncode == 0 else (r.stderr or r.stdout).strip()[:200]}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def ours(self) -> bool:
        """Only requests addressed to this server by its local name, and (when a browser says
        where the page came from) only from our own page: another website open in your
        browser must not be able to press these buttons."""
        host = self.headers.get("Host", "").lower()
        if host not in self.server.hosts:
            return False
        origin = self.headers.get("Origin")
        return origin is None or urllib.parse.urlparse(origin).netloc.lower() == host

    def do_GET(self):
        if not self.ours():
            return self.send(403, b'{"error": "wrong host or origin"}')
        if self.path == "/":
            return self.send(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path == "/api/voice":
            return self.send(200, json.dumps(status()).encode())
        self.send(404, b'{"error": "not found"}')

    def do_POST(self):
        if not self.ours():
            return self.send(403, b'{"error": "wrong host or origin"}')
        if self.path != "/api/act":
            return self.send(404, b'{"error": "not found"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n)) if 0 < n < 4096 else {}
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        result = act(str(body.get("do", "")), str(body.get("project", "")))
        self.send(200 if result["ok"] else 400, json.dumps(result).encode())


def make_server(port: int) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)   # this computer only
    port = srv.server_address[1]
    srv.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    return srv


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>voiceLoop</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --edge:#d9dde3; --fg:#16191d; --dim:#5d6672; --accent:#0b7a63; --ok:#1a7f37; --bad:#b42318; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0e1116; --card:#171b22; --edge:#262c36; --fg:#e6edf3; --dim:#8b949e; --accent:#7ee0c4; --ok:#3fb950; --bad:#f85149; } }
  body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, -apple-system, sans-serif; }
  main { max-width:720px; margin:0 auto; background:var(--card); border:1px solid var(--edge); border-radius:12px; padding:18px 20px; }
  .hero { display:flex; gap:12px; align-items:center; padding-bottom:14px; }
  .dot { flex:none; width:14px; height:14px; border-radius:50%; background:var(--edge); }
  .dot.on { background:var(--ok); }
  .title { font-size:19px; font-weight:600; } .title b { color:var(--accent); }
  .sub, .note { color:var(--dim); font-size:13px; }
  section { border-top:1px solid var(--edge); padding:12px 0 4px; }
  h2 { margin:0 0 8px; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--dim); }
  .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; padding:8px 10px; margin-bottom:6px;
         background:var(--bg); border:1px solid var(--edge); border-radius:8px; }
  .row.here { border-color:var(--accent); }
  .name { font-weight:600; min-width:9em; } .tags { flex:1; display:flex; gap:6px; flex-wrap:wrap; }
  .tag { font-size:11px; padding:1px 8px; border-radius:999px; border:1px solid var(--edge); color:var(--dim); }
  .tag.mic { border-color:var(--accent); color:var(--accent); } .tag.spk { border-color:var(--ok); color:var(--ok); }
  button { font:inherit; font-size:13px; padding:6px 12px; border-radius:8px; border:1px solid var(--edge);
           background:var(--card); color:var(--fg); cursor:pointer; }
  button.go { border-color:var(--accent); color:var(--accent); }
  button:hover { border-color:var(--accent); }
  .acts { display:flex; gap:8px; flex-wrap:wrap; }
  #msg { min-height:1.5em; font-size:13px; color:var(--dim); } #msg.bad { color:var(--bad); }
  code { background:var(--bg); border:1px solid var(--edge); border-radius:4px; padding:0 4px; font-size:13px; }
</style></head><body><main>
<div id="app">loading&hellip;</div><div id="msg"></div>
</main><script>
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const btn = (label, what, project, cls) =>
  `<button class="${cls||''}" data-do="${esc(what)}"${project ? ` data-project="${esc(project)}"` : ''}>${esc(label)}</button>`;

async function load() {
  const d = await (await fetch('/api/voice')).json(), app = document.getElementById('app');
  if (d.error) { app.textContent = d.error; return; }
  const li = d.listening || {}, sp = d.speaking || {}, tk = d.talk || {};
  const voiced = [...new Set(sp.sessions || [])], waiting = sp.waiting || [];
  const anyVoice = sp.global || voiced.length > 0;
  let h = `<div class="hero"><span class="dot ${li.on ? 'on' : ''}"></span><div>` + (li.on
    ? `<div class="title">Listening${li.target ? ` &mdash; talking to <b>${esc(li.target)}</b>` : ''}</div>
       <div class="sub">${li.mode === 'wake' ? 'Start each sentence with &ldquo;Claude, &hellip;&rdquo;' : 'Every sentence is sent'}</div>`
    : `<div class="title">The microphone is off</div>
       <div class="sub">Start it in a terminal: <code>voice listen wake</code>. This page can stop or redirect it, never start it.</div>`) + `</div></div>`;

  h += `<section><h2>Claude sessions</h2>`;
  const names = [...new Set([...(d.projects || []), ...voiced])].sort();
  for (const p of names) {
    const here = li.on && p === li.target;
    h += `<div class="row ${here ? 'here' : ''}"><span class="name">${esc(p)}</span><span class="tags">`
      + (here ? '<span class="tag mic">has the microphone</span>' : '')
      + (sp.speaker === p ? '<span class="tag mic">speaking now</span>' : '')
      + (waiting.includes(p) ? '<span class="tag mic">waiting to speak</span>' : '')
      + (sp.global || voiced.includes(p) ? '<span class="tag spk">replies aloud</span>' : '<span class="tag">silent</span>')
      + `</span>${li.on && !here ? btn('Talk to this one', 'switch', p, 'go') : ''}</div>`;
  }
  if (!names.length) h += `<div class="note">None yet. A session shows up after its first reply.</div>`;
  h += `</section><section><h2>Make it quiet</h2><div class="acts">`
    + (sp.playingNow || waiting.length ? btn('Silence now', 'silence') : '')
    + (anyVoice ? btn('Turn replies off everywhere', 'replies-off') : '')
    + (li.on ? btn('Stop listening', 'stop-listening') : '') + `</div>`
    + (!sp.playingNow && !waiting.length && !anyVoice && !li.on ? '<div class="note">Nothing is on.</div>' : '') + `</section>`;
  h += `<section><h2>Phone</h2><div class="note">` + (tk.running
    ? `Talk page: ${esc(tk.state)}${tk.project ? ', talking to ' + esc(tk.project) : ''}, ${tk.paired || 0} phone(s) paired`
    : 'The Talk page is not running. Start it in a Claude session: <code>/voice-talk</code>') + `</div></section>`;
  app.innerHTML = h;
}

document.addEventListener('click', async e => {
  const b = e.target.closest('button[data-do]'); if (!b) return;
  const msg = document.getElementById('msg');
  const r = await (await fetch('/api/act', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ do: b.dataset.do, project: b.dataset.project || '' }) })).json();
  msg.className = r.ok ? '' : 'bad'; msg.textContent = r.ok ? 'Done.' : 'Not done: ' + r.error;
  setTimeout(() => { msg.textContent = ''; load(); }, 2500);
});
load(); setInterval(load, 5000);
</script></body></html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="An example dashboard for voiceLoop (this computer only)")
    ap.add_argument("--port", type=int, default=8790)
    a = ap.parse_args()
    if not os.path.exists(VOICE):
        print("voice-dashboard: the `voice` command was not found. Install voiceLoop first (./install.sh).", file=sys.stderr)
        return 1
    try:
        srv = make_server(a.port)
    except OSError as e:
        print(f"voice-dashboard: port {a.port} is not free ({e.strerror}). Try --port.", file=sys.stderr)
        return 1
    print(f"voiceLoop example dashboard: http://localhost:{srv.server_address[1]}/   (Ctrl-C stops)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
