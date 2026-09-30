# Vibration over Bluetooth on Windows

**Summary:** vibration works over USB. Over Bluetooth, Windows rejects the
controller's vibration report before it is sent, and this app cannot work
around that.

Tested 2026-09-30 on Windows with a Stadia controller on the Bluetooth
firmware, using pygame 2.6.1 (SDL 2.28.4).

## What we found

| Connection | SDL input path | SDL rumble | Vibrates |
|---|---|---|---|
| USB | HIDAPI (Stadia driver) | accepted | yes |
| Bluetooth LE | HIDAPI (Stadia driver) | rejected | no |

- Both connections use SDL's HIDAPI Stadia driver (GUID byte 14 is `h`), so
  the difference is the Windows Bluetooth LE HID stack, not SDL's backend.
- SDL's driver sends the rumble report `{0x05, low, low, high, high}`. When it
  opens the device it writes a zero rumble report; if that write fails, it
  marks rumble as unsupported and never retries. Over Bluetooth that write
  fails.
- A direct HID test (since removed) opened the controller's HID interface
  without SDL. Windows exposes one collection over Bluetooth:
  `\\?\hid#{00001812-...}_dev_vid&0218d1_pid&9400...`, usage `0001:0005`
  (gamepad), input 11 bytes, output 5 bytes, output report IDs `[5]`. So the
  descriptor does declare the rumble report with the expected size.
- Writing report `0x05` (5 bytes) to it failed with
  `ERROR_INVALID_PARAMETER` (87) through both `WriteFile` and
  `HidD_SetOutputReport`, within about 1 ms. The rejection is local to
  Windows' Bluetooth LE HID driver; nothing reaches the controller.

## Why Stadia-X works

[Stadia-X](https://github.com/offvault/Stadia-X) sends the same `0x05`
report, but it passes the Bluetooth adapter into WSL2 and writes to Linux's
`/dev/hidraw*`. The Linux Bluetooth stack (BlueZ) accepts the write where
Windows does not. Its README notes that Windows "natively struggles" with
the controller's Bluetooth implementation.

## Options not pursued

- **Writing the GATT Report characteristic directly** (WinRT Bluetooth
  APIs): Windows blocks user-mode access to the HID-over-GATT service
  (`0x1812`).
- **Newer SDL:** the Stadia driver has no Bluetooth-specific changes up to
  SDL 2.32 and SDL 3.
- **Inspecting the controller's GATT table** (e.g. with nRF Connect) could
  show how the output report characteristic is declared (properties, Report
  Reference) and confirm why Windows rejects it, but is unlikely to give a
  fix inside this app.

## Workarounds

- Use a USB cable when you want vibration.
- Use Stadia-X's WSL2 approach for Bluetooth vibration.
