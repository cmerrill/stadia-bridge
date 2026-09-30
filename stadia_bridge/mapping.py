"""Translate SDL's normalized controller inputs into an XInput report."""
from dataclasses import dataclass
from math import hypot

# SDL GameController button order; values are XUSB_GAMEPAD bit flags.
BUTTONS = (
    ("A", 0x1000), ("B", 0x2000), ("X", 0x4000), ("Y", 0x8000),
    ("Back", 0x0020), ("Guide", 0x0400), ("Start", 0x0010),
    ("LS", 0x0040), ("RS", 0x0080), ("LB", 0x0100), ("RB", 0x0200),
    ("Up", 0x0001), ("Down", 0x0002), ("Left", 0x0004), ("Right", 0x0008),
)


def stick(x: int, y: int, deadzone: float) -> tuple[int, int]:
    if not 0 <= deadzone < 1:
        raise ValueError("Deadzone must be between 0 and 1 (exclusive).")
    def unit(value):
        return max(-1.0, min(1.0, value / (32768 if value < 0 else 32767)))
    nx, ny = unit(x), -unit(y)  # SDL positive Y is down; XInput positive Y is up.
    magnitude = hypot(nx, ny)
    if magnitude <= deadzone:
        return 0, 0
    scale = (min(1.0, magnitude) - deadzone) / (1 - deadzone) / magnitude
    def integer(value):
        return round(value * (32768 if value < 0 else 32767))
    return integer(nx * scale), integer(ny * scale)


@dataclass(frozen=True)
class Report:
    buttons: int = 0
    lx: int = 0
    ly: int = 0
    rx: int = 0
    ry: int = 0
    lt: int = 0
    rt: int = 0


def read_report(controller, deadzone: float = 0.08, trigger_threshold: float = 0.0) -> Report:
    if not 0 <= trigger_threshold < 1:
        raise ValueError("Trigger threshold must be between 0 and 1 (exclusive).")
    axes = [controller.get_axis(i) for i in range(6)]
    lx, ly = stick(axes[0], axes[1], deadzone)
    rx, ry = stick(axes[2], axes[3], deadzone)
    mask = sum(flag for i, (_, flag) in enumerate(BUTTONS) if controller.get_button(i))
    def trigger(value):
        # Below the threshold reads as released; the rest is rescaled so a full pull is still 255.
        unit = max(0, min(32767, value)) / 32767
        if unit <= trigger_threshold:
            return 0
        return round((unit - trigger_threshold) / (1 - trigger_threshold) * 255)
    return Report(mask, lx, ly, rx, ry, trigger(axes[4]), trigger(axes[5]))


def send_report(pad, report: Report):
    pad.reset()
    for _, flag in BUTTONS:
        if report.buttons & flag:
            pad.press_button(button=flag)
    pad.left_joystick(x_value=report.lx, y_value=report.ly)
    pad.right_joystick(x_value=report.rx, y_value=report.ry)
    pad.left_trigger(value=report.lt)
    pad.right_trigger(value=report.rt)
    pad.update()
