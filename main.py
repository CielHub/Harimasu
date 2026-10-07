#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Auto Rejoin Roblox
------------------
Android 10 + Termux + Magisk/KernelSU

Features:
- Automatic scan of installed Roblox clone packages.
- Interactive menu + fixed-width terminal dashboard.
- Per-package launch/recovery workers.
- Logcat-based error sensor with exact PID extraction.
- Exact-PID SIGTERM (kill -15) only. Never uses kill -9.
- Config stored in config.json.

Standard library only.
"""

from __future__ import annotations

import json
import os
import queue
import re
import select
import shlex
import signal
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple


APP_NAME = "Auto Rejoin Roblox"
STARTUP_CONCURRENCY = 2
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "lobby_delay": 30,
    "place_id": "123456789",
}

# Exact PID is extracted from the logcat line. The PID must also belong to a
# currently selected package before any kill command is allowed.
THREADTIME_RE = re.compile(
    r"^\s*(?:\d{2}-\d{2}|\d{4}-\d{2}-\d{2})"
    r"\s+\d{2}:\d{2}:\d{2}\.\d+"
    r"\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]+):\s*(.*)$"
)

DISCONNECT_267_RE = re.compile(r"\b267\b")
DISCONNECT_277_RE = re.compile(r"\b277\b")
CRASH_PATTERNS = (
    re.compile(r"fatal exception", re.I),
    re.compile(r"fatal signal\s+\d+", re.I),
    re.compile(r"sigsegv", re.I),
    re.compile(r"sigabrt", re.I),
    re.compile(r"segmentation fault", re.I),
    re.compile(r"process .*\bhas died\b", re.I),
    re.compile(r"application not responding", re.I),
    re.compile(r"\banr\b", re.I),
)

ANSI_CLEAR = "\033[2J\033[H"
ANSI_HIDE_CURSOR = "\033[?25l"
ANSI_SHOW_CURSOR = "\033[?25h"


@dataclass(frozen=True)
class CommandResult:
    code: int
    stdout: str
    stderr: str


class ConfigManager:
    """Load, validate, and atomically save config.json."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def ensure(self) -> dict:
        with self.lock:
            if not self.path.exists():
                self._atomic_write(DEFAULT_CONFIG.copy())
            return self.load()

    def load(self) -> dict:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = DEFAULT_CONFIG.copy()
                self._atomic_write(data)

            lobby_delay = data.get("lobby_delay", DEFAULT_CONFIG["lobby_delay"])
            place_id = data.get("place_id", DEFAULT_CONFIG["place_id"])

            try:
                lobby_delay = max(0, int(lobby_delay))
            except (TypeError, ValueError):
                lobby_delay = DEFAULT_CONFIG["lobby_delay"]

            place_id = str(place_id).strip()
            if not place_id.isdigit():
                place_id = DEFAULT_CONFIG["place_id"]

            normalized = {
                "lobby_delay": lobby_delay,
                "place_id": place_id,
            }

            if normalized != data:
                self._atomic_write(normalized)

            return normalized

    def update(self, lobby_delay: int, place_id: str) -> dict:
        normalized = {
            "lobby_delay": max(0, int(lobby_delay)),
            "place_id": str(place_id).strip(),
        }
        if not normalized["place_id"].isdigit():
            raise ValueError("Place ID harus berupa angka.")
        with self.lock:
            self._atomic_write(normalized)
        return normalized

    def _atomic_write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)


class AndroidShell:
    """Root command wrapper. Never invokes package-name killing."""

    def __init__(self, command_timeout: float = 15.0):
        self.command_timeout = command_timeout
        self.command_lock = threading.RLock()

    def run_su(self, command: str, timeout: Optional[float] = None) -> CommandResult:
        """Run one command through su -c without shell=True."""
        if timeout is None:
            timeout = self.command_timeout
        try:
            completed = subprocess.run(
                ["su", "-c", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            return CommandResult(
                completed.returncode,
                completed.stdout.strip(),
                completed.stderr.strip(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return CommandResult(124, stdout.strip(), stderr.strip() or "command timeout")
        except FileNotFoundError:
            return CommandResult(127, "", "su tidak ditemukan")
        except Exception as exc:
            return CommandResult(1, "", str(exc))

    def require_root(self) -> None:
        result = self.run_su("id -u", timeout=5)
        if result.code != 0 or result.stdout.strip() != "0":
            raise RuntimeError(
                "Root tidak tersedia. Pastikan Magisk/KernelSU aktif dan Termux diberi akses root."
            )

    def scan_roblox_packages(self) -> List[str]:
        """Primary scan required by the specification, with a safe fallback."""
        result = self.run_su("pm list packages | grep roblox", timeout=10)
        packages: List[str] = []

        if result.stdout:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.startswith("package:"):
                    pkg = line[len("package:"):].strip()
                    if pkg and "roblox" in pkg.lower():
                        packages.append(pkg)

        if not packages:
            fallback = self.run_su("pm list packages", timeout=10)
            for line in fallback.stdout.splitlines():
                line = line.strip()
                if line.startswith("package:"):
                    pkg = line[len("package:"):].strip()
                    if pkg and "roblox" in pkg.lower():
                        packages.append(pkg)

        return sorted(set(packages))

    def pidof(self, package: str) -> List[int]:
        result = self.run_su(f"pidof {shlex.quote(package)}", timeout=5)
        if result.code != 0 or not result.stdout:
            return []
        pids: List[int] = []
        for token in result.stdout.replace("\n", " ").split():
            if token.isdigit():
                pids.append(int(token))
        return sorted(set(pids))

    def proc_cmdline(self, pid: int) -> str:
        if not isinstance(pid, int) or pid <= 0:
            return ""
        result = self.run_su(f"cat /proc/{pid}/cmdline", timeout=3)
        if result.code != 0:
            return ""
        raw = result.stdout.replace("\x00", "").strip()
        return raw.splitlines()[0].strip() if raw else ""

    def launch_normal(self, package: str) -> CommandResult:
        pkg = shlex.quote(package)
        command = f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1"
        result = self.run_su(command, timeout=20)
        if result.code == 0:
            return result

        # Fallback for devices where monkey is unavailable or the launcher
        # resolver rejects the package. Still launches only the target package.
        fallback = f"am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER {pkg}"
        return self.run_su(fallback, timeout=20)

    def open_deep_link(self, package: str, place_id: str) -> CommandResult:
        pkg = shlex.quote(package)
        uri = shlex.quote(f"roblox://placeId={place_id}")
        command = f"am start -a android.intent.action.VIEW -d {uri} {pkg}"
        return self.run_su(command, timeout=20)

    def verify_pid_for_package(self, pid: int, package: str) -> bool:
        """Verify an exact PID belongs to the exact selected package."""
        if pid not in self.pidof(package):
            return False
        cmdline = self.proc_cmdline(pid)
        if not cmdline:
            return False
        # Main Roblox process normally uses the package name as cmdline.
        # Allow ':' suffixes for auxiliary Android processes if present.
        base = cmdline.split(":", 1)[0]
        return base == package

    def kill_exact_pid(self, pid: int, package: str) -> bool:
        """
        Safely terminate one exact PID with SIGTERM.

        IMPORTANT: this intentionally does NOT use kill -9 and does NOT use
        package-level stopping commands such as am force-stop.
        """
        if not self.verify_pid_for_package(pid, package):
            return False
        if not str(pid).isdigit() or pid <= 1:
            return False
        result = self.run_su(f"kill -15 {pid}", timeout=5)
        return result.code == 0


class PackageScanner:
    def __init__(self, shell: AndroidShell):
        self.shell = shell

    def scan(self) -> List[str]:
        return self.shell.scan_roblox_packages()


class RobloxInstance:
    """Mutable per-package runtime state."""

    def __init__(self, package: str):
        self.package = package
        self.lock = threading.RLock()
        self.active = True
        self.generation = 0
        self.pids: List[int] = []
        self.online_since: Optional[float] = None
        self.status = "Idle"
        self.last_event = ""
        self.events: Deque[str] = deque(maxlen=8)
        self.recovery_queue: queue.Queue[Tuple[int, str]] = queue.Queue(maxsize=1)
        self.recovery_thread: Optional[threading.Thread] = None
        self.recovery_last_pid: Optional[int] = None
        self.last_error_fingerprint: Optional[Tuple[int, str]] = None
        self.last_error_time = 0.0

    def add_event(self, message: str) -> None:
        now = time.strftime("%H:%M:%S")
        line = f"{now} {message}"
        with self.lock:
            self.last_event = message
            self.events.append(line)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "package": self.package,
                "active": self.active,
                "generation": self.generation,
                "pids": list(self.pids),
                "online_since": self.online_since,
                "status": self.status,
                "last_event": self.last_event,
                "events": list(self.events),
            }

    def update_pids(self, pids: List[int]) -> None:
        with self.lock:
            self.pids = list(pids)

    def primary_pid(self) -> Optional[int]:
        with self.lock:
            return self.pids[0] if self.pids else None

    def bump_generation(self) -> int:
        with self.lock:
            self.generation += 1
            return self.generation

    def current_generation(self) -> int:
        with self.lock:
            return self.generation

    def is_current(self, generation: int) -> bool:
        with self.lock:
            return self.active and self.generation == generation

    def set_status(self, status: str) -> None:
        with self.lock:
            if self.active:
                self.status = status

    def set_online(self) -> None:
        with self.lock:
            if self.active:
                self.online_since = time.monotonic()
                self.status = "Running"

    def clear_uptime(self) -> None:
        with self.lock:
            self.online_since = None

    def deactivate(self) -> None:
        with self.lock:
            self.active = False
            self.generation += 1
            self.status = "Stopped"
            self.online_since = None
        try:
            while True:
                self.recovery_queue.get_nowait()
                self.recovery_queue.task_done()
        except queue.Empty:
            pass

    def can_queue_recovery(self, pid: int, reason: str) -> bool:
        with self.lock:
            if not self.active:
                return False
            if pid not in self.pids:
                return False
            fingerprint = (pid, reason)
            now = time.monotonic()
            if fingerprint == self.last_error_fingerprint and now - self.last_error_time < 4.0:
                return False
            self.last_error_fingerprint = fingerprint
            self.last_error_time = now
            try:
                self.recovery_queue.put_nowait((pid, reason))
            except queue.Full:
                return False
            # Invalidate any normal launch flow immediately.
            self.generation += 1
            self.online_since = None
            self.status = "Error Detected"
            return True


class RobloxManager:
    def __init__(self, shell: AndroidShell, config: ConfigManager, shutdown_event: threading.Event):
        self.shell = shell
        self.config = config
        self.shutdown_event = shutdown_event
        self.lock = threading.RLock()
        self.instances: Dict[str, RobloxInstance] = {}
        self.pid_map: Dict[int, str] = {}
        self.pid_map_lock = threading.RLock()
        self.startup_semaphore = threading.Semaphore(STARTUP_CONCURRENCY)

    def instances_snapshot(self) -> List[RobloxInstance]:
        with self.lock:
            return list(self.instances.values())

    def start_sessions(self, packages: List[str]) -> None:
        self.stop_sessions(kill=False, clear=True)
        with self.lock:
            for package in packages:
                self.instances[package] = RobloxInstance(package)

        for instance in self.instances_snapshot():
            self._start_recovery_worker(instance)

        # Launch selected clones in parallel. Android remains the authority on
        # the actual window/foreground state, while each Python worker stays isolated.
        for instance in self.instances_snapshot():
            thread = threading.Thread(
                target=self._launch_flow_entry,
                args=(instance,),
                name=f"launch-{instance.package}",
                daemon=True,
            )
            thread.start()

    def _start_recovery_worker(self, instance: RobloxInstance) -> None:
        thread = threading.Thread(
            target=self._recovery_worker,
            args=(instance,),
            name=f"recovery-{instance.package}",
            daemon=True,
        )
        instance.recovery_thread = thread
        thread.start()

    def _launch_flow_entry(self, instance: RobloxInstance) -> None:
        self._launch_flow(instance, instance.current_generation())

    def _launch_flow(self, instance: RobloxInstance, generation: int) -> None:
        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return

        cfg = self.config.load()
        package = instance.package
        instance.add_event("Launching app")
        instance.set_status("Starting")

        with self.startup_semaphore:
            launch_result = self.shell.launch_normal(package)
        if launch_result.code != 0:
            instance.add_event(f"Launch failed ({launch_result.code})")
            instance.set_status("Launch Failed")
            return

        # Wait for the process to appear before entering the lobby timer.
        pid_deadline = time.monotonic() + 12.0
        while time.monotonic() < pid_deadline:
            if not instance.is_current(generation) or self.shutdown_event.is_set():
                return
            pids = self.shell.pidof(package)
            instance.update_pids(pids)
            if pids:
                break
            self.shutdown_event.wait(0.5)

        if not instance.is_current(generation):
            return

        delay = int(cfg.get("lobby_delay", DEFAULT_CONFIG["lobby_delay"]))
        instance.add_event(f"Lobby wait {delay}s")
        instance.set_status(f"Lobby {delay}s")
        if not self._wait_with_generation(instance, generation, float(delay), "Lobby"):
            return

        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return

        instance.add_event(f"Joining Place {cfg['place_id']}")
        instance.set_status("Joining")
        with self.startup_semaphore:
            join_result = self.shell.open_deep_link(package, cfg["place_id"])
        if join_result.code != 0:
            instance.add_event(f"Deep link failed ({join_result.code})")
            instance.set_status("Join Failed")
            return

        # Refresh once after the join intent.
        time.sleep(0.5)
        instance.update_pids(self.shell.pidof(package))
        instance.set_online()
        instance.add_event("Session online")

    def _wait_with_generation(
        self,
        instance: RobloxInstance,
        generation: int,
        seconds: float,
        label: str,
    ) -> bool:
        deadline = time.monotonic() + seconds
        last_display = -1
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            if not instance.is_current(generation) or self.shutdown_event.is_set():
                return False
            whole = int(remaining + 0.999)
            if whole != last_display:
                instance.set_status(f"{label} {whole:02d}s")
                last_display = whole
            if remaining <= 0:
                return True
            self.shutdown_event.wait(min(0.25, remaining))

    def _recovery_worker(self, instance: RobloxInstance) -> None:
        while not self.shutdown_event.is_set():
            if not instance.snapshot()["active"]:
                return
            try:
                pid, reason = instance.recovery_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                if not instance.snapshot()["active"]:
                    continue

                instance.recovery_last_pid = pid
                instance.clear_uptime()
                instance.set_status(f"Kill PID {pid}")
                instance.add_event(f"Recovery: {reason}, PID {pid}")

                killed = self.shell.kill_exact_pid(pid, instance.package)
                if not killed:
                    instance.add_event(f"Kill refused/failed for PID {pid}")
                    instance.set_status("Kill Failed")
                    continue

                # Exactly 10 seconds measured from the successful kill command.
                instance.set_status("Relaunch 10s")
                kill_deadline = time.monotonic() + 10.0
                while True:
                    if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                        return
                    remaining = kill_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    instance.set_status(f"Relaunch {int(remaining + 0.999):02d}s")
                    self.shutdown_event.wait(min(0.25, remaining))

                # If another recovery is already queued, handle that event first.
                try:
                    queued = instance.recovery_queue.get_nowait()
                except queue.Empty:
                    queued = None
                else:
                    instance.recovery_queue.task_done()
                    try:
                        instance.recovery_queue.put_nowait(queued)
                    except queue.Full:
                        pass
                    continue

                generation = instance.current_generation()
                instance.set_status("Relaunching")
                self._launch_flow(instance, generation)
            finally:
                instance.recovery_queue.task_done()

    def handle_sensor_event(self, pid: int, reason: str, raw_line: str) -> None:
        package = self.resolve_pid(pid)
        if not package:
            return

        with self.lock:
            instance = self.instances.get(package)
        if not instance:
            return

        message = raw_line.strip()
        if instance.can_queue_recovery(pid, reason):
            instance.add_event(f"Sensor: {reason}")

    def resolve_pid(self, pid: int) -> Optional[str]:
        if pid <= 0:
            return None
        with self.pid_map_lock:
            package = self.pid_map.get(pid)
        if package:
            with self.lock:
                instance = self.instances.get(package)
            if instance and instance.snapshot()["active"]:
                # Extra verification prevents a stale PID map from becoming a kill target.
                if self.shell.verify_pid_for_package(pid, package):
                    return package

        # Fallback: read the exact process command line, then validate with pidof.
        cmdline = self.shell.proc_cmdline(pid)
        if not cmdline:
            return None
        base = cmdline.split(":", 1)[0]
        with self.lock:
            candidates = list(self.instances.items())
        for package, instance in candidates:
            if not instance.snapshot()["active"]:
                continue
            if base == package and self.shell.verify_pid_for_package(pid, package):
                return package
        return None

    def refresh_pids(self) -> None:
        new_map: Dict[int, str] = {}
        for instance in self.instances_snapshot():
            snap = instance.snapshot()
            if not snap["active"]:
                continue
            try:
                pids = self.shell.pidof(instance.package)
            except Exception:
                pids = []
            instance.update_pids(pids)
            for pid in pids:
                new_map[pid] = instance.package
        with self.pid_map_lock:
            self.pid_map = new_map

    def stop_sessions(self, kill: bool = True, clear: bool = True) -> None:
        instances = self.instances_snapshot()
        for instance in instances:
            snap = instance.snapshot()
            if kill and snap["active"]:
                for pid in snap["pids"]:
                    try:
                        if self.shell.kill_exact_pid(pid, instance.package):
                            instance.add_event(f"Stopped PID {pid}")
                    except Exception:
                        pass
            instance.deactivate()
        with self.pid_map_lock:
            self.pid_map = {}
        if clear:
            with self.lock:
                self.instances = {}


class PidMonitor(threading.Thread):
    def __init__(self, manager: RobloxManager, stop_event: threading.Event):
        super().__init__(name="pid-monitor", daemon=True)
        self.manager = manager
        self.stop_event = stop_event

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.manager.refresh_pids()
            except Exception:
                pass
            self.stop_event.wait(1.0)


class LogcatSensor(threading.Thread):
    """Continuously reads `su -c 'exec logcat -v threadtime'`."""

    def __init__(self, manager: RobloxManager, stop_event: threading.Event):
        super().__init__(name="logcat-sensor", daemon=True)
        self.manager = manager
        self.stop_event = stop_event
        self.process: Optional[subprocess.Popen] = None
        self.process_lock = threading.RLock()

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._run_logcat_once()
            except Exception:
                pass
            if not self.stop_event.is_set():
                self.stop_event.wait(2.0)

    def _run_logcat_once(self) -> None:
        with self.process_lock:
            self.process = subprocess.Popen(
                ["su", "-c", "exec logcat -v threadtime"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

        try:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if self.stop_event.is_set():
                    break
                parsed = self._parse_line(line)
                if not parsed:
                    continue
                pid, _tid, tag, message = parsed
                reason = self._detect_reason(tag, message)
                if reason:
                    self.manager.handle_sensor_event(pid, reason, line)
        finally:
            with self.process_lock:
                proc = self.process
                self.process = None
            if proc is not None:
                try:
                    if proc.poll() is None:
                        proc.terminate()  # SIGTERM only; never SIGKILL.
                    proc.wait(timeout=3)
                except Exception:
                    pass

    @staticmethod
    def _parse_line(line: str) -> Optional[Tuple[int, int, str, str]]:
        match = THREADTIME_RE.match(line)
        if not match:
            return None
        pid = int(match.group(1))
        tid = int(match.group(2))
        tag = match.group(4).strip()
        message = match.group(5).strip()
        return pid, tid, tag, message

    @staticmethod
    def _detect_reason(tag: str, message: str) -> Optional[str]:
        text = f"{tag} {message}"
        if DISCONNECT_267_RE.search(text):
            return "Disconnect 267"
        if DISCONNECT_277_RE.search(text):
            return "Disconnect 277"
        for pattern in CRASH_PATTERNS:
            if pattern.search(text):
                return f"Crash: {pattern.pattern}"
        return None

    def stop_process(self) -> None:
        with self.process_lock:
            proc = self.process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()  # SIGTERM only.
            except Exception:
                pass


class Dashboard:
    """Small adaptive-width dashboard designed for Termux/Android screens."""

    def __init__(self, manager: RobloxManager, shutdown_event: threading.Event):
        self.manager = manager
        self.shutdown_event = shutdown_event
        self.show_logs = False

    @staticmethod
    def _fit(text: str, width: int) -> str:
        text = str(text)
        if len(text) > width:
            if width <= 1:
                return text[:width]
            return text[: width - 1] + "~"
        return text.ljust(width)

    @staticmethod
    def _uptime(online_since: Optional[float]) -> str:
        if online_since is None:
            return "--:--:--"
        seconds = max(0, int(time.monotonic() - online_since))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def _draw(self) -> None:
        terminal_columns = shutil.get_terminal_size((72, 24)).columns
        width = max(44, min(terminal_columns, 100))

        # Keep the dashboard usable even in narrow portrait-mode Termux.
        num_w = 3
        pid_w = 8
        online_w = 8
        status_w = 14
        package_w = max(10, width - (num_w + pid_w + online_w + status_w + 8))

        rows = []
        for index, instance in enumerate(self.manager.instances_snapshot(), start=1):
            snap = instance.snapshot()
            pids = snap["pids"]
            pid_text = str(pids[0]) if pids else "-"
            if len(pids) > 1:
                pid_text += f" +{len(pids)-1}"
            rows.append(
                f"{self._fit(index, num_w)} "
                f"{self._fit(snap['package'], package_w)} "
                f"{self._fit(pid_text, pid_w)} "
                f"{self._fit(self._uptime(snap['online_since']), online_w)} "
                f"{self._fit(snap['status'], status_w)}"
            )

        inner = width - 2
        cfg = self.manager.config.load()
        lines = [
            ANSI_CLEAR,
            f"+{'=' * inner}+",
            f"|{self._fit(APP_NAME, inner)}|",
            f"|{self._fit(f"Place ID: {cfg['place_id']}  |  Lobby: {cfg['lobby_delay']}s", inner)}|",
            f"+{'-' * inner}+",
            f"|{self._fit('#', num_w)} {self._fit('Package Name', package_w)} {self._fit('PID', pid_w)} {self._fit('Online', online_w)} {self._fit('Status', status_w)}|",
            f"+{'-' * inner}+",
        ]
        lines.extend(f"|{self._fit(row, inner)}|" for row in rows)
        if not rows:
            lines.append(f"|{self._fit('No active session.', inner)}|")

        lines += [
            f"+{'-' * inner}+",
            f"|{self._fit('Keys: [q] Quit  [s] Stop+Menu  [r] Refresh PID  [l] Logs', inner)}|",
            f"+{'=' * inner}+",
        ]

        if self.show_logs:
            lines.append("Events:")
            for instance in self.manager.instances_snapshot():
                snap = instance.snapshot()
                for event in snap["events"][-2:]:
                    event_line = f"  {snap['package']}: {event}"
                    lines.append(self._fit(event_line, width))

        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

    def run(self) -> str:
        """Return 'menu' or 'quit'."""
        raw_mode = sys.stdin.isatty() and sys.stdout.isatty()
        old_attrs = None

        try:
            if raw_mode:
                old_attrs = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
                sys.stdout.write(ANSI_HIDE_CURSOR)
                sys.stdout.flush()

            while not self.shutdown_event.is_set():
                self._draw()
                if raw_mode:
                    ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                    if ready:
                        key = sys.stdin.read(1).lower()
                        if key == "q":
                            self.shutdown_event.set()
                            return "quit"
                        if key == "s":
                            self.manager.stop_sessions(kill=True, clear=True)
                            return "menu"
                        if key == "r":
                            self.manager.refresh_pids()
                        if key == "l":
                            self.show_logs = not self.show_logs
                else:
                    self.shutdown_event.wait(1.0)
        finally:
            if raw_mode and old_attrs is not None:
                try:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attrs)
                except Exception:
                    pass
            sys.stdout.write(ANSI_SHOW_CURSOR)
            sys.stdout.flush()

        return "quit"


class AutoRejoinRobloxApp:
    def __init__(self):
        self.shutdown_event = threading.Event()
        self.config = ConfigManager(CONFIG_PATH)
        self.shell = AndroidShell()
        self.scanner = PackageScanner(self.shell)
        self.manager = RobloxManager(self.shell, self.config, self.shutdown_event)
        self.pid_monitor = PidMonitor(self.manager, self.shutdown_event)
        self.logcat_sensor = LogcatSensor(self.manager, self.shutdown_event)
        self.packages: List[str] = []

    def run(self) -> None:
        try:
            self.config.ensure()
            self.shell.require_root()
            self.packages = self.scanner.scan()
            self._start_background_monitors()
            self._main_menu()
        except KeyboardInterrupt:
            self.shutdown_event.set()
        except Exception as exc:
            self.shutdown_event.set()
            self._safe_print(f"\nERROR: {exc}")
            self._safe_print("Tekan Enter untuk keluar...")
            try:
                input()
            except EOFError:
                pass
        finally:
            self.shutdown()

    def _start_background_monitors(self) -> None:
        if not self.pid_monitor.is_alive():
            self.pid_monitor.start()
        if not self.logcat_sensor.is_alive():
            self.logcat_sensor.start()

    def _main_menu(self) -> None:
        while not self.shutdown_event.is_set():
            self._clear_screen()
            cfg = self.config.load()
            self._safe_print("=" * 60)
            self._safe_print("           AUTO REJOIN ROBLOX")
            self._safe_print("=" * 60)
            self._safe_print(f"Packages terdeteksi : {len(self.packages)}")
            self._safe_print(f"Lobby delay        : {cfg['lobby_delay']} detik")
            self._safe_print(f"Place ID           : {cfg['place_id']}")
            self._safe_print("-")
            self._safe_print("[1] Mulai / Start AFK")
            self._safe_print("[2] Setting")
            self._safe_print("[3] Keluar / Exit")
            self._safe_print("=" * 60)

            try:
                choice = input("Pilih: ").strip()
            except EOFError:
                return

            if choice == "1":
                self._start_menu()
            elif choice == "2":
                self._settings_menu()
            elif choice == "3":
                return
            else:
                self._safe_print("Pilihan tidak valid.")
                time.sleep(1)

    def _start_menu(self) -> None:
        self.packages = self.scanner.scan()
        self._clear_screen()
        self._safe_print("=" * 60)
        self._safe_print("SELECT ROBLOX PACKAGES")
        self._safe_print("=" * 60)

        if not self.packages:
            self._safe_print("Tidak ada package Roblox yang terdeteksi.")
            input("Enter untuk kembali...")
            return

        for idx, package in enumerate(self.packages, start=1):
            self._safe_print(f"{idx:>2}. {package}")

        self._safe_print("-")
        self._safe_print("Masukkan contoh: 1,2,4")
        try:
            raw = input("Pilih package: ").strip()
        except EOFError:
            return

        selected = self._parse_selection(raw, len(self.packages))
        if not selected:
            self._safe_print("Tidak ada pilihan yang valid.")
            time.sleep(1.5)
            return

        selected_packages = [self.packages[i - 1] for i in selected]
        self.manager.start_sessions(selected_packages)
        dashboard = Dashboard(self.manager, self.shutdown_event)
        result = dashboard.run()
        if result == "quit":
            return

    @staticmethod
    def _parse_selection(raw: str, maximum: int) -> List[int]:
        result = set()
        for token in raw.split(","):
            token = token.strip()
            if token.isdigit():
                value = int(token)
                if 1 <= value <= maximum:
                    result.add(value)
        return sorted(result)

    def _settings_menu(self) -> None:
        while not self.shutdown_event.is_set():
            cfg = self.config.load()
            self._clear_screen()
            self._safe_print("=" * 60)
            self._safe_print("SETTING")
            self._safe_print("=" * 60)
            self._safe_print(f"1. Lobby delay : {cfg['lobby_delay']} detik")
            self._safe_print(f"2. Place ID    : {cfg['place_id']}")
            self._safe_print("0. Kembali")
            self._safe_print("=" * 60)

            try:
                choice = input("Pilih: ").strip()
            except EOFError:
                return

            if choice == "0":
                return
            if choice == "1":
                try:
                    value = int(input("Lobby delay (detik): ").strip())
                    if value < 0:
                        raise ValueError
                    self.config.update(value, cfg["place_id"])
                    self._safe_print("Lobby delay disimpan.")
                except ValueError:
                    self._safe_print("Nilai lobby delay harus angka >= 0.")
                time.sleep(1.0)
            elif choice == "2":
                place_id = input("Place ID: ").strip()
                if not place_id.isdigit():
                    self._safe_print("Place ID harus berupa angka.")
                else:
                    self.config.update(cfg["lobby_delay"], place_id)
                    self._safe_print("Place ID disimpan.")
                time.sleep(1.0)
            else:
                self._safe_print("Pilihan tidak valid.")
                time.sleep(1.0)

    def shutdown(self) -> None:
        self.shutdown_event.set()
        try:
            self.manager.stop_sessions(kill=True, clear=True)
        except Exception:
            pass
        try:
            self.logcat_sensor.stop_process()
        except Exception:
            pass

        for thread in (self.pid_monitor, self.logcat_sensor):
            try:
                thread.join(timeout=3)
            except Exception:
                pass

        self._clear_screen()
        self._safe_print("Auto Rejoin Roblox dihentikan.")

    @staticmethod
    def _clear_screen() -> None:
        sys.stdout.write(ANSI_CLEAR)
        sys.stdout.flush()

    @staticmethod
    def _safe_print(text: str) -> None:
        try:
            print(text)
        except BrokenPipeError:
            pass


def main() -> None:
    app = AutoRejoinRobloxApp()
    app.run()


if __name__ == "__main__":
    main()
