# VenueWall

A tiny, free, self-hostable video-wall controller for many venues.

Drive displays of any size and make-up from cheap Raspberry Pis. Drop an image
on a venue's control page and it maps across that venue's whole wall by each
screen's real physical size and position — gaps and mixed resolutions handled
automatically. No image → the venue's logo.

One small server hosts as many venues as you like: **Live Rooms 1**,
**Live Rooms 2**, **Central Station**… each with its own layout, logo and
screens. No database. Cheap to run — a £4/month VPS or a spare Pi.

## How it works

- **One controller** runs `server.py` (Flask + Pillow, one file).
- Each **venue** is a folder: `sites/<slug>/` with `wall_config.json`,
  `default_logo.png`, and a runtime `state/`.
- Each **display** is a Pi that boots into a browser at
  `http://<server>/s/<venue>/display/<id>`. Nothing to maintain on the Pis.
- Images are sliced by physical millimetres, so a logo looks right across a
  32" next to a 50", with the gaps falling behind the wall.

The controller only serves images and small config, so venue Pis can pull over
the internet from one central host, or over the LAN from a Pi in the building.

## Run the controller

```bash
git clone https://github.com/<you>/wall.git && cd wall
pip install -r requirements.txt        # or: pip install flask pillow --break-system-packages
python3 server.py                      # open http://<server>:8080
```

Boot on startup (systemd): copy `controller.service` to
`/etc/systemd/system/wall.service`, edit the paths/user, then
`sudo systemctl enable --now wall`.

Protect the control pages (recommended for anything internet-facing):

```bash
export WALL_PASSWORD=something-strong   # display pages stay open; control needs login
```

Put it behind a reverse proxy (Caddy/nginx) for HTTPS if it's on the internet.

## Add a venue

Two ways:

- **In the browser:** on the dashboard, "＋ New venue" → name it → it appears.
- **By hand:** copy an existing folder under `sites/`, rename it, edit
  `wall_config.json`, drop in a `default_logo.png`.

Then open the venue → **layout** and enter each screen in millimetres: `x_mm`,
`y_mm` (top-left corner from the top-left of the whole wall), `w_mm`, `h_mm`
(active picture size), and `px_w`, `px_h` (native resolution). The live preview
shows the amber screen rectangles — nudge the numbers until they match the wall.

Two example venues ship in `sites/`: `live-rooms-1` (six mixed-size screens) and
`central-station` (three).

## Set up each display Pi

```bash
./setup-display-pi.sh <server> <venue-slug> <display-id>
# e.g.
./setup-display-pi.sh wall.example.com live-rooms-1 3
sudo reboot
```

The `<display-id>` must match an `id` in that venue's layout. Use wired Ethernet.

## Daily use

Open the venue's control page, drag an image in → **Send to wall**. Back to the
logo → **Reset to logo**. Screens update within ~2 seconds.

## Notes & roadmap

- **Hardware:** Pi 4 suits 1080p screens (hardware H.264); Pi 5 for 4K panels.
  One Pi per screen keeps any failure to a single dark screen.
- **Fit:** *Contain* (whole logo, letterboxed), *Cover* (fills, may crop),
  *Stretch*. Set per upload.
- **Not built yet:** live RTSP (football) — ingest once, re-broadcast to the
  Pis; scheduling/playlists; in-browser drag-to-place layout editor. PRs welcome.

## License

MIT. Free to use, change and deploy.
