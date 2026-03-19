#!/bin/bash
pkill -f "python3.*lofi.py" 2>/dev/null
pkill -f "mpv.*lofi-mpv" 2>/dev/null
sleep 0.5
nohup /Users/aidan/.pyenv/versions/3.12.4/bin/python3 /Users/aidan/apps/low-fi-music-player/lofi.py > /tmp/lofi-player.log 2>&1 &
echo "Lofi player started (PID: $!)"
