#!/usr/bin/env bash
# Log deletions in Blender's shared extension wheel folder, with every
# Blender process running at that moment and what launched it.
#
# Something emptied this folder under a running Blender on 2026-10-10 and
# took BlenderMCP's packages with it. A process that deletes the files is
# still alive while it does, so a ps snapshot taken on the event names it.
#
#   scripts/diag/watch_extension_wheels.sh [site-packages dir] [log file]
#
# Run it detached:
#   systemd-run --user --unit=blender-wheel-watch -p Restart=always \
#     scripts/diag/watch_extension_wheels.sh
set -u

DIR=${1:-$HOME/.config/blender/5.2/extensions/.local/lib/python3.14/site-packages}
LOG=${2:-$HOME/.local/state/blender-wheel-watch.log}
mkdir -p "$(dirname "$LOG")"

snapshot() {
    echo "  processes:"
    # Only processes whose program is blender (argv[0] ends in /blender or is blender).
    ps -eo pid=,ppid=,etimes=,args= | awk '$4 ~ /(^|\/)blender$/' | while read -r pid ppid age args; do
        parent=$(ps -o args= -p "$ppid" 2>/dev/null | cut -c1-160)
        printf '    pid %s, up %ss: %s\n      parent %s: %s\n' "$pid" "$age" "${args:0:200}" "$ppid" "$parent"
    done
}

echo "$(date -Is) watching $DIR" >> "$LOG"
while true; do
    mkdir -p "$DIR"
    last=""
    # delete_self ends the watch when the folder itself goes; the outer loop
    # recreates the watch on the new folder.
    inotifywait -m -q -e delete -e moved_from -e delete_self --format '%e %f' "$DIR" |
    while read -r ev name; do
        now=$(date -Is)
        echo "$now $ev $name" >> "$LOG"
        # One process snapshot per second: a wipe deletes many entries at once.
        if [ "$now" != "$last" ]; then
            snapshot >> "$LOG"
            last=$now
        fi
    done
    echo "$(date -Is) watch ended, restarting" >> "$LOG"
    sleep 1
done
