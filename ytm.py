#!/usr/bin/env python3
"""
ytm.py - YouTube Music phone page for Raspberry Pi (no login, no browser).

Search songs/videos/albums/playlists, open any public playlist by URL or ID,
queue, favourites, saved playlists, Auto Play on start. Playback via mpv + yt-dlp.

Setup:
    sudo apt install mpv
    pip install -U flask ytmusicapi yt-dlp --break-system-packages
    mkdir -p ~/YTMusic && cp ytm.py ~/YTMusic/ && cd ~/YTMusic
    nohup python3 ytm.py > ytm.log 2>&1 &
Open http://<pi-ip>:8001 on your phone.

Optional env vars: MPV_AUDIO_DEVICE (e.g. "alsa/plughw:CARD=Headphones,DEV=0"), YTM_PORT.
"""
import atexit
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request
from ytmusicapi import YTMusic

BASE = Path(os.environ.get("YTM_DIR", Path.home() / "YTMusic"))
BASE.mkdir(parents=True, exist_ok=True)
SOCK = "/tmp/ytm-mpv.sock"
PORT = int(os.environ.get("YTM_PORT", "8001"))
FAV_FILE = BASE / "favourites.json"
SAVED_FILE = BASE / "saved_playlists.json"
STATE_FILE = BASE / "state.json"
DL_DIR = BASE / "DownloadedSongs"
DL_DIR.mkdir(parents=True, exist_ok=True)
AUDIO_EXT = {".opus", ".webm", ".m4a", ".mp3", ".flac", ".ogg", ".wav", ".aac", ".mka", ".wma"}

os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={os.environ['XDG_RUNTIME_DIR']}/bus")
SINK = "@DEFAULT_AUDIO_SINK@"

app = Flask(__name__)
yt = YTMusic()  # unauthenticated: search + public playlists only
LOCK = threading.RLock()
PROC = None
_cache = {}


# ---------------------------------------------------------------- storage
def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    tmp.replace(path)


state = {"autoplay": False, "queue": [], "pos": 0, "volume": 70}
state.update(load(STATE_FILE, {}))


def save_state():
    save(STATE_FILE, state)


# ---------------------------------------------------------------- mpv
def ensure_mpv(start=False):
    global PROC
    if PROC and PROC.poll() is None and os.path.exists(SOCK):
        return True
    if not start:
        return False
    try:
        os.remove(SOCK)
    except OSError:
        pass
    args = [
        "mpv", "--no-video", "--idle=yes", "--force-window=no", "--no-terminal",
        f"--input-ipc-server={SOCK}", "--ytdl-format=bestaudio/best",
        "--volume=100",
        f"--log-file={BASE / 'mpv.log'}", "--msg-level=all=warn,ytdl_hook=info",
    ]
    # Anti-distortion: never amplify above 100%, and soft-limit peaks so loud masters can't clip.
    # Set YTM_NORMALIZE=1 to also even out loudness between tracks.
    filt = "alimiter=limit=0.9:level=disabled"
    if os.environ.get("YTM_NORMALIZE") == "1":
        filt = "loudnorm=I=-16:TP=-1.5:LRA=11," + filt
    args += ["--volume-max=100", f"--af=lavfi=[{filt}]"]
    cookies = BASE / "cookies.txt"  # optional: Netscape-format cookies from a throwaway account
    if cookies.exists():
        args.append(f"--ytdl-raw-options=cookies={cookies}")
    dev = os.environ.get("MPV_AUDIO_DEVICE")
    if dev:
        args.append(f"--audio-device={dev}")
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
    # Without these (SSH/cron/nohup), ALSA "default" can't reach PipeWire/Pulse -> "Host is down"
    xdg = env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={xdg}/bus")
    PROC = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if os.path.exists(SOCK):
            return True
        time.sleep(0.1)
    return False


def mpv_cmd(*cmd, start=False):
    with LOCK:
        if not ensure_mpv(start):
            return None
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(4)
                s.connect(SOCK)
                s.sendall(json.dumps({"command": list(cmd)}).encode() + b"\n")
                buf = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        return None
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if "error" in d:  # skip async event lines
                            return d
        except OSError:
            return None


def prop(name):
    d = mpv_cmd("get_property", name)
    return d.get("data") if d and d.get("error") == "success" else None


def wpctl(*args):
    try:
        return subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""


def get_volume():
    """System (PipeWire) output volume, same control radio.py uses: {"percent", "muted"} or None."""
    out = wpctl("get-volume", SINK)
    m = re.search(r"Volume:\s*([0-9.]+)", out)
    if not m:
        return None
    return {"percent": round(float(m.group(1)) * 100), "muted": "[MUTED]" in out}


def local_path(rel):
    """Resolve a DownloadedSongs-relative path; None if missing or outside the folder."""
    p = (DL_DIR / rel).resolve()
    return p if DL_DIR.resolve() in p.parents and p.is_file() else None


def url_of(t):
    if t["id"].startswith("local:"):
        p = local_path(t["id"][6:])
        return str(p) if p else str(DL_DIR / "_missing_")
    return f"https://www.youtube.com/watch?v={t['id']}"


def start_queue(tracks):
    tracks = [t for t in tracks if t.get("id")]
    if not tracks:
        return {"error": "Nothing playable"}
    with LOCK:
        state["queue"], state["pos"] = tracks, 0
        save_state()
        r = mpv_cmd("loadfile", url_of(tracks[0]), "replace", start=True)
        if r is None:
            return {"error": "Could not start mpv (is it installed?)"}
        for t in tracks[1:]:
            mpv_cmd("loadfile", url_of(t), "append")
        mpv_cmd("set_property", "pause", False)
    return {"ok": True, "count": len(tracks)}


def kill_stale():
    subprocess.run(["pkill", "-f", f"input-ipc-server={SOCK}"], check=False)
    try:
        os.remove(SOCK)
    except OSError:
        pass


@atexit.register
def _cleanup():
    if PROC and PROC.poll() is None:
        PROC.terminate()


# ---------------------------------------------------------------- YouTube Music
def cached(key, fn, ttl=300):
    v = _cache.get(key)
    if v and time.time() - v[0] < ttl:
        return v[1]
    r = fn()
    _cache[key] = (time.time(), r)
    return r


def names(artists):
    if isinstance(artists, str):
        return artists
    return ", ".join(a.get("name", "") for a in (artists or []) if a.get("name"))


def track_of(r, fallback_artist=""):
    return {
        "id": r.get("videoId"),
        "title": r.get("title") or "",
        "artist": names(r.get("artists")) or fallback_artist,
        "dur": r.get("duration") or "",
    }


def do_search(q, kind):
    res = yt.search(q, filter=kind, limit=25)
    out = []
    for r in res:
        if kind in ("songs", "videos"):
            t = track_of(r)
            if t["id"]:
                out.append(t)
        elif kind == "playlists":
            pid = (r.get("browseId") or "").removeprefix("VL")
            sub = r.get("author") or ""
            if r.get("itemCount"):
                sub += f" · {r['itemCount']} tracks"
            out.append({"kind": "playlist", "id": pid, "title": r.get("title", ""), "sub": sub})
        elif kind == "albums":
            sub = names(r.get("artists")) + (f" · {r['year']}" if r.get("year") else "")
            out.append({"kind": "album", "id": r.get("browseId"), "title": r.get("title", ""), "sub": sub})
    return out


def open_ref(ref, kind):
    ref = ref.strip()
    if kind == "album":
        a = yt.get_album(ref)
        art = names(a.get("artists"))
        tr = [track_of(t, art) for t in a.get("tracks", [])]
        return {"kind": "album", "id": ref, "title": a.get("title", "Album"), "tracks": [t for t in tr if t["id"]]}
    m = re.search(r"[?&]list=([\w-]+)", ref)
    pid = m.group(1) if m else None
    if not pid and re.match(r"^(VL)?(PL|OLAK|UU|LL|RD)[\w-]{8,}$", ref):
        pid = ref.removeprefix("VL")
    if pid:
        p = yt.get_playlist(pid, limit=300)
        tr = [track_of(t) for t in p.get("tracks", [])]
        return {"kind": "playlist", "id": pid, "title": p.get("title", "Playlist"), "tracks": [t for t in tr if t["id"]]}
    m = re.search(r"(?:[?&]v=|youtu\.be/)([\w-]{11})", ref) or re.match(r"^([\w-]{11})$", ref)
    if m:
        vid = m.group(1)
        try:
            d = yt.get_song(vid)["videoDetails"]
            t = {"id": vid, "title": d.get("title", vid), "artist": d.get("author", ""), "dur": ""}
        except Exception:
            t = {"id": vid, "title": vid, "artist": "", "dur": ""}
        return {"kind": "track", "id": vid, "title": t["title"], "tracks": [t]}
    raise ValueError("Not a recognised YouTube Music link or ID")


def fail(e, code=502):
    return jsonify({"error": str(e)}), code


# ---------------------------------------------------------------- downloads + local files
DL_JOBS = []
DL_Q = queue.Queue()
DL_LOCK = threading.Lock()


def have_download(vid):
    tag = f"[{vid}]"
    return any(p.is_file() and p.suffix.lower() in AUDIO_EXT and tag in p.name for p in DL_DIR.rglob("*"))


def safe_name(artist, title, vid):
    base = f"{artist} - {title}" if artist else (title or vid)
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", base).strip(" .") or vid
    while len(base.encode()) > 170:  # stay well under the 255-byte filename limit (Hindi = 3 bytes/char)
        base = base[:-1]
    return f"{base.rstrip()} [{vid}]"


def ytdlp_cmd():
    path = str(Path.home() / ".local/bin") + ":" + os.environ.get("PATH", "")
    exe = shutil.which("yt-dlp", path=path)
    return [exe] if exe else [sys.executable, "-m", "yt_dlp"]


def run_download(job):
    vid = job["id"]
    if have_download(vid):
        job.update(status="done", pct=100, msg="already downloaded")
        return
    job["status"] = "downloading"
    name = safe_name(job["artist"], job["title"], vid).replace("%", "%%")
    cmd = ytdlp_cmd() + [
        "--no-playlist", "--newline", "--no-warnings",
        "-f", "bestaudio[acodec=opus]/bestaudio",  # best public audio, native Opus when available
        "-P", str(DL_DIR), "-o", name + ".%(ext)s",
    ]
    if shutil.which("ffmpeg"):  # lets yt-dlp write a clean .opus with tags, no re-encoding
        cmd += ["-x", "--audio-format", "best", "--embed-metadata"]
    cmd.append(f"https://www.youtube.com/watch?v={vid}")
    env = os.environ.copy()
    env["PATH"] = str(Path.home() / ".local/bin") + ":" + env.get("PATH", "")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace", env=env)
    last_err = ""
    for line in p.stdout:
        m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if m:
            job["pct"] = float(m.group(1))
        elif "ERROR" in line:
            last_err = line.strip()
    p.wait()
    if p.returncode == 0 and have_download(vid):
        job.update(status="done", pct=100)
    else:
        job.update(status="error", msg=(last_err or f"yt-dlp exit {p.returncode}")[:160])


def dl_worker():
    while True:  # one download at a time, gentle on YouTube
        job = DL_Q.get()
        try:
            run_download(job)
        except Exception as e:
            job.update(status="error", msg=str(e)[:160])
        time.sleep(1.5)


@app.post("/api/download")
def api_download():
    n = 0
    for t in request.get_json(force=True).get("tracks", []):
        vid = t.get("id") or ""
        if not vid or vid.startswith("local:") or have_download(vid):
            continue
        with DL_LOCK:
            if any(j["id"] == vid and j["status"] in ("queued", "downloading") for j in DL_JOBS):
                continue
            job = {"id": vid, "title": t.get("title", ""), "artist": t.get("artist", ""),
                   "status": "queued", "pct": 0, "msg": ""}
            DL_JOBS.append(job)
            del DL_JOBS[:-100]
        DL_Q.put(job)
        n += 1
    return jsonify({"queued": n})


@app.get("/api/downloads")
def api_downloads():
    return jsonify({"jobs": DL_JOBS})


@app.post("/api/downloads/clear")
def api_downloads_clear():
    with DL_LOCK:
        DL_JOBS[:] = [j for j in DL_JOBS if j["status"] in ("queued", "downloading")]
    return jsonify({"ok": True})


@app.get("/api/local")
def api_local():
    tracks, yt_ids = [], []
    for p in sorted(DL_DIR.rglob("*"), key=lambda x: str(x).lower()):
        if not (p.is_file() and p.suffix.lower() in AUDIO_EXT):
            continue
        stem = p.stem
        m = re.search(r"\s\[([\w-]{11})\]$", stem)
        if m:
            yt_ids.append(m.group(1))
            stem = stem[:m.start()]
        artist, title = "", stem
        if " - " in stem:
            artist, title = stem.split(" - ", 1)
        mb = p.stat().st_size / 1048576
        tracks.append({"id": "local:" + str(p.relative_to(DL_DIR)), "title": title, "artist": artist,
                       "dur": f"{p.suffix[1:].upper()} {mb:.1f} MB"})
    return jsonify({"tracks": tracks, "yt_ids": yt_ids})


@app.post("/api/local/delete")
def api_local_delete():
    tid = request.get_json(force=True).get("id", "")
    p = local_path(tid[6:]) if tid.startswith("local:") else None
    if not p:
        return fail("File not found", 404)
    p.unlink()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API
@app.get("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    kind = request.args.get("type", "songs")
    if kind not in ("songs", "videos", "playlists", "albums") or not q:
        return fail("Bad search", 400)
    try:
        return jsonify({"results": cached(("s", q, kind), lambda: do_search(q, kind))})
    except Exception as e:
        return fail(e)


@app.get("/api/open")
def api_open():
    ref, kind = request.args.get("ref", ""), request.args.get("kind", "playlist")
    try:
        return jsonify(cached(("o", ref, kind), lambda: open_ref(ref, kind)))
    except Exception as e:
        return fail(e)


@app.post("/api/play")
def api_play():
    d = request.get_json(force=True)
    tracks = d.get("tracks", [])[int(d.get("index", 0)):]
    if d.get("shuffle"):
        random.shuffle(tracks)
    r = start_queue(tracks)
    return jsonify(r), (502 if "error" in r else 200)


@app.post("/api/enqueue")
def api_enqueue():
    t = request.get_json(force=True).get("track")
    if not t or not t.get("id"):
        return fail("No track", 400)
    alive = ensure_mpv(False) and not prop("idle-active")
    if not alive:
        return jsonify(start_queue([t]))
    with LOCK:
        state["queue"].append(t)
        save_state()
        mpv_cmd("loadfile", url_of(t), "append")
    return jsonify({"ok": True})


@app.post("/api/jump")
def api_jump():
    mpv_cmd("set_property", "playlist-pos", int(request.get_json(force=True)["index"]))
    mpv_cmd("set_property", "pause", False)
    return jsonify({"ok": True})


@app.post("/api/ctl")
def api_ctl():
    d = request.get_json(force=True)
    a = d.get("action")
    if a == "toggle":
        if not ensure_mpv(False) or prop("idle-active"):
            q = state["queue"][max(state["pos"], 0):]
            return jsonify(start_queue(q) if q else {"error": "Queue is empty"})
        mpv_cmd("cycle", "pause")
    elif a == "next":
        mpv_cmd("playlist-next")
    elif a == "prev":
        if (prop("time-pos") or 0) > 5:
            mpv_cmd("seek", 0, "absolute")
        else:
            mpv_cmd("playlist-prev")
    elif a == "seek":
        mpv_cmd("seek", float(d.get("value", 0)), "absolute")
    elif a == "volume":
        v = max(0, min(100, int(d.get("value", 70))))
        if get_volume() is not None:
            wpctl("set-volume", "-l", "1.0", SINK, f"{v}%")
        else:  # no wpctl: fall back to mpv's own volume
            state["volume"] = v
            save_state()
            mpv_cmd("set_property", "volume", v)
    elif a == "vol_delta":
        step = int(d.get("value", 0))
        if get_volume() is not None:
            wpctl("set-volume", "-l", "1.0", SINK, f"{abs(step)}%{'+' if step > 0 else '-'}")
        else:
            cur = prop("volume")
            v = max(0, min(100, round((state["volume"] if cur is None else cur) + step)))
            state["volume"] = v
            save_state()
            mpv_cmd("set_property", "volume", v)
    elif a == "mute":
        if get_volume() is not None:
            wpctl("set-mute", SINK, "toggle")
        else:
            mpv_cmd("cycle", "mute")
    elif a == "stop":
        mpv_cmd("stop")
    return jsonify({"ok": True})


@app.get("/api/status")
def api_status():
    out = {"alive": False, "paused": True, "idle": True, "pos": -1, "time": 0, "dur": 0,
           "volume": state["volume"], "muted": False, "track": None, "autoplay": state["autoplay"]}
    if ensure_mpv(False):
        idle = prop("idle-active")
        out.update(alive=True, idle=bool(idle), paused=bool(prop("pause")), muted=bool(prop("mute")))
        if not idle:
            pos = prop("playlist-pos")
            out["pos"] = pos if pos is not None else -1
            out["time"] = prop("time-pos") or 0
            out["dur"] = prop("duration") or 0
            v = prop("volume")
            if v is not None:
                out["volume"] = round(v)
            q = state["queue"]
            if pos is not None and 0 <= pos < len(q):
                out["track"] = q[pos]
                if state["pos"] != pos:
                    state["pos"] = pos
                    save_state()
            else:
                out["track"] = {"id": "", "title": prop("media-title") or "", "artist": ""}
    sysvol = get_volume()
    if sysvol:
        out["volume"], out["muted"] = sysvol["percent"], sysvol["muted"]
    return jsonify(out)


@app.get("/api/queue")
def api_queue():
    return jsonify({"queue": state["queue"], "pos": state["pos"] if ensure_mpv(False) else -1})


@app.route("/api/favs", methods=["GET", "POST"])
def api_favs():
    favs = load(FAV_FILE, [])
    if request.method == "POST":
        t = request.get_json(force=True)["track"]
        if any(f["id"] == t["id"] for f in favs):
            favs = [f for f in favs if f["id"] != t["id"]]
        else:
            favs.append({k: t.get(k, "") for k in ("id", "title", "artist", "dur")})
        save(FAV_FILE, favs)
    return jsonify({"favs": favs})


@app.route("/api/saved", methods=["GET", "POST"])
def api_saved():
    items = load(SAVED_FILE, [])
    if request.method == "POST":
        d = request.get_json(force=True)
        it = d["item"]
        items = [i for i in items if i["id"] != it["id"]]
        if d.get("action") == "add":
            items.append({"kind": it["kind"], "id": it["id"], "title": it["title"]})
        save(SAVED_FILE, items)
    return jsonify({"items": items})


@app.route("/api/autoplay", methods=["GET", "POST"])
def api_autoplay():
    if request.method == "POST":
        state["autoplay"] = bool(request.get_json(force=True).get("on"))
        save_state()
    return jsonify({"on": state["autoplay"]})


@app.get("/")
def index():
    return Response(HTML, mimetype="text/html")


# ---------------------------------------------------------------- UI
HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>YT Music Pi</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0f1115;color:#e8e8e8;font:15px system-ui,sans-serif;padding-bottom:130px}
header{position:sticky;top:0;background:#171a21;display:flex;z-index:5}
header button{flex:1;padding:14px 4px;background:none;border:0;color:#9aa;font-size:14px;border-bottom:3px solid transparent}
header button.on{color:#fff;border-color:#ff3d3d}
main{padding:10px}
input,select{background:#1d212b;color:#fff;border:1px solid #333a48;border-radius:8px;padding:11px;font-size:15px}
.row{display:flex;gap:6px;margin-bottom:10px}.row input{flex:1;min-width:0}
button.b{background:#ff3d3d;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-size:14px}
button.g{background:#262b38;color:#ddd;border:0;border-radius:8px;padding:10px 14px;font-size:14px}
.item{display:flex;align-items:center;gap:8px;padding:10px 6px;border-bottom:1px solid #1f2430}
.item.cur{background:#241a1f}
.info{flex:1;min-width:0}.t{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s{color:#8a93a6;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ic{background:none;border:0;color:#8a93a6;font-size:20px;padding:6px 8px}
.ic.on{color:#ff3d3d}
h3{margin:6px 0 10px}
.msg{color:#8a93a6;padding:16px;text-align:center}
.err{color:#ff7b7b}
#bar{position:fixed;left:0;right:0;bottom:0;background:#171a21;border-top:1px solid #2a2f3d;padding:8px 12px 12px;z-index:9}
#np{display:flex;align-items:center;gap:8px}
#ctl{display:flex;justify-content:center;align-items:center;gap:18px;margin:2px 0}
#ctl button{background:none;border:0;color:#fff;font-size:26px;padding:4px 10px}
#pp{font-size:32px!important}
input[type=range]{width:100%;padding:0;accent-color:#ff3d3d}
.vol{display:flex;align-items:center;gap:8px;color:#8a93a6;font-size:13px}
.vb{padding:8px 14px;font-size:18px}
.job{font-size:13px;color:#8a93a6;padding:4px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tm{display:flex;justify-content:space-between;color:#8a93a6;font-size:11px}
</style></head><body>
<header>
<button data-t="search" class="on">Search</button><button data-t="lists">Playlists</button>
<button data-t="local">Local</button><button data-t="favs">Favs</button><button data-t="queue">Queue</button>
</header>
<main>
<section id="search">
 <div class="row"><input id="q" placeholder="Search YouTube Music" enterkeyhint="search">
 <select id="qt"><option value="songs">Songs</option><option value="videos">Videos</option>
 <option value="albums">Albums</option><option value="playlists">Playlists</option></select></div>
 <div class="row"><button class="b" id="go" style="flex:1">Search</button></div>
 <div id="sres"></div>
</section>
<section id="lists" hidden>
 <div class="row"><input id="ref" placeholder="Paste playlist / song link or ID"><button class="b" id="open">Open</button></div>
 <h3>Saved</h3><div id="saved"></div>
</section>
<section id="local" hidden>
 <div id="dljobs"></div>
 <div class="row"><input id="lq" placeholder="Filter local songs"><button class="b" id="lall">Play</button>
 <button class="g" id="lshuf">Shuffle</button></div>
 <div id="llist"></div>
</section>
<section id="favs" hidden><div id="favlist"></div></section>
<section id="queue" hidden>
 <div class="row" style="align-items:center"><label style="flex:1">Auto Play on start</label>
 <button class="g" id="ap">Off</button></div>
 <div id="qlist"></div>
</section>
<section id="detail" hidden>
 <div class="row"><button class="g" id="back">← Back</button></div>
 <h3 id="dtitle"></h3>
 <div class="row"><button class="b" id="dplay">Play all</button><button class="g" id="dshuf">Shuffle</button>
 <button class="g" id="dsave">Save</button><button class="g" id="ddl">⬇ All</button></div>
 <div id="dlist"></div>
</section>
</main>
<div id="bar">
 <div id="np"><div class="info"><div class="t" id="nt">Nothing playing</div><div class="s" id="na"></div></div>
 <button class="ic" id="nf">♡</button></div>
 <input type="range" id="seek" min="0" max="100" value="0">
 <div class="tm"><span id="t1">0:00</span><span id="t2">0:00</span></div>
 <div id="ctl"><button id="prev">⏮</button><button id="pp">▶</button><button id="next">⏭</button></div>
 <div class="vol"><button class="g vb" id="vdn">−</button><input type="range" id="vol" min="0" max="100" value="70">
 <button class="g vb" id="vup">+</button><span id="vp" style="min-width:38px;text-align:right">70%</span>
 <button class="g vb" id="vmu">🔈</button></div>
</div>
<script>
const $=s=>document.querySelector(s);
const api=async(p,o)=>{try{const r=await fetch(p,o);return await r.json()}catch(e){return{error:String(e)}}};
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
const h=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
const fmt=s=>{s=Math.max(0,Math.floor(s||0));return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')};
let favIds=new Set(), cur=null, status={}, detail=null, lastTab='search', seeking=false, volDrag=false;

function msg(box,text,err){box.replaceChildren(h('div','msg'+(err?' err':''),text))}
function tab(t){
 document.querySelectorAll('header button').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 ['search','lists','local','favs','queue'].forEach(s=>$('#'+s).hidden=s!==t);
 $('#detail').hidden=true; lastTab=t;
 if(t==='lists')loadSaved(); if(t==='favs')loadFavs(); if(t==='queue')loadQueue();
 if(t==='local'){loadLocal();dlPoll()}
}
document.querySelectorAll('header button').forEach(b=>b.onclick=()=>tab(b.dataset.t));

async function toggleFav(t,btn){
 const r=await post('/api/favs',{track:t});
 favIds=new Set(r.favs.map(f=>f.id));
 if(btn)btn.classList.toggle('on',favIds.has(t.id));
 return r.favs;
}
function renderTracks(box,tracks){
 box.replaceChildren();
 if(!tracks.length)return msg(box,'No tracks');
 tracks.forEach((t,i)=>{
  const row=h('div','item'), info=h('div','info');
  info.append(h('div','t',t.title),h('div','s',[t.artist,t.dur].filter(Boolean).join(' · ')));
  info.onclick=()=>post('/api/play',{tracks,index:i}).then(r=>r.error&&alert(r.error)).then(poll);
  const q=h('button','ic','＋'); q.onclick=()=>post('/api/enqueue',{track:t}).then(()=>{q.textContent='✓'});
  const f=h('button','ic'+(favIds.has(t.id)?' on':''),'♥'); f.onclick=()=>toggleFav(t,f);
  row.append(info,q);
  if(!t.id.startsWith('local:')){
   const d=h('button','ic',ytDone.has(t.id)?'✓':'⬇');
   d.onclick=async()=>{if(ytDone.has(t.id))return;d.textContent='⏳';await post('/api/download',{tracks:[t]});startDlPoll()};
   row.append(d);
  }
  row.append(f); box.append(row);
 });
}
function renderCollections(box,items){
 box.replaceChildren();
 if(!items.length)return msg(box,'No results');
 items.forEach(it=>{
  const row=h('div','item'), info=h('div','info');
  info.append(h('div','t',it.title),h('div','s',it.sub||it.kind));
  row.onclick=()=>openDetail(it.kind,it.id);
  row.append(info,h('span','s','›')); box.append(row);
 });
}
async function doSearch(){
 const q=$('#q').value.trim(); if(!q)return;
 msg($('#sres'),'Searching…');
 const kind=$('#qt').value;
 const r=await api('/api/search?q='+encodeURIComponent(q)+'&type='+kind);
 if(r.error)return msg($('#sres'),r.error,true);
 (kind==='songs'||kind==='videos')?renderTracks($('#sres'),r.results):renderCollections($('#sres'),r.results);
}
$('#go').onclick=doSearch; $('#q').addEventListener('keydown',e=>e.key==='Enter'&&doSearch());

async function openDetail(kind,ref){
 ['search','lists','local','favs','queue'].forEach(s=>$('#'+s).hidden=true);
 $('#detail').hidden=false; $('#dtitle').textContent='Loading…'; $('#dlist').replaceChildren(); detail=null;
 const r=await api('/api/open?kind='+kind+'&ref='+encodeURIComponent(ref));
 if(r.error){$('#dtitle').textContent='';return msg($('#dlist'),r.error,true)}
 detail=r; $('#dtitle').textContent=r.title+' ('+r.tracks.length+')';
 $('#dsave').hidden=r.kind==='track'; renderTracks($('#dlist'),r.tracks);
}
$('#back').onclick=()=>tab(lastTab);
$('#dplay').onclick=()=>detail&&post('/api/play',{tracks:detail.tracks,index:0}).then(poll);
$('#dshuf').onclick=()=>detail&&post('/api/play',{tracks:detail.tracks,index:0,shuffle:true}).then(poll);
$('#dsave').onclick=async()=>{if(!detail)return;
 await post('/api/saved',{action:'add',item:{kind:detail.kind,id:detail.id,title:detail.title}});$('#dsave').textContent='Saved ✓'};
$('#open').onclick=()=>{const v=$('#ref').value.trim();if(v)openDetail('playlist',v)};

async function loadSaved(){
 const r=await api('/api/saved'), box=$('#saved'); box.replaceChildren();
 if(!r.items||!r.items.length)return msg(box,'No saved playlists yet. Open one and tap Save.');
 r.items.forEach(it=>{
  const row=h('div','item'), info=h('div','info');
  info.append(h('div','t',it.title),h('div','s',it.kind)); info.onclick=()=>openDetail(it.kind,it.id);
  const x=h('button','ic','✕'); x.onclick=async()=>{await post('/api/saved',{action:'remove',item:it});loadSaved()};
  row.append(info,x); box.append(row);
 });
}
async function loadFavs(){
 const r=await api('/api/favs'); favIds=new Set(r.favs.map(f=>f.id));
 const box=$('#favlist');
 if(!r.favs.length)return msg(box,'No favourites yet. Tap ♥ on any track.');
 renderTracks(box,r.favs);
}
async function loadQueue(){
 const a=await api('/api/autoplay'); $('#ap').textContent=a.on?'On':'Off'; $('#ap').className=a.on?'b':'g';
 const r=await api('/api/queue'), box=$('#qlist'); box.replaceChildren();
 if(!r.queue.length)return msg(box,'Queue is empty');
 r.queue.forEach((t,i)=>{
  const row=h('div','item'+(i===r.pos?' cur':'')), info=h('div','info');
  info.append(h('div','t',(i===r.pos?'▶ ':'')+t.title),h('div','s',t.artist));
  info.onclick=()=>post('/api/jump',{index:i}).then(()=>setTimeout(loadQueue,600));
  row.append(info); box.append(row);
 });
}
$('#ap').onclick=async()=>{const on=$('#ap').textContent==='Off';await post('/api/autoplay',{on});loadQueue()};

$('#pp').onclick=()=>post('/api/ctl',{action:'toggle'}).then(poll);
$('#next').onclick=()=>post('/api/ctl',{action:'next'}).then(()=>setTimeout(poll,500));
$('#prev').onclick=()=>post('/api/ctl',{action:'prev'}).then(()=>setTimeout(poll,500));
$('#nf').onclick=async()=>{if(cur&&cur.id)await toggleFav(cur,$('#nf'))};
$('#seek').oninput=()=>{seeking=true};
$('#seek').onchange=()=>{post('/api/ctl',{action:'seek',value:$('#seek').value});seeking=false};
$('#vol').oninput=()=>{volDrag=true;$('#vp').textContent=$('#vol').value+'%'};
$('#vol').onchange=()=>post('/api/ctl',{action:'volume',value:$('#vol').value}).then(()=>{volDrag=false});
const vstep=async d=>{await post('/api/ctl',{action:'vol_delta',value:d});poll()};
$('#vdn').onclick=()=>vstep(-5); $('#vup').onclick=()=>vstep(5);
$('#vmu').onclick=()=>post('/api/ctl',{action:'mute'}).then(poll);

let localTracks=[], localView=[], ytDone=new Set(), dlBusy=false;
async function loadLocal(){
 const r=await api('/api/local'); if(r.error)return;
 localTracks=r.tracks||[]; ytDone=new Set(r.yt_ids||[]); renderLocal();
}
function renderLocal(){
 const f=$('#lq').value.trim().toLowerCase(), box=$('#llist');
 localView=f?localTracks.filter(t=>(t.title+' '+t.artist).toLowerCase().includes(f)):localTracks;
 box.replaceChildren();
 if(!localView.length)return msg(box,localTracks.length?'No match':'No songs yet. Tap ⬇ on any track to download it.');
 localView.forEach((t,i)=>{
  const row=h('div','item'), info=h('div','info');
  info.append(h('div','t',t.title),h('div','s',[t.artist,t.dur].filter(Boolean).join(' · ')));
  info.onclick=()=>post('/api/play',{tracks:localView,index:i}).then(poll);
  const q=h('button','ic','＋'); q.onclick=()=>post('/api/enqueue',{track:t}).then(()=>{q.textContent='✓'});
  const x=h('button','ic','✕');
  x.onclick=async()=>{if(!confirm('Delete from the Pi?\n'+t.title))return;await post('/api/local/delete',{id:t.id});loadLocal()};
  row.append(info,q,x); box.append(row);
 });
}
$('#lq').oninput=renderLocal;
$('#lall').onclick=()=>localView.length&&post('/api/play',{tracks:localView,index:0}).then(poll);
$('#lshuf').onclick=()=>localView.length&&post('/api/play',{tracks:localView,index:0,shuffle:true}).then(poll);
$('#ddl').onclick=async()=>{
 if(!detail)return;
 const n=detail.tracks.filter(t=>!ytDone.has(t.id)).length;
 if(!n)return alert('Already downloaded');
 if(n>30&&!confirm('Download '+n+' songs?'))return;
 const r=await post('/api/download',{tracks:detail.tracks});
 alert((r.queued||0)+' queued. Progress is on the Local tab.'); startDlPoll();
};
function renderJobs(jobs){
 const box=$('#dljobs'); box.replaceChildren();
 if(!jobs.length)return;
 const top=h('div','row'); top.style.alignItems='center';
 const lab=h('div','s','Downloads'); lab.style.flex='1';
 const clr=h('button','g','Clear finished'); clr.onclick=async()=>{await post('/api/downloads/clear');dlPoll()};
 top.append(lab,clr); box.append(top);
 jobs.slice(-12).forEach(j=>{
  const st=j.status==='downloading'?Math.round(j.pct)+'%':j.status==='error'?'failed: '+(j.msg||''):j.status;
  box.append(h('div','job',st+' · '+(j.artist?j.artist+' - ':'')+j.title));
 });
}
async function dlPoll(){
 if(!dlBusy&&lastTab!=='local')return;
 const r=await api('/api/downloads'); if(r.error)return;
 const jobs=r.jobs||[]; renderJobs(jobs);
 const active=jobs.some(j=>j.status==='queued'||j.status==='downloading');
 if(!active&&dlBusy){dlBusy=false;loadLocal()} else if(active)dlBusy=true;
}
const startDlPoll=()=>{dlBusy=true;dlPoll()};
setInterval(dlPoll,2500);

async function poll(){
 const s=await api('/api/status'); if(s.error)return; status=s; cur=s.track;
 $('#nt').textContent=cur?cur.title||'Loading…':'Nothing playing';
 $('#na').textContent=cur?cur.artist||'':'';
 $('#nf').classList.toggle('on',!!(cur&&favIds.has(cur.id)));
 $('#pp').textContent=(s.idle||s.paused)?'▶':'⏸';
 if(!seeking){$('#seek').max=Math.max(1,s.dur);$('#seek').value=s.time;}
 $('#t1').textContent=fmt(s.time);$('#t2').textContent=fmt(s.dur);
 if(!volDrag){$('#vol').value=s.volume;$('#vp').textContent=s.volume+'%'}
 $('#vmu').textContent=s.muted?'🔇':'🔈';
}
api('/api/favs').then(r=>{favIds=new Set(r.favs.map(f=>f.id))});
poll(); loadLocal(); setInterval(poll,2500);
</script></body></html>
"""


def autoresume():
    if state.get("autoplay") and state.get("queue"):
        q = state["queue"][max(state.get("pos", 0), 0):]
        if q:
            start_queue(q)


if __name__ == "__main__":
    kill_stale()
    threading.Thread(target=dl_worker, daemon=True).start()
    threading.Timer(4, autoresume).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
