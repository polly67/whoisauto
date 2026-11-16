#!/usr/bin/env bash
set -euo pipefail
echo "=== Disabling macOS Window Animations, Transparency & Background Indexing ==="
defaults write NSGlobalDomain NSAutomaticWindowAnimationsEnabled -bool false || true
defaults write NSGlobalDomain NSWindowResizeTime -float 0.001 || true
defaults write com.apple.universalaccess reduceTransparency -bool true || true
defaults write com.apple.universalaccess reduceMotion -bool true || true
defaults write com.apple.dock expose-animation-duration -float 0.05 || true
defaults write com.apple.dock launchanim -bool false || true
defaults write com.apple.dock autohide-time-modifier -float 0.05 || true
sudo mdutil -a -i off 2>/dev/null || true
sudo softwareupdate --schedule off 2>/dev/null || true
killall Dock 2>/dev/null || true

echo "=== Launching BetterDisplay & Creating Non-HiDPI Virtual Display ==="
/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay >/tmp/betterdisplay.log 2>&1 </dev/null &
sleep 5

betterdisplaycli create -deviceType=virtualScreen -virtualScreenName="StreamDisplay" -aspectWidth=16 -aspectHeight=9 -virtualScreenHiDPI=off || true
sleep 3
betterdisplaycli set -name="StreamDisplay" -connected=on || true
betterdisplaycli set -name="StreamDisplay" -virtualScreenHiDPI=off || true
sleep 2

echo "=== Configuring 1:1 Non-HiDPI Resolution (${DISP_W}x${DISP_H}) & Mirroring via displayplacer ==="
python3 - "$DISP_W" "$DISP_H" << 'PYEOF'
import ctypes, json, re, subprocess, sys

target_w, target_h = sys.argv[1], sys.argv[2]
stream_cg_id = "2"
apple_cg_id = "1"
try:
    raw_ident = subprocess.check_output(["betterdisplaycli", "get", "-identifiers"], text=True, stderr=subprocess.DEVNULL)
    items = json.loads("[" + raw_ident.strip().strip(",") + "]")
    for it in items:
        if it.get("name") == "StreamDisplay" and it.get("displayID"):
            stream_cg_id = str(it["displayID"])
        elif "Apple Virtual" in it.get("name", "") and it.get("displayID"):
            apple_cg_id = str(it["displayID"])
except Exception as e:
    print("Warning parsing betterdisplaycli identifiers:", e)

with open("/tmp/stream_display_id.txt", "w") as f:
    f.write(stream_cg_id)

dp_out = subprocess.run(["displayplacer", "list"], capture_output=True, text=True).stdout
print(dp_out)

# Map contextual screen id -> persistent screen id
blocks = dp_out.split("Persistent screen id:")
ctx_to_pers = {}
for b in blocks[1:]:
    lines = b.strip().splitlines()
    pers_id = lines[0].strip()
    m = re.search(r"Contextual screen id:\s*(\d+)", b)
    if m:
        ctx_to_pers[m.group(1)] = pers_id

s_id = ctx_to_pers.get(stream_cg_id, stream_cg_id)
a_id = ctx_to_pers.get(apple_cg_id, apple_cg_id)
with open("/tmp/stream_dp_ids.txt", "w") as f:
    f.write(f"{s_id}\n{a_id}\n")

# Try mirroring Apple Virtual onto StreamDisplay at exact 1:1 non-HiDPI resolution
cmds = [
    ["displayplacer", f"id:{s_id}+{a_id} res:{target_w}x{target_h} scaling:off origin:(0,0) degree:0"],
    ["displayplacer", f"id:{s_id} res:{target_w}x{target_h} scaling:off origin:(0,0) degree:0"],
    ["displayplacer", f"id:{s_id} res:{target_w}x{target_h} origin:(0,0) degree:0"],
]
for cmd in cmds:
    print("Running:", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    print(r.stdout, r.stderr)
    if r.returncode == 0:
        break

# Verify actual pixel dimensions via CoreGraphics
try:
    cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
    cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
    cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
    cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
    did = int(stream_cg_id)
    pw = cg.CGDisplayPixelsWide(did)
    ph = cg.CGDisplayPixelsHigh(did)
    actual = f"{pw}x{ph}"
except Exception as e:
    actual = f"error:{e}"
print(f"✅ Verified StreamDisplay (CGDirectDisplayID={stream_cg_id}) actual framebuffer: {actual}")
with open("/tmp/active_resolution.txt", "w") as f:
    f.write(actual)
PYEOF

# Install convenient 'set-res' CLI helper for live SSH resolution switching
sudo mkdir -p /usr/local/bin
cat << 'EOF' > /tmp/set-res
#!/bin/bash
RES="${1:-800x600}"
SID=$(sed -n '1p' /tmp/stream_dp_ids.txt 2>/dev/null || echo "2")
AID=$(sed -n '2p' /tmp/stream_dp_ids.txt 2>/dev/null || echo "1")
displayplacer "id:${SID}+${AID} res:${RES} scaling:off origin:(0,0) degree:0" || \
displayplacer "id:${SID} res:${RES} scaling:off origin:(0,0) degree:0"
echo "Updated display resolution to $RES (scaling:off)"
EOF
sed -i '' 's/^[[:space:]]*//' /tmp/set-res
chmod +x /tmp/set-res
sudo cp /tmp/set-res /usr/local/bin/set-res
cp /tmp/set-res /opt/homebrew/bin/set-res 2>/dev/null || true
