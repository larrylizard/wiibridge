#!/usr/bin/env python3
"""
Live status + remapping GUI for wiimote_bridge.py.

Connects to the daemon's local Unix socket (no root needed for the GUI
itself). Shows up to 4 connected remotes side by side, each in its own
compact column with live button state and independent remapping -- every
remote can have its own button -> gamepad/keyboard mapping and its own IR
pointer settings, since the daemon tracks mapping per Bluetooth address.
Changes take effect live -- the daemon rebuilds its virtual input device
for that remote immediately.
"""

import json
import os
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

from wiimote_bridge import INPUT_NAMES, SOCK_PATH

MAX_SLOTS = 4


def ensure_daemon_running():
    """The daemon needs root (raw Bluetooth sockets + /dev/uinput). Rather
    than requiring a separate systemd install step, launch it on demand
    via pkexec -- this also makes the GUI work unmodified when run from
    inside a read-only-mounted AppImage."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(SOCK_PATH)
        s.close()
        return  # already running
    except OSError:
        pass

    bridge_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiimote_bridge.py")
    try:
        log_file = open("/tmp/wiimote_bridge.log", "a")
        subprocess.Popen(
            ["pkexec", "python3", bridge_path],
            stdout=log_file, stderr=log_file,
            start_new_session=True,
        )
    except OSError as ex:
        print(f"Could not launch wiimote_bridge daemon via pkexec: {ex}")

SHORT_NAMES = {
    "A": "A", "B": "B", "ONE": "1", "TWO": "2", "MINUS": "-", "PLUS": "+",
    "HOME": "Home", "DPAD_UP": "↑", "DPAD_DOWN": "↓",
    "DPAD_LEFT": "←", "DPAD_RIGHT": "→",
}

GAMEPAD_BUTTONS = [
    "BTN_A", "BTN_B", "BTN_X", "BTN_Y", "BTN_TL", "BTN_TR", "BTN_TL2", "BTN_TR2",
    "BTN_SELECT", "BTN_START", "BTN_MODE", "BTN_THUMBL", "BTN_THUMBR",
    "BTN_DPAD_UP", "BTN_DPAD_DOWN", "BTN_DPAD_LEFT", "BTN_DPAD_RIGHT",
]

KEYSYM_TO_EVDEV = {
    "space": "KEY_SPACE", "Return": "KEY_ENTER", "Escape": "KEY_ESC",
    "Tab": "KEY_TAB", "BackSpace": "KEY_BACKSPACE",
    "Up": "KEY_UP", "Down": "KEY_DOWN", "Left": "KEY_LEFT", "Right": "KEY_RIGHT",
    "Shift_L": "KEY_LEFTSHIFT", "Shift_R": "KEY_RIGHTSHIFT",
    "Control_L": "KEY_LEFTCTRL", "Control_R": "KEY_RIGHTCTRL",
    "Alt_L": "KEY_LEFTALT", "Alt_R": "KEY_RIGHTALT",
    "comma": "KEY_COMMA", "period": "KEY_DOT", "minus": "KEY_MINUS", "equal": "KEY_EQUAL",
}


def keysym_to_code(keysym):
    if keysym in KEYSYM_TO_EVDEV:
        return KEYSYM_TO_EVDEV[keysym]
    if keysym.startswith("F") and keysym[1:].isdigit():
        return f"KEY_{keysym.upper()}"
    if len(keysym) == 1:
        ch = keysym.upper()
        if ch.isalpha() or ch.isdigit():
            return f"KEY_{ch}"
    return None


def fmt_mapping(spec):
    if not spec or spec.get("kind") == "none":
        return "—"
    code = spec.get("code", "")
    return code.replace("BTN_", "").replace("KEY_", "").title()


class RemapDialog(tk.Toplevel):
    def __init__(self, parent, label, on_set):
        super().__init__(parent)
        self.title(f"Remap {label}")
        self.resizable(False, False)
        self.on_set = on_set

        tk.Label(self, text=f"Button: {label}", font=("", 11, "bold")).pack(padx=16, pady=(14, 4))
        tk.Label(self, text="Press any keyboard key to bind it\n(Esc cancels)", justify="center").pack(padx=16, pady=4)

        tk.Frame(self, height=1, bg="#888").pack(fill="x", padx=16, pady=10)

        tk.Label(self, text="...or choose a gamepad button:").pack(padx=16)
        self.combo = ttk.Combobox(self, values=GAMEPAD_BUTTONS, state="readonly")
        self.combo.pack(padx=16, pady=(4, 10))
        self.combo.bind("<<ComboboxSelected>>", self._on_combo)

        btnrow = tk.Frame(self)
        btnrow.pack(pady=(0, 14))
        tk.Button(btnrow, text="Unmap", command=self._on_unmap).pack(side="left", padx=6)
        tk.Button(btnrow, text="Cancel", command=self.destroy).pack(side="left", padx=6)

        self.bind("<Key>", self._on_key)
        self.grab_set()
        self.focus_set()

    def _on_key(self, event):
        if event.keysym == "Escape":
            self.destroy()
            return
        code = keysym_to_code(event.keysym)
        if code:
            self.on_set("key", code)
            self.destroy()

    def _on_combo(self, _event):
        self.on_set("button", self.combo.get())
        self.destroy()

    def _on_unmap(self):
        self.on_set("none", "")
        self.destroy()


class DeviceColumn:
    """One compact panel bound to a Bluetooth address once a remote occupies
    this slot; shows a placeholder while the slot is empty."""

    def __init__(self, parent, app, col_index):
        self.app = app
        self.addr = None
        self.mapping = {}

        self.frame = tk.LabelFrame(parent, text=f"Slot {col_index + 1}: empty", padx=6, pady=4)
        self.frame.grid(row=0, column=col_index, padx=5, pady=5, sticky="n")

        ptr_row = tk.Frame(self.frame)
        ptr_row.pack(fill="x", pady=(0, 4))
        self.ptr_var = tk.BooleanVar(value=True)
        self.ix_var = tk.BooleanVar(value=True)
        self.iy_var = tk.BooleanVar(value=False)
        self._suppress = True
        tk.Checkbutton(ptr_row, text="Ptr", variable=self.ptr_var, command=self._send_pointer).pack(side="left")
        tk.Checkbutton(ptr_row, text="iX", variable=self.ix_var, command=self._send_pointer).pack(side="left")
        tk.Checkbutton(ptr_row, text="iY", variable=self.iy_var, command=self._send_pointer).pack(side="left")
        self._suppress = False

        self.row_buttons = {}
        for name in INPUT_NAMES:
            btn = tk.Button(
                self.frame, text=f"{SHORT_NAMES[name]}: —", width=14, anchor="w",
                bg="#444444", fg="white", relief="flat",
                command=lambda n=name: self._open_remap(n),
            )
            btn.pack(fill="x", pady=1)
            self.row_buttons[name] = btn

    def bind_addr(self, addr):
        if self.addr == addr:
            return
        self.addr = addr
        short = addr[-8:] if addr else "empty"
        self.frame.config(text=f"Remote: {short}" if addr else "Slot: empty")
        state = "normal" if addr else "disabled"
        for w in self.row_buttons.values():
            w.config(state=state, bg="#444444")

    def set_mapping(self, mapping):
        self.mapping = mapping
        for name, btn in self.row_buttons.items():
            spec = mapping.get(name, {})
            btn.config(text=f"{SHORT_NAMES[name]}: {fmt_mapping(spec)}")

    def set_pointer_config(self, cfg):
        self._suppress = True
        self.ptr_var.set(cfg.get("enabled", True))
        self.ix_var.set(cfg.get("invert_x", True))
        self.iy_var.set(cfg.get("invert_y", False))
        self._suppress = False

    def set_button_state(self, name, pressed):
        btn = self.row_buttons.get(name)
        if btn:
            btn.config(bg="#4CAF50" if pressed else "#444444")

    def _send_pointer(self):
        if self._suppress or not self.addr:
            return
        self.app.send({
            "type": "set_pointer", "addr": self.addr,
            "enabled": self.ptr_var.get(), "invert_x": self.ix_var.get(), "invert_y": self.iy_var.get(),
        })

    def _open_remap(self, name):
        if not self.addr:
            return
        RemapDialog(self.frame, f"{name} ({self.addr[-5:]})", lambda kind, code: self.app.send(
            {"type": "set_mapping", "addr": self.addr, "input": name, "kind": kind, "code": code}
        ))


class GuiApp:
    def __init__(self, root):
        self.root = root
        self.sock = None
        self.msg_queue = queue.Queue()
        self.slots = []          # list[DeviceColumn], fixed size MAX_SLOTS
        self.slot_of_addr = {}   # addr -> slot index

        self._build_ui()
        threading.Thread(target=self._socket_worker, daemon=True).start()
        self.root.after(50, self._poll_queue)

    def _build_ui(self):
        self.root.title("Wii Remote Control")

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True)

        controllers_tab = tk.Frame(notebook)
        notebook.add(controllers_tab, text="Controllers")

        self.status_lbl = tk.Label(controllers_tab, text="Connecting to daemon...", anchor="w")
        self.status_lbl.pack(fill="x", padx=8, pady=(8, 2))

        cols = tk.Frame(controllers_tab)
        cols.pack(padx=5, pady=(0, 8))
        for i in range(MAX_SLOTS):
            self.slots.append(DeviceColumn(cols, self, i))

        settings_tab = tk.Frame(notebook)
        notebook.add(settings_tab, text="Settings")
        self._build_settings_tab(settings_tab)
        notebook.bind("<<NotebookTabChanged>>", lambda e: self._maybe_refresh_adapters(notebook, settings_tab))

    def _build_settings_tab(self, parent):
        tk.Label(parent, text="Bluetooth Adapters", font=("", 11, "bold")).pack(anchor="w", padx=10, pady=(12, 2))
        tk.Label(
            parent, fg="#555", justify="left", wraplength=520,
            text="Remotes are automatically spread across every adapter listed here. "
                 "Plug in another USB Bluetooth dongle and pass it through to the VM, "
                 "then press Search to pick it up without restarting the daemon.",
        ).pack(anchor="w", padx=10, pady=(0, 8))

        self.adapters_tree = ttk.Treeview(parent, columns=("addr", "count", "devices"), show="tree headings", height=6)
        self.adapters_tree.heading("#0", text="Adapter")
        self.adapters_tree.column("#0", width=80)
        self.adapters_tree.heading("addr", text="Address")
        self.adapters_tree.heading("count", text="Connected")
        self.adapters_tree.heading("devices", text="Remote Addresses")
        self.adapters_tree.column("addr", width=140)
        self.adapters_tree.column("count", width=70, anchor="center")
        self.adapters_tree.column("devices", width=280)
        self.adapters_tree.pack(fill="x", padx=10, pady=(0, 8))

        tk.Button(parent, text="Search for Adapters", command=self._refresh_adapters).pack(anchor="w", padx=10, pady=(0, 10))

    def _maybe_refresh_adapters(self, notebook, settings_tab):
        if notebook.select() == str(settings_tab):
            self._refresh_adapters()

    def _refresh_adapters(self):
        self.send({"type": "get_adapters"})

    def _update_adapters(self, adapters):
        self.adapters_tree.delete(*self.adapters_tree.get_children())
        for a in adapters:
            devices = a.get("devices", [])
            self.adapters_tree.insert(
                "", "end", text=a.get("hci", "?"), iid=a.get("hci", "?"),
                values=(a.get("addr", ""), len(devices), ", ".join(devices) or "—"),
            )

    def _socket_worker(self):
        while True:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCK_PATH)
                self.sock = s
                self.msg_queue.put(("status", True))
                buf = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise OSError("daemon closed connection")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if line.strip():
                            self.msg_queue.put(("msg", json.loads(line)))
            except (OSError, json.JSONDecodeError):
                self.sock = None
                self.msg_queue.put(("status", False))
                time.sleep(2)

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "status":
                    self._set_connected(payload)
                else:
                    self._handle_msg(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def _set_connected(self, connected):
        self.status_lbl.config(
            text="Connected to wiimote_bridge daemon" if connected else "Daemon not reachable - is it running (systemctl status wiimote-bridge)?",
            fg="#2e7d32" if connected else "#c62828",
        )
        if connected:
            self._refresh_adapters()

    def _handle_msg(self, msg):
        t = msg.get("type")
        if t == "devices":
            self._sync_slots(msg.get("addrs", []), msg.get("slots", {}))
        elif t == "mapping":
            col = self._slot_for(msg.get("addr"))
            if col:
                col.set_mapping(msg.get("mapping", {}))
        elif t == "pointer":
            col = self._slot_for(msg.get("addr"))
            if col:
                col.set_pointer_config(msg.get("config", {}))
        elif t == "state":
            col = self._slot_for(msg.get("addr"))
            if col:
                for name, pressed in msg.get("buttons", {}).items():
                    col.set_button_state(name, pressed)
        elif t == "adapters":
            self._update_adapters(msg.get("adapters", []))

    def _slot_for(self, addr):
        idx = self.slot_of_addr.get(addr)
        return self.slots[idx] if idx is not None else None

    def _sync_slots(self, addrs, slot_map):
        """Column assignment always mirrors the daemon's own player-slot
        assignment (same index it uses for the remote's physical LED) --
        never computed independently here, so the two can't disagree."""
        addrs = set(addrs)
        for addr in list(self.slot_of_addr):
            if addr not in addrs:
                idx = self.slot_of_addr.pop(addr)
                self.slots[idx].bind_addr(None)
        for addr in addrs:
            idx = slot_map.get(addr)
            if idx is None or not (0 <= idx < MAX_SLOTS):
                continue
            if self.slot_of_addr.get(addr) == idx:
                continue
            # Clear any stale occupant of this slot (e.g. a ghost entry
            # from a connection that hadn't been recognized as dead yet).
            for other_addr, other_idx in list(self.slot_of_addr.items()):
                if other_idx == idx and other_addr != addr:
                    del self.slot_of_addr[other_addr]
                    self.slots[other_idx].bind_addr(None)
            self.slot_of_addr[addr] = idx
            self.slots[idx].bind_addr(addr)

    def send(self, obj):
        try:
            if self.sock:
                self.sock.sendall((json.dumps(obj) + "\n").encode())
        except OSError:
            pass


def main():
    ensure_daemon_running()
    root = tk.Tk()
    GuiApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
