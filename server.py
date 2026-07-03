#!/usr/bin/env python3
"""
Wall — a tiny multi-venue video-wall controller.

One small server hosts many venues ("sites"). Each site has its own wall layout,
logo and displays. A display just opens a full-screen browser at:

    http://<server>:8080/s/<site>/display/<id>

Upload an image on a site's control page -> it maps across that site's wall by
each screen's real physical size/position. Nothing loaded -> the site's logo.

No database. Every site is just a folder under ./sites/<slug>/ containing:
    wall_config.json   default_logo.png   state/

Free and open source. Cheap to host: a small VPS or a Raspberry Pi.

Optional password: set WALL_PASSWORD to gate the control pages (display pages
stay open so the Pis can always reach them).
"""

import io
import json
import os
import re
import functools
from pathlib import Path

from flask import (Flask, request, redirect, send_file, jsonify, session,
                   render_template_string, abort, url_for, flash)
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent
SITES_DIR = BASE / "sites"
MAX_CANVAS_PX = 6000
SITES_DIR.mkdir(exist_ok=True)

PASSWORD = os.environ.get("WALL_PASSWORD", "")  # empty = open (local mode)

app = Flask(__name__)
app.secret_key = os.environ.get("WALL_SECRET", os.urandom(24).hex())
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

STARTER_CONFIG = {
    "wall_name": "New venue",
    "fit": "contain",
    "background": "#000000",
    "displays": [
        {"id": 1, "name": "screen 1", "x_mm": 0,   "y_mm": 0, "w_mm": 900, "h_mm": 500, "px_w": 1920, "px_h": 1080},
        {"id": 2, "name": "screen 2", "x_mm": 950, "y_mm": 0, "w_mm": 900, "h_mm": 500, "px_w": 1920, "px_h": 1080},
    ],
}


# ------------------------------------------------------------------ auth (optional)
def admin_required(view):
    @functools.wraps(view)
    def wrapped(*a, **k):
        if PASSWORD and not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return view(*a, **k)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not PASSWORD:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if request.form.get("password") == PASSWORD:
            session["ok"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Wrong password.")
    return render_template_string(LOGIN_HTML)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ site helpers
def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "site"


def list_sites():
    return sorted(p.name for p in SITES_DIR.iterdir()
                  if p.is_dir() and (p / "wall_config.json").exists())


def sdir(slug):      return SITES_DIR / slug
def cfg_path(slug):  return sdir(slug) / "wall_config.json"
def logo_path(slug): return sdir(slug) / "default_logo.png"
def state_dir(slug): d = sdir(slug) / "state"; d.mkdir(parents=True, exist_ok=True); return d
def master_path(slug):  return state_dir(slug) / "master.png"
def version_path(slug): return state_dir(slug) / "version.txt"


def site_exists(slug):
    return cfg_path(slug).exists()


def load_cfg(slug):
    with open(cfg_path(slug)) as f:
        return json.load(f)


def save_cfg(slug, cfg):
    cfg_path(slug).write_text(json.dumps(cfg, indent=2))


def get_version(slug):
    try:
        return int(version_path(slug).read_text().strip())
    except Exception:
        return 0


def bump_version(slug):
    v = get_version(slug) + 1
    version_path(slug).write_text(str(v))
    return v


def display_by_id(cfg, did):
    return next((d for d in cfg["displays"] if str(d["id"]) == str(did)), None)


def make_logo(text, path):
    W, H = 2400, 900
    img = Image.new("RGB", (W, H), "#0c0a08")
    d = ImageDraw.Draw(img)

    def font(sz):
        try:
            return ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", sz)
        except Exception:
            return ImageFont.load_default()
    d.text((W / 2, H / 2), text.upper(), font=font(200), fill="#e8a33d", anchor="mm")
    img.save(path)


def create_site(name):
    slug = slugify(name)
    if site_exists(slug):
        return None
    sdir(slug).mkdir(parents=True, exist_ok=True)
    cfg = dict(STARTER_CONFIG)
    cfg["wall_name"] = name.strip() or "New venue"
    save_cfg(slug, cfg)
    make_logo(name, logo_path(slug))
    rebuild(slug)
    return slug


# ------------------------------------------------------------------ image slicing
def canvas_bounds(cfg):
    ds = cfg["displays"]
    minx = min(d["x_mm"] for d in ds)
    miny = min(d["y_mm"] for d in ds)
    W = max(d["x_mm"] + d["w_mm"] for d in ds) - minx
    H = max(d["y_mm"] + d["h_mm"] for d in ds) - miny
    return minx, miny, W, H


def render_canvas(src_img, cfg):
    minx, miny, cw_mm, ch_mm = canvas_bounds(cfg)
    ppm = max(d["px_w"] / d["w_mm"] for d in cfg["displays"])
    long_mm = max(cw_mm, ch_mm)
    if long_mm * ppm > MAX_CANVAS_PX:
        ppm = MAX_CANVAS_PX / long_mm
    cw, ch = max(1, round(cw_mm * ppm)), max(1, round(ch_mm * ppm))
    canvas = Image.new("RGB", (cw, ch), cfg.get("background", "#000000"))
    img = src_img.convert("RGBA")
    fit = cfg.get("fit", "contain")
    if fit == "stretch":
        placed = img.resize((cw, ch), Image.LANCZOS)
        canvas.paste(placed, (0, 0), placed)
    else:
        scale = (min(cw / img.width, ch / img.height) if fit == "contain"
                 else max(cw / img.width, ch / img.height))
        nw, nh = max(1, round(img.width * scale)), max(1, round(img.height * scale))
        placed = img.resize((nw, nh), Image.LANCZOS)
        canvas.paste(placed, ((cw - nw) // 2, (ch - nh) // 2), placed)
    return canvas, minx, miny, ppm


def current_source(slug, cfg):
    if master_path(slug).exists():
        return Image.open(master_path(slug))
    if logo_path(slug).exists():
        return Image.open(logo_path(slug))
    return Image.new("RGB", (1920, 1080), cfg.get("background", "#000000"))


def reslice(slug):
    cfg = load_cfg(slug)
    canvas, minx, miny, ppm = render_canvas(current_source(slug, cfg), cfg)
    for d in cfg["displays"]:
        l = round((d["x_mm"] - minx) * ppm)
        t = round((d["y_mm"] - miny) * ppm)
        r = round((d["x_mm"] - minx + d["w_mm"]) * ppm)
        b = round((d["y_mm"] - miny + d["h_mm"]) * ppm)
        tile = canvas.crop((l, t, r, b)).resize((d["px_w"], d["px_h"]), Image.LANCZOS)
        tile.save(state_dir(slug) / f"slice_{d['id']}.png")


def rebuild(slug):
    reslice(slug)
    return bump_version(slug)


# ------------------------------------------------------------------ display (Pi) pages
DISPLAY_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{{ slug }} · {{ did }}</title>
<style>html,body{margin:0;height:100%;background:#000;overflow:hidden;cursor:none}
img{position:absolute;inset:0;width:100%;height:100%;object-fit:fill;transition:opacity .35s}</style>
</head><body><img id=a><img id=b style=opacity:0><script>
var base='/s/{{ slug }}',did={{ did }},ver=-1,front=document.getElementById('a'),back=document.getElementById('b');
function swap(v){back.onload=function(){back.style.opacity=1;front.style.opacity=0;var t=front;front=back;back=t;ver=v};
back.src=base+'/display/'+did+'/image?v='+v}
function poll(){fetch(base+'/version',{cache:'no-store'}).then(r=>r.json()).then(d=>{if(d.version!==ver)swap(d.version)}).catch(()=>{})}
setInterval(poll,2000);poll();</script></body></html>"""


@app.route("/s/<slug>/display/<did>")
def display_page(slug, did):
    if not site_exists(slug) or not display_by_id(load_cfg(slug), did):
        abort(404)
    return render_template_string(DISPLAY_HTML, slug=slug, did=int(did))


@app.route("/s/<slug>/display/<did>/image")
def display_image(slug, did):
    p = state_dir(slug) / f"slice_{did}.png"
    if not p.exists():
        abort(404)
    resp = send_file(p, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/s/<slug>/version")
def version(slug):
    if not site_exists(slug):
        abort(404)
    return jsonify(version=get_version(slug))


# ------------------------------------------------------------------ dashboard
DASH_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Wall · venues</title>
<style>{{ css|safe }}</style></head><body>
<header><b>WALL</b><span>venues</span>{{ logout|safe }}</header>
<main>
  {% for m in messages %}<div class=flash>{{ m }}</div>{% endfor %}
  <div class=grid>
  {% for s in sites %}
    <a class=card href="/s/{{ s.slug }}">
      <div class=wall style="aspect-ratio:{{ s.cw }}/{{ s.ch }}">
        {% for d in s.displays %}<div class=tile style="left:{{ d.l }}%;top:{{ d.t }}%;width:{{ d.w }}%;height:{{ d.h }}%">
          <img src="/s/{{ s.slug }}/display/{{ d.id }}/image?v={{ s.ver }}"></div>{% endfor %}
      </div>
      <div class=meta><b>{{ s.name }}</b><span>{{ s.n }} screens · /s/{{ s.slug }}</span></div>
    </a>
  {% endfor %}
    <form class="card new" method=post action="/new">
      <b>+ New venue</b>
      <input name=name placeholder="e.g. Central Station" required>
      <button>Create</button>
    </form>
  </div>
</main></body></html>"""

PANEL_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{{ cfg.wall_name }} · control</title>
<style>{{ css|safe }}</style></head><body>
<header><b>WALL</b><span>{{ cfg.wall_name }}</span>
  <a class=lnk href="/">all venues</a><a class=lnk href="/s/{{ slug }}/settings">layout</a>{{ logout|safe }}</header>
<main>
  {% for m in messages %}<div class=flash>{{ m }}</div>{% endfor %}
  <form id=f method=post action="/s/{{ slug }}/upload" enctype=multipart/form-data>
    <div class=drop id=drop><h2>Drop an image, or tap to choose</h2>
      <p>maps across all {{ cfg.displays|length }} screens</p>
      <input id=file type=file name=image accept="image/*" hidden></div>
    <div class=row>
      <label>Fit</label><select name=fit>
        <option value=contain {{ 'selected' if cfg.fit=='contain' }}>Contain</option>
        <option value=cover {{ 'selected' if cfg.fit=='cover' }}>Cover</option>
        <option value=stretch {{ 'selected' if cfg.fit=='stretch' }}>Stretch</option></select>
      <label>Background</label><input type=color name=background value="{{ cfg.background }}">
      <button class=go type=submit>Send to wall</button>
      <button class=ghost type=button onclick="reset()">Reset to logo</button>
    </div>
  </form>
  <div class=preview><h3>Live preview — v<span id=ver>{{ version }}</span></h3>
    <div class=wall id=wall style="aspect-ratio:{{ cw }}/{{ ch }}"></div>
    <div class=hint>Amber outlines are screens; black between them is wall behind the gaps.</div></div>
</main><script>
var slug='{{ slug }}',displays={{ displays_json|safe }},bounds={{ bounds_json|safe }},ver={{ version }};
var wall=document.getElementById('wall');
function draw(){wall.innerHTML='';displays.forEach(function(d){var t=document.createElement('div');t.className='tile';
 t.style.left=((d.x_mm-bounds.minx)/bounds.w*100)+'%';t.style.top=((d.y_mm-bounds.miny)/bounds.h*100)+'%';
 t.style.width=(d.w_mm/bounds.w*100)+'%';t.style.height=(d.h_mm/bounds.h*100)+'%';
 var im=new Image();im.src='/s/'+slug+'/display/'+d.id+'/image?v='+ver;
 var lb=document.createElement('b');lb.textContent=d.id;t.appendChild(im);t.appendChild(lb);wall.appendChild(t)})}
function refresh(){fetch('/s/'+slug+'/version',{cache:'no-store'}).then(r=>r.json()).then(d=>{
 if(d.version!==ver){ver=d.version;document.getElementById('ver').textContent=ver;draw()}})}
draw();setInterval(refresh,1500);
var drop=document.getElementById('drop'),file=document.getElementById('file');
drop.onclick=function(){file.click()};file.onchange=function(){if(file.files.length)document.getElementById('f').submit()};
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('hot')}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('hot')}));
drop.addEventListener('drop',ev=>{file.files=ev.dataTransfer.files;document.getElementById('f').submit()});
function reset(){fetch('/s/'+slug+'/reset',{method:'POST'}).then(()=>location.reload())}
</script></body></html>"""

SETTINGS_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{{ slug }} · layout</title>
<style>{{ css|safe }}</style></head><body>
<header><b>WALL</b><span>{{ slug }} · layout</span><a class=lnk href="/s/{{ slug }}">control</a>{{ logout|safe }}</header>
<main>{% for m in messages %}<div class=flash>{{ m }}</div>{% endfor %}
  <p class=hint>Positions and sizes in millimetres. x_mm / y_mm = top-left corner of the glass from the
  top-left of the whole wall. w_mm / h_mm = active picture size. px_w / px_h = native resolution.
  Gaps are worked out automatically. The Pi for each screen opens
  <code>/s/{{ slug }}/display/&lt;id&gt;</code>.</p>
  <form method=post action="/s/{{ slug }}/settings">
    <textarea name=config spellcheck=false>{{ config_text }}</textarea>
    <div class=row><button class=go>Save layout</button></div>
  </form></main></body></html>"""

LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Wall · sign in</title>
<style>body{background:#0c0a08;color:#f2ede4;font:15px ui-monospace,monospace;display:grid;place-items:center;height:100vh;margin:0}
form{background:#16130f;border:1px solid #2a2622;border-radius:10px;padding:26px;display:grid;gap:12px;width:280px}
input{background:#0c0a08;color:#f2ede4;border:1px solid #2a2622;border-radius:6px;padding:10px}
button{background:#e8a33d;color:#1a1206;border:0;border-radius:6px;padding:11px;font-weight:700;cursor:pointer}
b{color:#e8a33d;letter-spacing:1px}</style></head><body>
<form method=post><b>WALL</b><input type=password name=password placeholder=password autofocus>
<button>Sign in</button></form></body></html>"""

CSS = """
:root{--ink:#f2ede4;--dim:#8b8578;--line:#2a2622;--amber:#e8a33d;--panel:#16130f}
*{box-sizing:border-box}body{margin:0;background:#0c0a08;color:var(--ink);font:15px/1.5 ui-monospace,Menlo,monospace}
header{padding:20px 26px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:14px}
header b{font-size:19px;letter-spacing:.5px}header span{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:2px}
.lnk{margin-left:6px;color:var(--dim);text-decoration:none;font-size:12px;border-bottom:1px solid transparent}
.lnk:hover{color:var(--ink);border-color:var(--amber)}
header .lnk:last-of-type,header .out{margin-left:auto}
main{max-width:1080px;margin:0 auto;padding:26px}
.flash{background:#3a1c14;border:1px solid #7a3a24;color:#f0c9b8;padding:10px 14px;border-radius:8px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;text-decoration:none;color:inherit;display:block}
.card:hover{border-color:var(--amber)}
.card .wall{position:relative;width:100%;background:#000;border-radius:6px;overflow:hidden}
.meta{margin-top:10px}.meta b{display:block}.meta span{color:var(--dim);font-size:12px}
.card.new{display:flex;flex-direction:column;gap:10px;justify-content:center;align-items:stretch}
.card.new b{color:var(--amber)}
.wall{position:relative;width:100%;background:#000;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.tile{position:absolute;overflow:hidden;outline:1px solid rgba(232,163,61,.35)}
.tile img{width:100%;height:100%;object-fit:fill;display:block}
.tile b{position:absolute;left:4px;top:3px;font-size:10px;color:#fff;text-shadow:0 0 4px #000}
.drop{border:1.5px dashed var(--line);border-radius:10px;padding:32px;text-align:center;background:var(--panel);cursor:pointer}
.drop.hot{border-color:var(--amber);background:#1c1811}.drop h2{margin:0 0 6px;font-size:16px}.drop p{margin:0;color:var(--dim);font-size:13px}
.row{display:flex;gap:14px;flex-wrap:wrap;align-items:center;margin:20px 0}
label{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:1px}
select,input[type=color],input[type=text],input:not([type]){background:#0c0a08;color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:8px}
button{font:inherit;border:0;border-radius:6px;padding:11px 20px;cursor:pointer}
.go{background:var(--amber);color:#1a1206;font-weight:700}.ghost{background:transparent;color:var(--dim);border:1px solid var(--line)}
.go:hover{filter:brightness(1.07)}.ghost:hover{color:var(--ink)}
.preview{margin-top:26px}.preview h3{font-size:12px;letter-spacing:1px;text-transform:uppercase;color:var(--dim)}
.hint{color:var(--dim);font-size:12px;margin-top:10px}code{color:var(--amber)}
textarea{width:100%;height:52vh;background:#0c0a08;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:14px;font:13px/1.5 ui-monospace,monospace}
"""


def logout_link():
    return '<a class="lnk out" href="/logout">sign out</a>' if PASSWORD else ""


@app.route("/")
@admin_required
def dashboard():
    sites = []
    for slug in list_sites():
        cfg = load_cfg(slug)
        minx, miny, W, H = canvas_bounds(cfg)
        tiles = [{"id": d["id"],
                  "l": (d["x_mm"] - minx) / W * 100, "t": (d["y_mm"] - miny) / H * 100,
                  "w": d["w_mm"] / W * 100, "h": d["h_mm"] / H * 100} for d in cfg["displays"]]
        sites.append({"slug": slug, "name": cfg.get("wall_name", slug),
                      "n": len(cfg["displays"]), "cw": W, "ch": H,
                      "ver": get_version(slug), "displays": tiles})
    return render_template_string(DASH_HTML, sites=sites, css=CSS,
                                  logout=logout_link(), messages=get_flashed())


@app.route("/new", methods=["POST"])
@admin_required
def new_site():
    slug = create_site(request.form.get("name", ""))
    if not slug:
        flash("A venue with that name already exists.")
        return redirect(url_for("dashboard"))
    return redirect(url_for("panel", slug=slug))


@app.route("/s/<slug>")
@admin_required
def panel(slug):
    if not site_exists(slug):
        abort(404)
    cfg = load_cfg(slug)
    minx, miny, W, H = canvas_bounds(cfg)
    return render_template_string(
        PANEL_HTML, slug=slug, cfg=cfg, css=CSS, version=get_version(slug),
        cw=W, ch=H, displays_json=json.dumps(cfg["displays"]),
        bounds_json=json.dumps({"minx": minx, "miny": miny, "w": W, "h": H}),
        logout=logout_link(), messages=get_flashed())


@app.route("/s/<slug>/settings", methods=["GET", "POST"])
@admin_required
def settings(slug):
    if not site_exists(slug):
        abort(404)
    if request.method == "POST":
        try:
            cfg = json.loads(request.form["config"])
            assert cfg["displays"] and all(
                {"id", "x_mm", "y_mm", "w_mm", "h_mm", "px_w", "px_h"} <= set(d)
                for d in cfg["displays"]), "each display needs id,x_mm,y_mm,w_mm,h_mm,px_w,px_h"
            save_cfg(slug, cfg)
            rebuild(slug)
            flash("Layout saved.")
            return redirect(url_for("panel", slug=slug))
        except Exception as e:
            flash(f"Not saved: {e}")
    return render_template_string(
        SETTINGS_HTML, slug=slug, css=CSS,
        config_text=json.dumps(load_cfg(slug), indent=2),
        logout=logout_link(), messages=get_flashed())


@app.route("/s/<slug>/upload", methods=["POST"])
@admin_required
def upload(slug):
    if not site_exists(slug):
        abort(404)
    cfg = load_cfg(slug)
    changed = False
    if request.form.get("fit"):
        cfg["fit"] = request.form["fit"]; changed = True
    if request.form.get("background"):
        cfg["background"] = request.form["background"]; changed = True
    if changed:
        save_cfg(slug, cfg)
    f = request.files.get("image")
    if f and f.filename:
        Image.open(io.BytesIO(f.read())).save(master_path(slug))
    rebuild(slug)
    return redirect(url_for("panel", slug=slug))


@app.route("/s/<slug>/reset", methods=["POST"])
@admin_required
def reset(slug):
    if not site_exists(slug):
        abort(404)
    if master_path(slug).exists():
        master_path(slug).unlink()
    rebuild(slug)
    return ("", 204)


# small helper so templates get flash messages without Jinja globals fuss
def get_flashed():
    from flask import get_flashed_messages
    return get_flashed_messages()


# rebuild every site's slices on startup so a fresh boot always shows something
for _s in list_sites():
    try:
        rebuild(_s)
    except Exception as e:
        print("skip", _s, e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, threaded=True)
