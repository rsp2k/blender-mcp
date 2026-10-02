#!/usr/bin/env bash
# Build and verify the B. Clip application template (app_template/B_Clip/)
# with headless Blender in the blender-desktop image.
#
#   onboarding/b_clip_template.sh build    regenerate startup.blend, userpref.blend, splash.png
#   onboarding/b_clip_template.sh verify   run the checks; exits non-zero on failure
#   onboarding/b_clip_template.sh all      both
#   onboarding/b_clip_template.sh splash   only re-render splash.png
#
# Env: BLENDER_IMAGE (default the 5.2.2 image below), CLIP_STL (the mascot
# mesh; defaults to onboarding/blender/projects/clip.stl in this checkout or,
# from a git worktree, in the main checkout).
set -euo pipefail

TEMPLATE=B_Clip
IMAGE="${BLENDER_IMAGE:-git.supported.systems/rsp2k/blender-desktop:5.2.2}"

# ---------------------------------------------------------------- in the container

inside_build() {
  local out=/work/app_template/$TEMPLATE prev=/work/onboarding/b_clip_previews
  local script=/work/onboarding/build_b_clip_template.py
  export HOME; HOME=$(mktemp -d)

  echo "== scene"
  blender -b --factory-startup --python-exit-code 1 --python "$script" -- scene "$out" "$prev"

  echo "== layout (GUI under Xvnc)"
  Xvnc :7 -geometry 1920x1080 -depth 24 -SecurityTypes None -localhost >/tmp/xvnc.log 2>&1 &
  sleep 1
  DISPLAY=:7 timeout -s KILL 120 blender --factory-startup "$out/startup.blend" --python "$script" -- layout

  echo "== splash"
  blender -b --factory-startup --python-exit-code 1 --python "$script" -- splash "$out" "$prev"
  cp "$out/splash.png" "$prev/splash.png"

  echo "== userpref.blend (factory, no extensions)"
  local prefs_home; prefs_home=$(mktemp -d)
  HOME=$prefs_home blender -b --factory-startup \
    --python-expr 'import bpy; bpy.ops.wm.save_userpref()'
  cp "$(find "$prefs_home" -name userpref.blend | head -1)" "$out/userpref.blend"
  rm -f "$out"/*.blend1
}

inside_splash() {
  local out=/work/app_template/$TEMPLATE prev=/work/onboarding/b_clip_previews
  export HOME; HOME=$(mktemp -d)
  blender -b --factory-startup --python-exit-code 1 \
    --python /work/onboarding/build_b_clip_template.py -- splash "$out" "$prev"
  cp "$out/splash.png" "$prev/splash.png"
}

inside_verify() {
  local v="/work/onboarding/verify_b_clip_template.py"
  local b="blender -b --python-exit-code 1"
  local fail=0
  step() { echo; echo "== $1"; }
  run() { "$@" || { echo "!! FAILED: $*"; fail=1; }; }
  fresh() { HOME=$(mktemp -d); export HOME; }

  step "register, scene, layout, File > New round trip"
  fresh
  run $b --factory-startup --python "$v" -- install
  run $b --app-template $TEMPLATE --python "$v" -- template

  step "prefs: the template must not overwrite the main add-on list"
  fresh
  run $b --factory-startup --python "$v" -- install
  run $b --python "$v" -- enable-nw
  run $b --app-template $TEMPLATE --python "$v" -- template-save
  run $b --python "$v" -- check-nw

  step "control: same sequence with no userpref.blend in the template"
  fresh
  $b --factory-startup --python "$v" -- install-control >/dev/null || true
  $b --python "$v" -- enable-nw >/dev/null || true
  $b --app-template Control_NoPrefs --python "$v" -- template-save >/dev/null || true
  $b --python "$v" -- check-nw-control || true

  step "BlenderMCP installed but off: the template turns it on"
  fresh
  run $b --factory-startup --python "$v" -- install stub
  run $b --app-template $TEMPLATE --python "$v" -- stub
  run $b --python "$v" -- check-stub-off

  step "GUI: first look (BlenderMCP missing, then installed)"
  Xvnc :7 -geometry 1920x1080 -depth 24 -SecurityTypes None -localhost >/tmp/xvnc.log 2>&1 &
  sleep 1
  for mode in missing stub; do
    fresh
    if [ "$mode" = stub ]; then run $b --factory-startup --python "$v" -- install stub
    else run $b --factory-startup --python "$v" -- install; fi
    run env DISPLAY=:7 timeout -s KILL 60 blender --app-template $TEMPLATE --python "$v" -- gui "$mode"
  done

  echo
  if [ $fail -eq 0 ]; then echo "ALL CHECKS PASSED"; else echo "SOME CHECKS FAILED"; fi
  return $fail
}

if [ "${1:-}" = "--inside" ]; then
  shift
  case "$1" in
    build) inside_build ;;
    splash) inside_splash ;;
    verify) inside_verify ;;
  esac
  exit
fi

# ---------------------------------------------------------------- on the host

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
stl="${CLIP_STL:-$ROOT/onboarding/blender/projects/clip.stl}"
if [ ! -f "$stl" ] && common=$(git -C "$ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null); then
  stl="$(dirname "$common")/onboarding/blender/projects/clip.stl"
fi

container() {
  local extra=()
  [ "$1" != verify ] && {
    [ -f "$stl" ] || { echo "clip.stl not found (set CLIP_STL)"; exit 1; }
    extra=(-v "$stl:/home/blender/projects/clip.stl:ro")
    mkdir -p "$ROOT/onboarding/b_clip_previews"
  }
  docker run --rm --device nvidia.com/gpu=all --user "$(id -u):$(id -g)" \
    -v "$ROOT:/work" "${extra[@]}" --entrypoint /bin/bash "$IMAGE" \
    /work/onboarding/b_clip_template.sh --inside "$1"
}

case "${1:-}" in
  build) container build ;;
  verify) container verify ;;
  all) container build && container verify ;;
  splash) container splash ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
