"""Single-controller lifecycle, with neutralization on detach and errors."""
import logging
import time
from .mapping import Report, read_report, send_report

RUMBLE_MS = 300      # Each effect expires on its own, so rumble cannot stick on.
RUMBLE_REFRESH = 0.1  # Re-send an unchanged effect this often to keep it running.

# SDL stores the input backend in byte 14 of the joystick GUID.
BACKENDS = {"68": "HIDAPI", "72": "RawInput", "77": "Windows.Gaming.Input", "78": "XInput", "76": "Virtual"}


def backend_name(guid: str) -> str:
    if not isinstance(guid, str) or len(guid) != 32:
        return "Unknown"
    return BACKENDS.get(guid[28:30].lower(), "DirectInput")


class Bridge:
    def __init__(self, source, output_factory):
        self.source = source
        self.output_factory = output_factory
        self.controller = None
        self.pad = None
        self.enabled = False
        self.deadzone = 0.08
        self.trigger_threshold = 0.0
        self.rumble = (0, 0)  # (large, small) motors, 0-255, written by the driver thread.
        self.sent_rumble = (0, 0)
        self.next_rumble = 0.0
        self.test_rumble_until = 0.0
        self.backend = ""
        self.rumble_result = None  # Last value returned by SDL rumble: None until tried.
        self.next_scan = 0.0
        self.status = "Stopped"
        self.report = Report()
        self.devices = "No controllers detected"

    def start(self):
        if self.enabled:
            return
        self.enabled = True
        self.next_scan = 0.0
        self.status = "Waiting for a Stadia controller…"

    def _on_rumble(self, client, target, large_motor, small_motor, led_number, user_data):
        # Runs on a ViGEm driver thread: only record it; SDL is driven from tick().
        self.rumble = (large_motor, small_motor)

    def test_rumble(self, seconds=0.5):
        self.test_rumble_until = time.monotonic() + seconds

    def _apply_rumble(self):
        now = time.monotonic()
        large, small = (255, 255) if now < self.test_rumble_until else self.rumble
        if (large, small) == self.sent_rumble and (not (large or small) or now < self.next_rumble):
            return
        if large or small:
            # Large (left) motor is low frequency; small (right) motor is high frequency.
            result = bool(self.controller.rumble(large / 255, small / 255, RUMBLE_MS))
            if result != self.rumble_result:
                logging.info("SDL rumble %s via %s", "accepted" if result else "rejected", self.backend)
            self.rumble_result = result
        else:
            self.controller.stop_rumble()
        self.sent_rumble = (large, small)
        self.next_rumble = now + RUMBLE_REFRESH

    def release(self):
        pad, controller = self.pad, self.controller
        self.pad = self.controller = None
        self.report = Report()
        self.rumble = self.sent_rumble = (0, 0)
        self.test_rumble_until = 0.0
        try:
            if pad is not None:
                try:
                    pad.unregister_notification()
                finally:
                    send_report(pad, self.report)
        finally:
            # vgamepad removes the virtual device when its last reference is released.
            if controller is not None:
                try:
                    controller.stop_rumble()
                finally:
                    controller.quit()

    def stop(self):
        self.enabled = False
        try:
            self.release()
        finally:
            self.status = "Stopped"

    def tick(self):
        if not self.enabled:
            return
        try:
            self.source.pump()
            if self.controller is not None and not self.controller.attached():
                self.release()
                self.next_scan = 0.0
                self.status = "Disconnected — waiting for your Stadia controller…"
            if self.controller is None:
                now = time.monotonic()
                if now < self.next_scan:
                    return
                self.next_scan = now + 1.0
                names = self.source.names()
                self.devices = ", ".join(name for _, name in names) or "No controllers detected"
                for index, name in names:
                    # Never open the virtual Xbox output as an input.
                    if "stadia" in name.casefold():
                        self.controller = self.source.open(index)
                        self.pad = self.output_factory()
                        self.pad.register_notification(callback_function=self._on_rumble)
                        guid = self.source.guid(self.controller)
                        self.backend = backend_name(guid)
                        self.rumble_result = None
                        logging.info("Opened %s, GUID %s (%s)", name, guid, self.backend)
                        self.status = f"Connected: {name} → Xbox 360"
                        break
            if self.controller is not None:
                self.report = read_report(self.controller, self.deadzone, self.trigger_threshold)
                send_report(self.pad, self.report)
                self._apply_rumble()
        except Exception as exc:
            logging.exception("Controller bridge failed")
            try:
                self.stop()
            except Exception:
                logging.exception("Controller cleanup failed")
            self.status = f"Bridge stopped: {exc}. Check the connection and ViGEmBus, then press Start."


class SDLSource:
    def __init__(self):
        import os
        os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
        os.environ["SDL_VIDEODRIVER"] = "dummy"
        os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"
        import pygame
        from pygame._sdl2 import controller
        self.pygame, self.api = pygame, controller
        pygame.display.init()
        pygame.display.set_mode((1, 1))
        pygame.joystick.init()
        controller.init()

    def pump(self):
        self.pygame.event.get()  # Pump and drain even while the Tk window is minimized.

    def names(self):
        return [(i, self.api.name_forindex(i) or "Unknown controller")
                for i in range(self.api.get_count())]

    def open(self, index):
        if not self.api.is_controller(index):
            raise RuntimeError("SDL does not recognize this Stadia controller mapping")
        return self.api.Controller(index)

    def guid(self, controller):
        try:
            return controller.as_joystick().get_guid()
        except Exception:
            logging.exception("Could not read controller GUID")
            return ""

    def close(self):
        self.api.quit()
        self.pygame.quit()


def xbox_factory():
    # Import lazily: vgamepad connects to the driver at import time.
    try:
        import vgamepad
        pad = vgamepad.VX360Gamepad()
        # ViGEm's USB handshake includes a fixed off-center report, while its
        # report cache starts at zero. A zero-only first update is discarded.
        # Force a one-unit change (far below normal deadzones), then center it.
        time.sleep(0.25)  # Let Windows finish the initial USB handshake first.
        pad.left_joystick(x_value=1, y_value=0)
        pad.update()
        time.sleep(0.02)
        pad.reset()
        pad.update()
        return pad
    except Exception as exc:
        raise RuntimeError(f"Could not create Xbox controller; install ViGEmBus and restart the app ({exc})") from exc
