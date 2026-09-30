import unittest
from unittest.mock import Mock, patch, call
from stadia_bridge.bridge import Bridge, backend_name, xbox_factory
from stadia_bridge.hidrumble import choose_report_id, rumble_report
from stadia_bridge.mapping import BUTTONS, Report, read_report, send_report, stick


def controller(axes=(0, 0, 0, 0, 0, 0), pressed=()):
    c = Mock()
    c.get_axis.side_effect = lambda i: axes[i]
    c.get_button.side_effect = lambda i: i in pressed
    c.attached.return_value = True
    return c


class MappingTests(unittest.TestCase):
    def test_center_and_deadzone(self):
        self.assertEqual(stick(120, -200, .08), (0, 0))
        self.assertEqual(stick(0, 0, 0), (0, 0))

    def test_full_stick_travel_and_y_direction(self):
        self.assertEqual(stick(-32768, 0, .08), (-32768, 0))
        self.assertEqual(stick(32767, 0, .08), (32767, 0))
        self.assertEqual(stick(0, -32768, .08), (0, 32767))
        self.assertEqual(stick(0, 32767, .08), (0, -32768))
        x, y = stick(32767, -32768, .08)
        self.assertAlmostEqual(x / 32767, 2 ** -.5, places=4)
        self.assertEqual(x, y)

    def test_triggers_are_independent_and_clamped(self):
        r = read_report(controller((0, 0, 0, 0, 16384, 32768)))
        self.assertEqual((r.lt, r.rt), (128, 255))
        self.assertEqual(read_report(controller((0, 0, 0, 0, -1, 0))).lt, 0)

    def test_trigger_threshold(self):
        light = controller((0, 0, 0, 0, 6553, 32767))
        self.assertEqual((read_report(light, trigger_threshold=.25).lt, read_report(light, trigger_threshold=.25).rt), (0, 255))
        self.assertEqual(read_report(controller((0, 0, 0, 0, 16384, 0)), trigger_threshold=.25).lt, 85)
        self.assertEqual(read_report(light).lt, 51)  # Default threshold is off.
        with self.assertRaises(ValueError):
            read_report(controller(), trigger_threshold=1)

    def test_each_button_and_simultaneous_inputs(self):
        expected = [4096, 8192, 16384, 32768, 32, 1024, 16, 64, 128, 256, 512, 1, 2, 4, 8]
        for i, flag in enumerate(expected):
            self.assertEqual(read_report(controller(pressed=(i,))).buttons, flag)
        self.assertEqual(read_report(controller(pressed=(0, 9, 11))).buttons, 0x1101)

    def test_new_xbox_is_primed_then_centered(self):
        pad = Mock()
        module = Mock()
        module.VX360Gamepad.return_value = pad
        with patch.dict('sys.modules', {'vgamepad': module}):
            self.assertIs(xbox_factory(), pad)
        self.assertEqual(pad.mock_calls, [
            call.left_joystick(x_value=1, y_value=0), call.update(),
            call.reset(), call.update(),
        ])

    def test_each_report_clears_previous_buttons(self):
        pad = Mock()
        send_report(pad, Report(buttons=0x1000, lt=255))
        send_report(pad, Report())
        self.assertEqual(pad.reset.call_count, 2)
        self.assertEqual(pad.press_button.call_count, 1)
        pad.left_trigger.assert_called_with(value=0)
        self.assertEqual(pad.update.call_count, 2)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.source = Mock()
        self.source.names.return_value = [(0, 'Xbox 360 Controller'), (1, 'Stadia Controller')]
        self.c = controller(pressed=(0,))
        self.source.open.return_value = self.c
        self.pad = Mock()
        self.factory = Mock(return_value=self.pad)
        self.bridge = Bridge(self.source, self.factory)

    def test_only_stadia_is_opened(self):
        self.bridge.start()
        self.bridge.tick()
        self.source.open.assert_called_once_with(1)
        self.assertEqual(self.bridge.report.buttons, 0x1000)

    def test_no_virtual_output_without_stadia(self):
        self.source.names.return_value = [(0, 'Xbox 360 Controller')]
        self.bridge.start()
        self.bridge.tick()
        self.factory.assert_not_called()

    def test_disconnect_releases_inputs_and_reconnects(self):
        self.bridge.start()
        self.bridge.tick()
        self.c.attached.return_value = False
        self.source.names.return_value = []
        self.bridge.tick()
        self.c.quit.assert_called_once()
        self.assertIsNone(self.bridge.pad)
        self.pad.left_trigger.assert_called_with(value=0)
        self.assertEqual(self.bridge.report, Report())
        self.source.names.return_value = [(2, 'Stadia Controller')]
        self.c.attached.return_value = True
        self.bridge.next_scan = 0
        self.bridge.tick()
        self.source.open.assert_called_with(2)
        self.assertIsNotNone(self.bridge.pad)

    def test_stop_removes_output_and_stays_stopped(self):
        self.bridge.start()
        self.bridge.tick()
        self.bridge.stop()
        self.bridge.tick()
        self.assertFalse(self.bridge.enabled)
        self.assertIsNone(self.bridge.pad)
        self.c.quit.assert_called_once()
        self.assertEqual(self.bridge.report, Report())

    def test_driver_failure_closes_input_and_can_retry(self):
        self.factory.side_effect = RuntimeError('Missing driver')
        self.bridge.start()
        with self.assertLogs(level='ERROR'):
            self.bridge.tick()
        self.assertFalse(self.bridge.enabled)
        self.c.quit.assert_called_once()
        self.assertIn('Missing driver', self.bridge.status)
        self.factory.side_effect = None
        self.bridge.start()
        self.bridge.tick()
        self.assertIsNotNone(self.bridge.pad)

    def test_read_failure_neutralizes_output(self):
        self.bridge.start()
        self.bridge.tick()
        self.c.get_axis.side_effect = RuntimeError('Device lost')
        with self.assertLogs(level='ERROR'):
            self.bridge.tick()
        self.assertEqual(self.bridge.report, Report())
        self.assertIsNone(self.bridge.pad)
        self.pad.left_joystick.assert_called_with(x_value=0, y_value=0)
        self.assertFalse(self.bridge.enabled)


class RumbleTests(unittest.TestCase):
    def setUp(self):
        self.source = Mock()
        self.source.names.return_value = [(1, 'Stadia Controller')]
        self.source.guid.return_value = '05000000d11800000094000000006800'
        self.c = controller()
        self.source.open.return_value = self.c
        self.pad = Mock()
        self.hid = Mock()
        self.hid_factory = Mock(return_value=self.hid)
        self.bridge = Bridge(self.source, Mock(return_value=self.pad), self.hid_factory)
        self.bridge.start()
        self.bridge.tick()

    def game_rumbles(self, large, small):
        callback = self.pad.register_notification.call_args.kwargs['callback_function']
        callback(None, None, large, small, 0, None)

    def test_game_rumble_is_forwarded_and_stopped(self):
        self.c.rumble.assert_not_called()
        self.c.stop_rumble.assert_not_called()
        self.game_rumbles(255, 51)
        self.bridge.tick()
        self.c.rumble.assert_called_once_with(1.0, 0.2, 300)
        self.bridge.tick()  # Unchanged and not yet due for a refresh.
        self.c.rumble.assert_called_once()
        self.bridge.next_rumble = 0
        self.bridge.tick()
        self.assertEqual(self.c.rumble.call_count, 2)
        self.game_rumbles(0, 0)
        self.bridge.tick()
        self.bridge.tick()
        self.c.stop_rumble.assert_called_once()

    def test_backend_and_rumble_result_are_reported(self):
        self.assertEqual(self.bridge.backend, 'HIDAPI')
        self.assertIsNone(self.bridge.rumble_result)
        self.c.rumble.return_value = False
        self.bridge.test_rumble()
        self.bridge.tick()
        self.assertIs(self.bridge.rumble_result, False)

    def test_rejected_sdl_rumble_falls_back_to_hid(self):
        self.c.rumble.return_value = False
        self.bridge.test_rumble()
        self.bridge.tick()
        self.hid_factory.assert_called_once()
        self.hid.set.assert_called_with(255, 255)
        self.bridge.test_rumble_until = 0
        self.bridge.tick()
        self.hid.set.assert_called_with(0, 0)  # Sent every tick; SDL is not retried.
        self.c.rumble.assert_called_once()
        self.bridge.stop()
        self.hid.close.assert_called_once()
        self.assertIsNone(self.bridge.hid_rumble)

    def test_accepted_sdl_rumble_does_not_use_hid(self):
        self.c.rumble.return_value = True
        self.bridge.test_rumble()
        self.bridge.tick()
        self.hid_factory.assert_not_called()

    def test_hid_rumble_report(self):
        self.assertEqual(rumble_report(5, 5, 255, 1), bytes([5, 255, 255, 1, 1]))
        self.assertEqual(rumble_report(5, 8, 0, 0), bytes([5, 0, 0, 0, 0, 0, 0, 0]))
        self.assertEqual(choose_report_id({3, 5}), 5)
        self.assertEqual(choose_report_id({7}), 7)
        self.assertEqual(choose_report_id(set()), 5)

    def test_backend_names(self):
        self.assertEqual(backend_name('03000000d11800000094000000007700'), 'Windows.Gaming.Input')
        self.assertEqual(backend_name('03000000d11800000094000000000000'), 'DirectInput')
        self.assertEqual(backend_name(''), 'Unknown')

    def test_test_button_pulses_then_stops(self):
        self.bridge.test_rumble()
        self.bridge.tick()
        self.c.rumble.assert_called_once_with(1.0, 1.0, 300)
        self.bridge.test_rumble_until = 0
        self.bridge.tick()
        self.c.stop_rumble.assert_called_once()

    def test_release_stops_rumble_and_unregisters(self):
        self.game_rumbles(200, 200)
        self.bridge.tick()
        self.bridge.stop()
        self.pad.unregister_notification.assert_called_once()
        self.c.stop_rumble.assert_called_once()
        self.c.quit.assert_called_once()
        self.assertEqual(self.bridge.rumble, (0, 0))


if __name__ == '__main__':
    unittest.main()
