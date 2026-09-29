# Onboarding screenshots

The install walkthrough on https://blender.bet/#install is built from
real screenshots of a first-time install, taken in a fresh Blender. Rerun
this whenever a new Blender release changes the Preferences UI, or when
the add-on's panel changes enough that the frames look stale.

A full pass takes about 30 minutes, most of it clicking.

## What you need

- `blender-instance` (from `~/claude/blender-docker`). Its image opens on
  the Supported Systems mark by default (`BLENDER_STARTUP_SCENE=mark`), so
  the frames show that instead of the default cube.
- mcvnc to drive the desktop (`claude mcp add mcvnc -- uvx mcvnc`).
- A BlenderMCP account for the login frames. The `claude` account in
  Authentik is used for this; its password is in
  `~/.config/supported-systems/authentik-claude.pass`. To reset it, run
  `ak shell` in `authentik-worker` over
  `ssh -J dell01 deploy@auth.supported.systems` and pipe the new password
  in on stdin, so it never lands in a command line or a log.
- ImageMagick (`magick`) for cropping.

## 1. A clean instance

The instance must not have the add-on, so do not pass `--with-mcp`:

```bash
blender-instance new onboarding/blender --up
blender-instance info onboarding     # VNC port and password
blender-instance watch onboarding    # view-only link for anyone following along
```

To pick up a new Blender, set `BLENDER_VERSION` in `onboarding/blender/.env`
and `make -C onboarding/blender pull up`. To start over from a truly fresh
Blender (no repositories, no prefs), remove the config volume as well:
`make -C onboarding/blender down` then
`docker volume rm blender-onboarding_blender-config` (check the name with
`docker volume ls`).

`build_mark.py` builds the mark scene from the logo SVG's geometry. The
image already ships it, so you only need it if the mark changes:

```bash
cp onboarding/build_mark.py onboarding/blender/projects/
docker exec blender-onboarding blender -b --factory-startup \
    --python /home/blender/projects/build_mark.py
```

## 2. Capture

Connect with `vnc_connect(host="127.0.0.1", port=<VNC_PORT>, password=...)`
and take each frame with a full-resolution `vnc_screenshot` (no
`max_width`). The tool result gives the PNG's path; copy it to
`web/public/img/onboarding/` under the name below.

| File | What is on screen |
|---|---|
| `01-fresh-blender.png` | Blender just opened, mark in the viewport |
| `02-edit-preferences.png` | Edit menu open, pointer on Preferences |
| `03-repositories.png` | Get Extensions, Repositories menu open |
| `04-add-remote-repository.png` | the `+` menu, pointer on Add Remote Repository |
| `05-repository-url.png` | dialog filled with `https://mcp.blender.bet/install`, Check for Updates ticked, pointer on Create |
| `06-repository-added.png` | repository listed with the short URL, Blender MCP under Available |
| `07-install.png` | Blender MCP expanded, pointer on Install |
| `08-installed.png` | Blender MCP under Installed |
| `09-login.png` | after a restart: BlenderMCP tab, Not logged in, pointer on Login |
| `10-consent.png` | Blender waiting beside Authentik's consent screen |
| `11-signed-in.png` | panel reads Connected, browser shows You're signed in |
| `12-update-available.png` | the Update button at the top of the panel |

Between 08 and 09, restart with `make -C onboarding/blender restart`. The
image quits Blender cleanly on stop, so the add-on stays enabled. Frame 12
needs a newer version than the one installed: publish a bump and wait for
the panel to offer it.

For 10 and 11, the login opens the container's Firefox. Type the username,
then the password, and dismiss Firefox's offer to save it before taking
the frame. Close Firefox afterwards (`firefox-bin`, by pid): the `claude`
account is an Authentik admin, and the instance has a public watch link.

### mcvnc habits that save retakes

- Move to a control before clicking it. Blender ignores a click on a
  button it has not seen the pointer hover.
- The first click after a window or popover changes often only focuses.
  Take a screenshot and click again if nothing happened.
- `vnc_type_text` drops the second of two identical characters in a row.
  Type the URL in pieces: `ht`, `tps:/`, `/mcp.blender.bet/instal`, `l`.
  Split passwords the same way.
- After a restart, the first key press is often swallowed. If `N` did not
  open the sidebar, click an empty part of the viewport and press it again.
- A text field commits on the next click, so the first click on the
  checkbox under the URL only commits the URL. Click it a second time.
- If a screenshot fails with "0 bytes read", disconnect and reconnect. The
  restart also drops the session.

## 3. Crop and point

```bash
onboarding/crop_frames.sh
```

It writes `web/public/img/onboarding/web/NN.webp`. If the Preferences
window opened somewhere else, adjust the crop geometry at the top of the
script.

Each step's spotlight is `spot: [left, top, width, height]` in
`web/src/data/install-steps.ts`, in percent of the cropped frame. Take the
control's pixel box from the full frame, subtract the crop's X and Y, and
divide by the crop's width and height. Leave `spot` out for a frame that
is about the whole screen, like step 1.

## 4. Check and ship

```bash
cd web && npx astro build && PORT=4399 node ./dist/server/entry.mjs
```

Open http://127.0.0.1:4399/#install and step through every frame at
desktop width and at about 390px. Each spotlight should sit on its
control, and the console should be clean. Then commit and redeploy the
web container on dell01:

```bash
ssh -A dell01 'cd ~/warehack-ing/blender-mcp && git pull --ff-only && cd web && docker compose up -d --build web'
```

When you're done, `make -C onboarding/blender down` frees the instance's
memory. The config volume is kept, so the next pass can reuse it or
remove it for a clean start.
