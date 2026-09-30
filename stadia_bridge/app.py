"""Windows desktop interface."""
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk, messagebox
import webbrowser
from .bridge import Bridge, SDLSource, xbox_factory
from .mapping import BUTTONS
from .instance import SingleInstance

DRIVER_URL = "https://github.com/nefarius/ViGEmBus/releases/latest"


def main():
    root = tk.Tk()
    root.title("Stadia Bridge")
    root.geometry("650x610")
    root.minsize(590, 570)
    if sys.platform != "win32":
        messagebox.showerror("Windows required", "Stadia Bridge creates a virtual Xbox controller on Windows 10/11.")
        root.destroy()
        return
    instance = SingleInstance()
    if instance.already_running:
        root.withdraw()
        messagebox.showinfo("Already running", "Stadia Bridge is already open. Use its existing window to avoid duplicate controllers.")
        instance.close()
        root.destroy()
        return
    log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "StadiaBridge"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=log_dir / "bridge.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        source = SDLSource()
    except Exception as exc:
        logging.exception("SDL initialization failed")
        messagebox.showerror("Could not start", f"{exc}\n\nPlease download a fresh copy of Stadia Bridge.")
        instance.close()
        root.destroy()
        return
    bridge = Bridge(source, xbox_factory)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#151b29")
    style.configure("TLabel", background="#151b29", foreground="#e5eaf4", font=("Segoe UI", 10))
    style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
    style.configure("TButton", font=("Segoe UI", 10), padding=8)
    panel = ttk.Frame(root, padding=26)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text="Stadia Bridge", style="Title.TLabel").pack(anchor="w")
    ttk.Label(panel, text="Your Stadia controller. Xbox-compatible input.").pack(anchor="w", pady=(2, 22))
    status = tk.StringVar(value="Starting…")
    ttk.Label(panel, textvariable=status, wraplength=575).pack(anchor="w", pady=(0, 8))
    devices = tk.StringVar()
    ttk.Label(panel, textvariable=devices, wraplength=575).pack(anchor="w")
    actions = ttk.Frame(panel)
    actions.pack(fill="x", pady=18)
    ttk.Button(actions, text="Start", command=bridge.start).pack(side="left", padx=(0, 8))
    def stop():
        try:
            bridge.stop()
        except Exception as exc:
            logging.exception("Stop failed")
            messagebox.showerror("Controller cleanup", str(exc))
    ttk.Button(actions, text="Stop", command=stop).pack(side="left")
    ttk.Button(actions, text="Test vibration", command=bridge.test_rumble).pack(side="left", padx=(8, 0))
    ttk.Button(actions, text="Test in Windows", command=lambda: subprocess.Popen(["control.exe", "joy.cpl"])).pack(side="right")
    zone_label = tk.StringVar(value="Stick deadzone: 8%")
    ttk.Label(panel, textvariable=zone_label).pack(anchor="w")
    def deadzone(value):
        bridge.deadzone = float(value) / 100
        zone_label.set(f"Stick deadzone: {float(value):.0f}%")
    scale = ttk.Scale(panel, from_=0, to=30, command=deadzone)
    scale.set(8)
    scale.pack(fill="x", pady=(4, 16))
    threshold_label = tk.StringVar(value="Trigger threshold: 0% (off)")
    ttk.Label(panel, textvariable=threshold_label).pack(anchor="w")
    def trigger_threshold(value):
        bridge.trigger_threshold = float(value) / 100
        threshold_label.set(f"Trigger threshold: {float(value):.0f}%" + (" (off)" if float(value) < 0.5 else ""))
    threshold = ttk.Scale(panel, from_=0, to=60, command=trigger_threshold)
    threshold.set(0)
    threshold.pack(fill="x", pady=(4, 16))
    live = tk.StringVar(value="No input")
    ttk.Label(panel, text="LIVE XBOX OUTPUT").pack(anchor="w")
    ttk.Label(panel, textvariable=live, font=("Consolas", 10), wraplength=575).pack(anchor="w", pady=8)
    ttk.Label(panel, text="Keep this app open while playing. It works when minimized.\nUSB, or Bluetooth if your controller already has Bluetooth firmware.", wraplength=575).pack(anchor="w", pady=12)
    links = ttk.Frame(panel)
    links.pack(fill="x", side="bottom")
    ttk.Button(links, text="Get ViGEmBus driver", command=lambda: webbrowser.open(DRIVER_URL)).pack(side="left")
    def licenses():
        bundle = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        folder = bundle / 'third_party'
        if folder.is_dir():
            os.startfile(folder)
        else:
            webbrowser.open('https://github.com/adamdavies1915/stadia-bridge/tree/main/third_party')
    ttk.Button(links, text="Licenses", command=licenses).pack(side="left", padx=8)
    ttk.Button(links, text="Open logs", command=lambda: os.startfile(log_dir)).pack(side="right")
    last_ui = 0.0
    def tick():
        nonlocal last_ui
        bridge.tick()
        now = time.monotonic()
        if now - last_ui >= 0.05:
            status.set(bridge.status)
            rumble = {None: "not tried", True: "accepted by SDL", False: "rejected by SDL"}[bridge.rumble_result]
            path = f"\nInput path: {bridge.backend}    Vibration: {rumble}" if bridge.controller is not None else ""
            devices.set(f"Detected: {bridge.devices}{path}")
            r = bridge.report
            pressed = " ".join(name for name, flag in BUTTONS if r.buttons & flag) or "—"
            live.set(f"Left  {r.lx:6d}, {r.ly:6d}    LT {r.lt:3d}/255\nRight {r.rx:6d}, {r.ry:6d}    RT {r.rt:3d}/255\nButtons: {pressed}")
            last_ui = now
        root.after(8, tick)
    def close():
        try:
            stop()
        finally:
            source.close()
            instance.close()
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    bridge.start()
    tick()
    root.mainloop()
