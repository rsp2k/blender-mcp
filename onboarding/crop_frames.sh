#!/usr/bin/env bash
# Crop the captured onboarding frames into the WebP files the homepage
# walkthrough shows. Masters stay as PNG in web/public/img/onboarding/;
# output goes to web/public/img/onboarding/web/NN.webp.
#
# Crops are at native pixels (no resampling) so Blender's small UI text
# stays sharp. Full-frame shots are downscaled to 1440 wide instead.
# If a new Blender moves the Preferences window, adjust the geometry here
# and the spot percentages in web/src/data/install-steps.ts to match.
set -euo pipefail

cd "$(dirname "$0")/../web/public/img/onboarding"
mkdir -p web

# name                          crop (WxH+X+Y, or - for none)   resize
frames=(
  "01-fresh-blender             -                  1440x"
  "02-edit-preferences          -                  -"
  "03-repositories              -                  -"
  "04-add-remote-repository     852x560+534+260    -"
  "05-repository-url            852x560+534+260    -"
  "06-repository-added          852x560+534+260    -"
  "07-install                   852x560+534+260    -"
  "08-installed                 852x560+534+260    -"
  "09-login                     1010x660+560+50    -"
  "10-consent                   -                  1440x"
  "11-signed-in                 -                  1440x"
  "12-update-available          760x460+300+30     -"
)

for row in "${frames[@]}"; do
  read -r name crop resize <<<"$row"
  src="$name.png"
  [ -f "$src" ] || { echo "missing $src" >&2; exit 1; }
  args=("$src")
  [ "$crop" != "-" ] && args+=(-crop "$crop" +repage)
  [ "$resize" != "-" ] && args+=(-resize "$resize")
  out="web/${name%%-*}.webp"
  magick "${args[@]}" -strip -quality 82 "$out"
  printf '%-32s %s\n' "$out" "$(magick identify -format '%wx%h' "$out")"
done
