"""Inside the headless Shell (tests/desktop/arrange.sh): tile two windows side by side,
move one to another workspace, read the screens and the notification waiting."""

import sys

from arcee.platform.shell import ShellBridge

bridge = ShellBridge()
state = bridge.begin_control("arrange test")
windows = bridge.windows()
print("windows:", [(w["id"], w["title"], w["monitor"], w["workspace"]) for w in windows])
assert len(windows) >= 2, windows
first, second = windows[0], windows[1]
assert bridge.tile_window(first["id"], "left")
assert bridge.tile_window(second["id"], "right")
screens = bridge.monitors()
print("screens:", screens)
assert bridge.move_window(second["id"], -1, 1)
after = {w["id"]: w for w in bridge.windows()}
print("after:", [(w["title"], w["workspace"]) for w in after.values()])
assert after[second["id"]]["workspace"] == 1, after
notes = bridge.notifications()
print("notifications:", notes)
assert any(n["title"] == "Lunch?" and n["app"] for n in notes), notes
bridge.end_control()
print("ok")
sys.exit(0)
