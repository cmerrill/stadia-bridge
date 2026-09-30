"""Direct HID vibration for when SDL cannot rumble the controller (Bluetooth on Windows).

SDL writes the Stadia rumble report {0x05, low, low, high, high} to the gamepad
collection only. This opens every HID collection Windows exposes for the
controller, logs what each one supports, and uses the first that accepts the
report through WriteFile or HidD_SetOutputReport. Writes run on a worker thread
so a slow Bluetooth link never stalls input.
"""
import logging
import threading
import time

VENDOR_ID, PRODUCT_ID = 0x18D1, 0x9400
RUMBLE_REPORT_ID = 0x05
WATCHDOG = 0.5  # Stop the motors if the bridge stops asking for rumble.


def choose_report_id(ids) -> int:
    if RUMBLE_REPORT_ID in ids or not ids:
        return RUMBLE_REPORT_ID
    return min(ids)


def rumble_report(report_id: int, length: int, large: int, small: int) -> bytes:
    # Scale 0-255 to 0-65535 little endian, as SDL sends it.
    low, high = large * 257, small * 257
    data = bytes([report_id, low & 0xFF, low >> 8, high & 0xFF, high >> 8])
    return data[:length].ljust(length, b"\0")


class _Windows:
    """Minimal ctypes bindings for HID enumeration and output reports."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class InterfaceData(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("InterfaceClassGuid", GUID), ("Flags", w.DWORD),
                        ("Reserved", ctypes.c_size_t)]

        class Attributes(ctypes.Structure):
            _fields_ = [("Size", w.ULONG), ("VendorID", w.USHORT), ("ProductID", w.USHORT),
                        ("VersionNumber", w.USHORT)]

        class Caps(ctypes.Structure):
            _fields_ = [("Usage", w.USHORT), ("UsagePage", w.USHORT), ("InputReportByteLength", w.USHORT),
                        ("OutputReportByteLength", w.USHORT), ("FeatureReportByteLength", w.USHORT),
                        ("Reserved", w.USHORT * 17)] + [(name, w.USHORT) for name in (
                            "NumberLinkCollectionNodes", "NumberInputButtonCaps", "NumberInputValueCaps",
                            "NumberInputDataIndices", "NumberOutputButtonCaps", "NumberOutputValueCaps",
                            "NumberOutputDataIndices", "NumberFeatureButtonCaps", "NumberFeatureValueCaps",
                            "NumberFeatureDataIndices")]

        self.GUID, self.InterfaceData, self.Attributes, self.Caps = GUID, InterfaceData, Attributes, Caps
        P = ctypes.POINTER
        self.setupapi = setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
        self.hid = hid = ctypes.WinDLL("hid", use_last_error=True)
        self.kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for fn, args, res in (
            (hid.HidD_GetHidGuid, [P(GUID)], None),
            (hid.HidD_GetAttributes, [w.HANDLE, P(Attributes)], w.BOOLEAN),
            (hid.HidD_GetPreparsedData, [w.HANDLE, P(ctypes.c_void_p)], w.BOOLEAN),
            (hid.HidD_FreePreparsedData, [ctypes.c_void_p], w.BOOLEAN),
            (hid.HidD_SetOutputReport, [w.HANDLE, ctypes.c_void_p, w.ULONG], w.BOOLEAN),
            (hid.HidP_GetCaps, [ctypes.c_void_p, P(Caps)], ctypes.c_long),
            (hid.HidP_GetValueCaps, [ctypes.c_int, ctypes.c_void_p, P(w.USHORT), ctypes.c_void_p], ctypes.c_long),
            (hid.HidP_GetButtonCaps, [ctypes.c_int, ctypes.c_void_p, P(w.USHORT), ctypes.c_void_p], ctypes.c_long),
            (setupapi.SetupDiGetClassDevsW, [P(GUID), w.LPCWSTR, w.HWND, w.DWORD], ctypes.c_void_p),
            (setupapi.SetupDiEnumDeviceInterfaces,
             [ctypes.c_void_p, ctypes.c_void_p, P(GUID), w.DWORD, P(InterfaceData)], w.BOOL),
            (setupapi.SetupDiGetDeviceInterfaceDetailW,
             [ctypes.c_void_p, P(InterfaceData), ctypes.c_void_p, w.DWORD, P(w.DWORD), ctypes.c_void_p], w.BOOL),
            (setupapi.SetupDiDestroyDeviceInfoList, [ctypes.c_void_p], w.BOOL),
            (kernel32.CreateFileW,
             [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
            (kernel32.WriteFile, [w.HANDLE, ctypes.c_void_p, w.DWORD, P(w.DWORD), ctypes.c_void_p], w.BOOL),
            (kernel32.CloseHandle, [w.HANDLE], w.BOOL),
        ):
            fn.argtypes, fn.restype = args, res
        self.invalid = ctypes.c_void_p(-1).value

    def error(self) -> str:
        code = self.ctypes.get_last_error()
        return f"error {code}: {self.ctypes.FormatError(code).strip()}"

    def paths(self):
        ctypes = self.ctypes
        guid = self.GUID()
        self.hid.HidD_GetHidGuid(ctypes.byref(guid))
        info = self.setupapi.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x2 | 0x10)  # PRESENT | INTERFACE
        if info is None or info == self.invalid:
            raise OSError(f"SetupDiGetClassDevs failed ({self.error()})")
        paths = []
        try:
            index = 0
            while True:
                data = self.InterfaceData(cbSize=ctypes.sizeof(self.InterfaceData))
                if not self.setupapi.SetupDiEnumDeviceInterfaces(info, None, ctypes.byref(guid), index,
                                                                 ctypes.byref(data)):
                    break
                index += 1
                size = self.w.DWORD()
                self.setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), None, 0,
                                                               ctypes.byref(size), None)
                detail = ctypes.create_string_buffer(size.value)
                # cbSize is the fixed part of SP_DEVICE_INTERFACE_DETAIL_DATA_W.
                ctypes.cast(detail, ctypes.POINTER(self.w.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
                if self.setupapi.SetupDiGetDeviceInterfaceDetailW(info, ctypes.byref(data), detail, size.value,
                                                                  None, None):
                    paths.append(ctypes.wstring_at(ctypes.addressof(detail) + 4))
        finally:
            self.setupapi.SetupDiDestroyDeviceInfoList(info)
        return paths

    def open(self, path, access):
        handle = self.kernel32.CreateFileW(path, access, 0x1 | 0x2, None, 3, 0, None)  # share R/W, OPEN_EXISTING
        return None if handle is None or handle == self.invalid else handle

    def close(self, handle):
        self.kernel32.CloseHandle(handle)

    def ids(self, handle):
        ctypes = self.ctypes
        attributes = self.Attributes(Size=ctypes.sizeof(self.Attributes))
        if not self.hid.HidD_GetAttributes(handle, ctypes.byref(attributes)):
            return None
        return attributes.VendorID, attributes.ProductID

    def caps(self, handle):
        """Return (caps, output report IDs)."""
        ctypes = self.ctypes
        preparsed = ctypes.c_void_p()
        if not self.hid.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            raise OSError(f"HidD_GetPreparsedData failed ({self.error()})")
        try:
            caps = self.Caps()
            if self.hid.HidP_GetCaps(preparsed, ctypes.byref(caps)) & 0xFFFFFFFF != 0x00110000:
                raise OSError("HidP_GetCaps failed")
            ids = set()
            # HIDP_VALUE_CAPS and HIDP_BUTTON_CAPS are both 72 bytes with ReportID at offset 2.
            for get, count in ((self.hid.HidP_GetValueCaps, caps.NumberOutputValueCaps),
                               (self.hid.HidP_GetButtonCaps, caps.NumberOutputButtonCaps)):
                if count:
                    buffer = (ctypes.c_ubyte * (72 * count))()
                    n = self.w.USHORT(count)
                    if get(1, buffer, ctypes.byref(n), preparsed) & 0xFFFFFFFF == 0x00110000:  # HidP_Output
                        ids.update(buffer[72 * i + 2] for i in range(n.value))
            return caps, ids
        finally:
            self.hid.HidD_FreePreparsedData(preparsed)

    def write_file(self, handle, data):
        written = self.w.DWORD()
        return bool(self.kernel32.WriteFile(handle, data, len(data), self.ctypes.byref(written), None))

    def set_output_report(self, handle, data):
        buffer = self.ctypes.create_string_buffer(data, len(data))
        return bool(self.hid.HidD_SetOutputReport(handle, buffer, len(data)))


class HidRumble:
    """Finds a writable rumble report on the controller and drives it from a worker thread."""

    def __init__(self):
        self.status = "searching for a writable HID report…"
        self._target, self._stamp = (0, 0), time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hid-rumble", daemon=True)
        self._thread.start()

    def set(self, large: int, small: int):
        with self._lock:
            self._target, self._stamp = (large, small), time.monotonic()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _find(self, api):
        """Log every collection of the controller and return the first that accepts rumble."""
        found = False
        for path in api.paths():
            probe = api.open(path, 0)  # Query access works even for collections Windows holds exclusively.
            if probe is None:
                continue
            try:
                ids = api.ids(probe)
            finally:
                api.close(probe)
            if ids != (VENDOR_ID, PRODUCT_ID):
                continue
            found = True
            handle = api.open(path, 0x80000000 | 0x40000000) or api.open(path, 0x40000000)  # R/W, then W only
            if handle is None:
                logging.info("HID %s: cannot open for writing (%s)", path, api.error())
                continue
            keep = False
            try:
                caps, report_ids = api.caps(handle)
                logging.info("HID %s: usage %04x:%04x, input %d, output %d, feature %d bytes, output reports %s",
                             path, caps.UsagePage, caps.Usage, caps.InputReportByteLength,
                             caps.OutputReportByteLength, caps.FeatureReportByteLength,
                             sorted(report_ids) or "none")
                length = caps.OutputReportByteLength
                if not length:
                    continue
                report_id = choose_report_id(report_ids)
                for name, write in (("WriteFile", api.write_file), ("HidD_SetOutputReport", api.set_output_report)):
                    if write(handle, rumble_report(report_id, length, 0, 0)):
                        logging.info("HID rumble: %s accepted report 0x%02x (%d bytes)", name, report_id, length)
                        keep = True
                        return handle, write, report_id, length, name
                    logging.info("HID rumble: %s rejected report 0x%02x (%s)", name, report_id, api.error())
            finally:
                if not keep:
                    api.close(handle)
        raise RuntimeError("no HID collection accepted the rumble report" if found
                           else "controller not found among HID devices")

    def _run(self):
        try:
            api = _Windows()
            handle, write, report_id, length, name = self._find(api)
        except Exception as exc:
            logging.exception("Direct HID rumble unavailable")
            self.status = f"unavailable: {exc}"
            return
        self.status = f"direct HID via {name}"
        sent, failed = None, False
        try:
            while not self._stop.is_set():
                with self._lock:
                    target, stamp = self._target, self._stamp
                if time.monotonic() - stamp > WATCHDOG:
                    target = (0, 0)
                if target != sent:
                    if not write(handle, rumble_report(report_id, length, *target)) and not failed:
                        logging.warning("HID rumble write failed (%s)", api.error())
                        failed = True
                    sent = target
                self._stop.wait(0.02)
        finally:
            write(handle, rumble_report(report_id, length, 0, 0))
            api.close(handle)
