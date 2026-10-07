#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto Rejoin Roblox - Termux / Android 10 (root)
Run:  sudo python main.py     (or: su -c "python main.py")
Only Python standard library is used. No pip install needed.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {"lobby_delay": 30, "place_id": "123456789"}

RELAUNCH_WAIT = 10        # seconds after kill, before relaunch
RECOVERY_COOLDOWN = 45    # ignore repeated errors for the same package
LAUNCH_STAGGER = 5        # seconds between starting each package (safe startup)

# logcat -v threadtime:  MM-DD HH:MM:SS.mmm  PID  TID L Tag: Message
LOG_RE = re.compile(
    r"^\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+(.*?)\s*:\s?(.*)$"
)
# Roblox disconnect (267 = kicked) and typical crash signatures
ERROR_RE = re.compile(
    r"(error\s*code\s*[:=]?\s*267\b"
    r"|\b267\b.{0,60}(kick|disconnect|banned)"
    r"|(kick|disconnect).{0,60}\b267\b"
    r"|FATAL EXCEPTION"
    r"|Fatal signal \d+"
    r"|ANR in com\.roblox)",
    re.IGNORECASE,
)


def sh(cmd, timeout=20):
    """Run a command as root and return stdout (stripped). Never raises."""
    try:
        r = subprocess.run(
            ["su", "-c", cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            universal_newlines=True,
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def fmt_time(sec):
    sec = max(0, int(sec))
    return "%02d:%02d:%02d" % (sec // 3600, (sec % 3600) // 60, sec % 60)


# --------------------------------------------------------------------------
class Config:
    def __init__(self, path=CONFIG_FILE):
        self.path = path
        self.data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            self.save()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data.update({k: loaded[k] for k in DEFAULT_CONFIG if k in loaded})
        except Exception:
            self.save()

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=4)
        except Exception as e:
            print("Gagal menyimpan config:", e)

    @property
    def lobby_delay(self):
        try:
            return max(0, int(self.data["lobby_delay"]))
        except Exception:
            return DEFAULT_CONFIG["lobby_delay"]

    @property
    def place_id(self):
        return str(self.data["place_id"]).strip()


# --------------------------------------------------------------------------
class RobloxManager:
    """Package scan, launch flow, PID tracking and recovery."""

    def __init__(self, config):
        self.config = config
        self.packages = []
        self.selected = []
        self.state = {}            # pkg -> dict(pid, pids, start, status, last_recover)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self._threads = []

    # ---- scan ----
    def scan_packages(self):
        out = sh("pm list packages | grep roblox")
        pkgs = []
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkgs.append(line[len("package:"):].strip())
        self.packages = sorted(set(pkgs))
        return self.packages

    # ---- state ----
    def init_state(self, pkgs):
        with self.lock:
            self.selected = list(pkgs)
            for p in pkgs:
                self.state[p] = {
                    "pid": None, "pids": set(), "start": time.time(),
                    "status": "Starting", "last_recover": 0.0,
                }

    def set_status(self, pkg, status):
        with self.lock:
            if pkg in self.state:
                self.state[pkg]["status"] = status

    def get_pids(self, pkg):
        out = sh("pidof %s" % pkg, timeout=8)
        return {int(x) for x in out.split() if x.isdigit()}

    def pkg_for_pid(self, pid):
        with self.lock:
            for pkg, st in self.state.items():
                if pid in st["pids"]:
                    return pkg
        return None

    # ---- launch ----
    def launch_flow(self, pkg, first_delay=0):
        """Step A -> wait lobby_delay -> Step C."""
        if first_delay and self.stop_event.wait(first_delay):
            return
        self.set_status(pkg, "Launching")
        sh("monkey -p %s -c android.intent.category.LAUNCHER 1" % pkg, timeout=30)
        if self.stop_event.wait(self.config.lobby_delay):
            return
        self.set_status(pkg, "Joining")
        sh(
            "am start -a android.intent.action.VIEW -d 'roblox://placeId=%s' %s"
            % (self.config.place_id, pkg),
            timeout=30,
        )
        with self.lock:
            if pkg in self.state:
                self.state[pkg]["start"] = time.time()
                self.state[pkg]["status"] = "Running"

    def start_all(self):
        t = threading.Thread(target=self._start_sequence, daemon=True)
        t.start()
        self._threads.append(t)
        p = threading.Thread(target=self._pid_poller, daemon=True)
        p.start()
        self._threads.append(p)

    def _start_sequence(self):
        for i, pkg in enumerate(self.selected):
            if self.stop_event.is_set():
                return
            threading.Thread(
                target=self.launch_flow, args=(pkg,), daemon=True
            ).start()
            if self.stop_event.wait(LAUNCH_STAGGER):
                return

    # ---- pid poller ----
    def _pid_poller(self):
        while not self.stop_event.is_set():
            for pkg in list(self.selected):
                if self.stop_event.is_set():
                    return
                try:
                    pids = self.get_pids(pkg)
                except Exception:
                    pids = set()
                with self.lock:
                    st = self.state.get(pkg)
                    if not st:
                        continue
                    new_main = min(pids) if pids else None
                    if new_main != st["pid"]:
                        # new process => reset uptime
                        if new_main is not None:
                            st["start"] = time.time()
                        st["pid"] = new_main
                    st["pids"] = pids
                    if not pids and st["status"] == "Running":
                        st["status"] = "Stopped"
            self.stop_event.wait(2)

    # ---- recovery ----
    def handle_error(self, pid, line):
        pkg = self.pkg_for_pid(pid)
        if not pkg:
            return  # not one of our managed processes -> never touch
        now = time.time()
        with self.lock:
            st = self.state[pkg]
            if st["status"] in ("Relaunching", "Launching", "Joining"):
                return
            if now - st["last_recover"] < RECOVERY_COOLDOWN:
                return
            st["last_recover"] = now
            st["status"] = "Error Detected"
        threading.Thread(
            target=self._recover, args=(pkg, pid), daemon=True
        ).start()

    def _recover(self, pkg, pid):
        # kill ONLY the exact PID, SIGTERM only (never -9)
        sh("kill -15 %d" % pid)
        self.set_status(pkg, "Relaunching")
        if self.stop_event.wait(RELAUNCH_WAIT):
            return
        self.launch_flow(pkg)

    # ---- shutdown ----
    def shutdown(self):
        self.stop_event.set()


# --------------------------------------------------------------------------
class LogcatSensor(threading.Thread):
    def __init__(self, manager):
        super().__init__(daemon=True)
        self.manager = manager
        self.proc = None

    def run(self):
        stop = self.manager.stop_event
        while not stop.is_set():
            try:
                sh("logcat -c", timeout=10)
                self.proc = subprocess.Popen(
                    ["su", "-c", "logcat -v threadtime"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    universal_newlines=True,
                    errors="ignore",
                    bufsize=1,
                )
                for line in self.proc.stdout:
                    if stop.is_set():
                        break
                    if "267" not in line and "FATAL" not in line and \
                            "Fatal signal" not in line and "ANR" not in line:
                        continue  # cheap pre-filter
                    if not ERROR_RE.search(line):
                        continue
                    m = LOG_RE.match(line.strip())
                    if not m:
                        continue
                    self.manager.handle_error(int(m.group(1)), line)
            except Exception:
                pass
            finally:
                self.terminate()
            stop.wait(2)  # logcat died -> restart

    def terminate(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        self.proc = None


# --------------------------------------------------------------------------
class Dashboard:
    def __init__(self, manager):
        self.m = manager

    def render(self):
        os.system("clear")
        w = 62
        print("=" * w)
        print(" AUTO REJOIN ROBLOX - LIVE MONITOR".center(w))
        print("=" * w)
        print("%-22s %-8s %-10s %-18s" % ("Package", "PID", "Online", "Status"))
        print("-" * w)
        now = time.time()
        with self.m.lock:
            for pkg in self.m.selected:
                st = self.m.state[pkg]
                pid = st["pid"] if st["pid"] else "-"
                up = fmt_time(now - st["start"]) if st["pid"] else "00:00:00"
                print("%-22s %-8s %-10s %-18s" % (pkg[-22:], pid, up, st["status"]))
        print("-" * w)
        print(" lobby_delay=%ss  place_id=%s" % (self.m.config.lobby_delay, self.m.config.place_id))
        print(" Ctrl+C untuk berhenti")

    def run(self):
        while not self.m.stop_event.is_set():
            self.render()
            time.sleep(1.5)


# --------------------------------------------------------------------------
class App:
    def __init__(self):
        self.config = Config()
        self.manager = RobloxManager(self.config)
        self.sensor = None

    def banner(self):
        os.system("clear")
        print("=" * 40)
        print("        AUTO REJOIN ROBLOX")
        print("=" * 40)

    def menu(self):
        while True:
            self.banner()
            print("Paket terdeteksi: %d" % len(self.manager.packages))
            print("[1] Mulai / Start AFK")
            print("[2] Setting")
            print("[3] Keluar / Exit")
            c = input("\nPilih: ").strip()
            if c == "1":
                if self.start():
                    return
            elif c == "2":
                self.settings()
            elif c == "3":
                return

    def settings(self):
        while True:
            self.banner()
            print("[1] lobby_delay : %s" % self.config.data["lobby_delay"])
            print("[2] place_id    : %s" % self.config.data["place_id"])
            print("[3] Kembali")
            c = input("\nPilih: ").strip()
            if c == "1":
                v = input("lobby_delay (detik): ").strip()
                if v.isdigit():
                    self.config.data["lobby_delay"] = int(v)
                    self.config.save()
            elif c == "2":
                v = input("place_id: ").strip()
                if v.isdigit():
                    self.config.data["place_id"] = v
                    self.config.save()
            elif c == "3":
                return

    def start(self):
        pkgs = self.manager.packages
        if not pkgs:
            input("Tidak ada paket Roblox ditemukan. Enter...")
            return False
        self.banner()
        for i, p in enumerate(pkgs, 1):
            print("%d. %s" % (i, p))
        raw = input("\nPilih (contoh 1,2,4): ").strip()
        idx = []
        for part in raw.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= len(pkgs):
                n = int(part) - 1
                if n not in idx:
                    idx.append(n)
        if not idx:
            input("Pilihan tidak valid. Enter...")
            return False
        chosen = [pkgs[i] for i in idx]
        self.manager.init_state(chosen)
        self.sensor = LogcatSensor(self.manager)
        self.sensor.start()
        self.manager.start_all()
        Dashboard(self.manager).run()
        return True

    def shutdown(self):
        self.manager.shutdown()
        if self.sensor:
            self.sensor.terminate()

    def run(self):
        print("Scanning paket Roblox...")
        self.manager.scan_packages()
        try:
            self.menu()
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            self.shutdown()
            print("\nBerhenti. Sampai jumpa.")


if __name__ == "__main__":
    App().run()
    sys.exit(0)
