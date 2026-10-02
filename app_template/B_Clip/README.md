# B. Clip starter template

A Blender application template for people who have never used Blender.
It opens on **B. Clip** ("B" for short), the BlenderMCP mascot: a dark blue
steel binder clip with big eyes, standing where the default cube usually
is. The 3D Viewport's sidebar is already open on the **BlenderMCP** tab, so
the Chat panel is the first thing you see and you can ask Claude for
changes straight away.

## What's in the folder

| File | What it does |
|---|---|
| `startup.blend` | The scene: B at the origin on the floor grid, a camera framing him, his lights. One 3D Viewport (light and camera overlays off, so nothing is drawn across B) with the sidebar open and wide on the BlenderMCP tab, a small Outliner, no timeline or Properties editor, and only the Layout workspace. |
| `__init__.py` | Adds a small "B. Clip" panel to the BlenderMCP tab for when BlenderMCP isn't running: without it, the panel says how to get it (with a few example requests as plain text); if it is installed but turned off, there's a button to turn it on, and it is turned on a moment after startup anyway. Once BlenderMCP runs, the panel steps aside. |
| `userpref.blend` | Factory preferences with no extensions listed. Required, see below. |
| `splash.png` | The 1000x500 splash: B and "Tell Claude what to make". |

Everything the template needs at runtime is in this folder.

**Starter prompts come from BlenderMCP, not from this folder.** The server
serves them as MCP prompts, and BlenderMCP's Chat panel shows them as
buttons in a new chat ("Make B wave", "Give B a top hat", "Put B on a
desk" while this template is active or B is in the scene, scene-agnostic
ones otherwise). Clicking one fills the message box without sending, so
you can edit it and press Enter. Changing them is a server change; see
`src/blender_mcp/starter_prompts.py` and the Prompts reference in the docs.

## Install it by hand

Copy the whole `B_Clip` folder into Blender's user templates folder, then
pick **File > New > B Clip** (it is also on the splash screen's list).

| System | Folder (Blender 5.2) |
|---|---|
| Linux | `~/.config/blender/5.2/scripts/startup/bl_app_templates_user/` |
| macOS | `~/Library/Application Support/Blender/5.2/scripts/startup/bl_app_templates_user/` |
| Windows | `%APPDATA%\Blender Foundation\Blender\5.2\scripts\startup\bl_app_templates_user\` |

Blender can tell you the exact path for your setup: in its Python console,
run `bpy.utils.user_resource('SCRIPTS', path="startup/bl_app_templates_user")`.
Or zip the folder (with `B_Clip/` at the top of the zip) and use the Blender
logo menu > **Install Application Template...**.

To start Blender straight into it: `blender --app-template B_Clip`.

BlenderMCP itself installs from <https://mcp.blender.bet/install>; the
quickstart is at <https://docs.blender.bet/tutorials/quickstart/>.

## Things worth knowing

**Why `userpref.blend` has to be here.** In Blender 5.2, while a template
without its own `userpref.blend` is active, Blender starts from factory
add-ons, and the next preferences save (including auto-save on quit)
writes that factory list over your main preferences. You would lose every
add-on you had turned on, BlenderMCP included. With this file present,
add-on, theme and keymap changes made inside B. Clip are saved to
`config/B_Clip/userpref.blend` and your main preferences keep their
add-ons. The flip side: while B. Clip is open you get Blender's default
theme and keymap. `onboarding/b_clip_template.sh verify` checks both
sides of this on every run.

**The sidebar tab and width are written into `startup.blend`.** Blender's
Python API can open the sidebar but can't set its width or choose its tab
(`Region.width` is read-only and `Region.active_panel_category` refuses
assignment in 5.2). The build saves the file from a real Blender window,
then `onboarding/blend_patch.py` rewrites those two fields in place. The
template's own panel uses the BlenderMCP tab name, so the tab exists even
before BlenderMCP is installed.

**The folder name is `B_Clip`, not `b_clip`.** Blender sorts template
folders by plain string order, and the splash only lists the first four
(`TOPBAR_MT_file_new.draw_ex` in Blender's `space_topbar.py`). Lowercase
names sort after the capitalised stock templates, so `b_clip` ended up
behind "More...", while `B_Clip` lands second, right after 2D Animation.
Both show as "B Clip" in menus. The period in "B. Clip" can't be part of a
folder name (it must be a Python identifier), so the full name lives in
the scene and the splash.

## Rebuild

From the repo root, with Docker and the GPU runtime available:

```bash
onboarding/b_clip_template.sh build    # startup.blend, userpref.blend, splash.png
onboarding/b_clip_template.sh verify   # checks; exits non-zero on failure
onboarding/b_clip_template.sh splash   # only the splash, for quick iteration
```

It runs headless Blender 5.2.2 from the `blender-desktop` image (override
with `BLENDER_IMAGE`). The mesh, `clip.stl`, is kept out of git: put it at
`onboarding/blender/projects/clip.stl` or point `CLIP_STL` at it. From a
git worktree the script also looks in the main checkout. B is built by
`onboarding/build_clip_mascot.py` (hero pose, face, rig, materials); the
template's scene, layout and splash come from
`onboarding/build_b_clip_template.py`. Preview renders and screenshots of
the first look land in `onboarding/b_clip_previews/`.

## Credits

B's binder clip mesh is by **teen-wolf** on CGTrader
(<https://www.cgtrader.com/designers/teen-wolf>); its licence allows
redistribution. The face, rig, materials and staging were added for
BlenderMCP. The splash text is set in Inter, the font Blender ships for
its own interface.
