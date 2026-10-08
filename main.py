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
STARTUP_CONCURRENCY = 1
STARTUP_STABILIZATION_DELAY = 12.0
PID_MISSING_CONFIRMATIONS = 3
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

ROBLOX_ERROR_RE = re.compile(
    r"\b(264|266|267|268|270|273|275|277|279|280|286|403|524|600)\b"
)

# A bare number in logcat is not enough. The code must also appear in a
# Roblox/error/disconnect-related context before it becomes a candidate.
ROBLOX_ERROR_CONTEXT_RE = re.compile(
    r"(?:\b(?:roblox|error(?:\s*(?:code|id))?|code|disconnect(?:ed|ion)?|"
    r"kicked|kick)\b.{0,24}\b(?:264|266|267|268|270|273|275|277|279|280|286|403|524|600)\b"
    r"|\b(?:264|266|267|268|270|273|275|279|280|286|403|524|600)\b.{0,24}"
    r"\b(?:roblox|error(?:\s*(?:code|id))?|code|disconnect(?:ed|ion)?|kicked|kick)\b)",
    re.I,
)

HTTP_OR_STATUS_RE = re.compile(
    r"\b(?:http(?:/\d(?:\.\d)?)?|status(?:[_ ]code)?|response)\b"
    r".{0,18}\b(?:403|524)\b",
    re.I,
)

CRASH_PATTERNS = (
    re.compile(r"fatal exception", re.I),
    re.compile(r"fatal signal\s+\d+", re.I),
    re.compile(r"sigsegv", re.I),
    re.compile(r"sigabrt", re.I),
    re.compile(r"segmentation fault", re.I),
    re.compile(r"process .*\bhas died\b", re.I),
    re.compile(r"application not responding", re.I),
    re.compile(r"\banr\b", re.I),
    re.compile(r"out of memory", re.I),
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
                stdin=subprocess.DEVNULL,
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
        Stop the verified target package through Android Activity Manager.

        The PID is still used for strict target validation, but the actual
        termination is package-scoped via `am force-stop`.
        """
        if not self.verify_pid_for_package(pid, package):
            return False
        if not str(pid).isdigit() or pid <= 1:
            return False
        result = self.run_su(f"am force-stop {shlex.quote(package)}", timeout=5)
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
        self.last_known_pid: Optional[int] = None
        self.missing_pid_checks = 0
        self.process_loss_queued = False
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
            if self.pids:
                self.last_known_pid = self.pids[0]
                self.missing_pid_checks = 0
                self.process_loss_queued = False
                if self.status.startswith("PID Missing"):
                    self.status = "Running"

    def note_pid_missing(self) -> int:
        """Record one consecutive PID-missing check and return the count."""
        with self.lock:
            self.pids = []
            self.missing_pid_checks += 1
            return self.missing_pid_checks

    def queue_process_lost_recovery(self, reason: str) -> bool:
        """Queue recovery after a previously-running process is confirmed lost."""
        with self.lock:
            if not self.active or self.process_loss_queued:
                return False
            try:
                self.recovery_queue.put_nowait((self.last_known_pid or 0, reason))
            except queue.Full:
                return False
            self.process_loss_queued = True
            self.generation += 1
            self.online_since = None
            self.status = "Process Lost"
            return True

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
                self.missing_pid_checks = 0
                self.process_loss_queued = False

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
        # True while the selected clones are going through the initial
        # sequential boot + lobby + map + stabilization sequence. During this
        # window Logcat errors are treated as startup noise and must not cancel
        # the normal launch flow.
        self.startup_in_progress = False
        self.startup_run_id = 0

    def instances_snapshot(self) -> List[RobloxInstance]:
        with self.lock:
            return list(self.instances.values())

    def start_sessions(self, packages: List[str]) -> None:
        self.stop_sessions(kill=False, clear=True)
        with self.lock:
            self.startup_run_id += 1
            startup_run_id = self.startup_run_id
            self.startup_in_progress = True
            for package in packages:
                self.instances[package] = RobloxInstance(package)

        for instance in self.instances_snapshot():
            self._start_recovery_worker(instance)

        # Initial startup is deliberately FULLY SEQUENTIAL. Only the startup
        # coordinator runs in the background, so the dashboard/main thread
        # remains responsive while Android gets breathing room between clones.
        startup_thread = threading.Thread(
            target=self._sequential_startup_worker,
            args=(startup_run_id,),
            name="sequential-startup",
            daemon=True,
        )
        startup_thread.start()

    def _sequential_startup_worker(self, startup_run_id: int) -> None:
        """Launch selected Roblox instances one at a time.

        Each package completes its normal launch flow (including lobby delay
        and deep-link) before the next package is started. After the deep-link
        returns, wait a short stabilization period so the first instance can
        render the map and settle CPU/RAM usage before starting the next one.

        Logcat recovery is intentionally disarmed for this entire initial
        startup window. This prevents CPU/RAM loading noise from invalidating
        the launch generation and leaving a clone stuck in the lobby.
        """
        try:
            for instance in self.instances_snapshot():
                if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                    return

                self._launch_flow_entry(instance)

                if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                    return

                # _launch_flow_entry blocks until the package has either completed
                # its normal flow or failed. Only apply stabilization after a
                # successful online transition, i.e. after open_deep_link ran.
                snap = instance.snapshot()
                if snap["status"] == "Running" and snap["online_since"] is not None:
                    instance.add_event(
                        f"Startup stabilization {int(STARTUP_STABILIZATION_DELAY)}s"
                    )
                    deadline = time.monotonic() + STARTUP_STABILIZATION_DELAY
                    while True:
                        if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                            return
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self.shutdown_event.wait(min(0.25, remaining))
        finally:
            with self.lock:
                # A stale startup thread from an older session must never
                # disable the startup shield of a newer session.
                if self.startup_run_id == startup_run_id:
                    self.startup_in_progress = False

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
                instance.set_status("Rejoining")
                instance.add_event(f"Recovery: {reason}, PID {pid}")

                # Do not kill or force-stop the Roblox process.
                # Give the error screen a short moment to settle first.
                generation = instance.current_generation()
                rejoin_deadline = time.monotonic() + 25.0

                while True:
                    if (
                        self.shutdown_event.is_set()
                        or not instance.is_current(generation)
                    ):
                        return

                    remaining = rejoin_deadline - time.monotonic()
                    if remaining <= 0:
                        break

                    self.shutdown_event.wait(min(0.25, remaining))

                if (
                    not instance.is_current(generation)
                    or self.shutdown_event.is_set()
                ):
                    return

                cfg = self.config.load()

                instance.add_event(
                    f"Rejoining Place {cfg['place_id']}"
                )
                instance.set_status("Rejoining")

                # Recovery bypasses the normal launch flow.
                # Keep the Roblox process alive and send only the deep-link
                # intent to the target package.
                join_result = self.shell.open_deep_link(
                    instance.package,
                    cfg["place_id"],
                )

                if join_result.code != 0:
                    instance.add_event(
                        f"Deep link failed ({join_result.code})"
                    )
                    instance.set_status("Join Failed")
                    continue

                if (
                    not instance.is_current(generation)
                    or self.shutdown_event.is_set()
                ):
                    return

                instance.set_online()
                instance.add_event("Rejoin successful")

                # Preserve the existing queued-recovery handling.
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

            finally:
                instance.recovery_queue.task_done()

    def handle_sensor_event(self, pid: int, reason: str, raw_line: str) -> None:
        # During the initial multi-instance boot, Android/Zetsu can emit noisy
        # logcat lines while CPU/RAM usage is peaking. Do not let those lines
        # invalidate a normal launch flow. The detector itself remains running
        # and resumes as soon as the final startup stabilization completes.
        with self.lock:
            if self.startup_in_progress:
                return

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
        with self.lock:
            startup_in_progress = self.startup_in_progress

        for instance in self.instances_snapshot():
            snap = instance.snapshot()
            if not snap["active"]:
                continue

            try:
                pids = self.shell.pidof(instance.package)
            except Exception:
                pids = []

            if pids:
                instance.update_pids(pids)
            else:
                # During the initial sequential boot, PID gaps are expected
                # while Android/Zetsu creates and transitions processes. The
                # startup shield already suppresses logcat recovery; extend
                # the same protection to PID-loss detection.
                if startup_in_progress:
                    instance.update_pids([])
                elif snap["status"] == "Running" and snap["online_since"] is not None:
                    missing_count = instance.note_pid_missing()
                    if missing_count < PID_MISSING_CONFIRMATIONS:
                        instance.set_status(
                            f"PID Missing {missing_count}/{PID_MISSING_CONFIRMATIONS}"
                        )
                    else:
                        queued = instance.queue_process_lost_recovery("Process Lost")
                        if queued:
                            instance.add_event(
                                f"Process Lost: PID missing {PID_MISSING_CONFIRMATIONS} checks"
                            )

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
        # Candidate events are confirmed before recovery is triggered. This
        # filters transient/noisy logcat messages produced during normal
        # multi-instance operation. Keyed by exact PID + reason.
        self.candidate_lock = threading.RLock()
        self.candidates: Dict[Tuple[int, str], Tuple[float, int]] = {}
        self.candidate_window = 4.0
        self.candidate_max = 256

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
                stdin=subprocess.DEVNULL,
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
                if reason and self._confirm_candidate(pid, reason):
                    # Only confirmed events reach the manager. PID/package
                    # validation is still performed there as before.
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

        # Ignore HTTP/status-style 403/524 values. These are often ordinary
        # network diagnostics rather than an actual Roblox client error.
        error_match = ROBLOX_ERROR_RE.search(text)
        if error_match:
            code = error_match.group(1)
            if code in {"403", "524"} and HTTP_OR_STATUS_RE.search(text):
                return None

            # A matching number is only a candidate when nearby text explicitly
            # indicates Roblox/error/disconnect semantics. This prevents values
            # such as packet sizes, HTTP counters, IDs, or unrelated integers
            # from becoming recovery triggers.
            context = ROBLOX_ERROR_CONTEXT_RE.search(text)
            if context is None:
                return None

            if code == "277":
                return "Disconnect 277"
            return f"Error {code}"

        for pattern in CRASH_PATTERNS:
            if pattern.search(text):
                return f"Crash: {pattern.pattern}"

        return None

    def _confirm_candidate(self, pid: int, reason: str) -> bool:
        """Confirm noisy logcat candidates before starting package recovery.

        Explicit Roblox disconnect codes 267/277 are considered strong signals
        and may trigger immediately. Other error/crash candidates require two
        matching events from the same PID within a short window.
        """
        if reason in {"Error 267", "Disconnect 277"}:
            return True

        now = time.monotonic()
        key = (pid, reason)

        with self.candidate_lock:
            # Periodically prune stale entries to keep memory bounded during
            # long 24-hour sessions.
            stale_before = now - self.candidate_window
            stale_keys = [
                candidate_key
                for candidate_key, (timestamp, _count) in self.candidates.items()
                if timestamp < stale_before
            ]
            for candidate_key in stale_keys:
                self.candidates.pop(candidate_key, None)

            previous = self.candidates.get(key)
            if previous is None:
                self.candidates[key] = (now, 1)
                if len(self.candidates) > self.candidate_max:
                    oldest_key = min(
                        self.candidates,
                        key=lambda candidate_key: self.candidates[candidate_key][0],
                    )
                    self.candidates.pop(oldest_key, None)
                return False

            first_time, count = previous
            if now - first_time > self.candidate_window:
                self.candidates[key] = (now, 1)
                return False

            count += 1
            if count >= 2:
                self.candidates.pop(key, None)
                return True

            self.candidates[key] = (first_time, count)
            return False

    def stop_process(self) -> None:
        with self.process_lock:
            proc = self.process
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()  # SIGTERM only.
            except Exception:
                pass


class TerminalIO:
    """Centralized terminal-state handling for Termux/Android.

    The old implementation changed stdin into cbreak mode only inside the
    dashboard. On some Android terminal/floating-window combinations, an
    interrupted process could leave ECHO/ICANON altered, making the next
    input("") appear to be "dead". This class always restores a normal line
    terminal before menu input and again during shutdown.
    """

    _lock = threading.RLock()
    _saved_attrs = None

    @classmethod
    def _tty(cls):
        try:
            if sys.stdin.isatty():
                return sys.stdin
        except Exception:
            pass
        return None

    @classmethod
    def restore_normal(cls) -> None:
        """Force normal line input with visible typed characters."""
        tty_stream = cls._tty()
        if tty_stream is None:
            return
        with cls._lock:
            try:
                attrs = termios.tcgetattr(tty_stream)
                # Explicitly restore the flags most important for input("").
                attrs[3] |= termios.ICANON | termios.ECHO | termios.ISIG | termios.IEXTEN
                attrs[6][termios.VMIN] = 1
                attrs[6][termios.VTIME] = 0
                termios.tcsetattr(tty_stream, termios.TCSADRAIN, attrs)
            except Exception:
                pass

    @classmethod
    def enter_cbreak(cls) -> bool:
        """Enable single-key dashboard input while keeping ECHO enabled."""
        tty_stream = cls._tty()
        if tty_stream is None:
            return False
        with cls._lock:
            try:
                cls._saved_attrs = termios.tcgetattr(tty_stream)
                attrs = termios.tcgetattr(tty_stream)
                attrs[3] &= ~(termios.ICANON)
                attrs[3] |= termios.ECHO | termios.ISIG | termios.IEXTEN
                attrs[6][termios.VMIN] = 0
                attrs[6][termios.VTIME] = 1
                termios.tcsetattr(tty_stream, termios.TCSADRAIN, attrs)
                return True
            except Exception:
                cls._saved_attrs = None
                return False

    @classmethod
    def leave_cbreak(cls) -> None:
        tty_stream = cls._tty()
        if tty_stream is None:
            return
        with cls._lock:
            try:
                if cls._saved_attrs is not None:
                    termios.tcsetattr(tty_stream, termios.TCSADRAIN, cls._saved_attrs)
            except Exception:
                pass
            finally:
                cls._saved_attrs = None
                cls.restore_normal()

    @classmethod
    def safe_line_input(cls, prompt: str) -> str:
        """Restore normal echo before every blocking input("") call."""
        cls.restore_normal()
        try:
            return input(prompt)
        finally:
            cls.restore_normal()

    @staticmethod
    def terminal_columns(default: int = 60) -> int:
        try:
            columns = shutil.get_terminal_size((default, 24)).columns
            if columns and columns > 0:
                return int(columns)
        except Exception:
            pass
        return default

    @classmethod
    def clear(cls) -> None:
        """Clear using the terminal's own clear command when possible."""
        cls.restore_normal()
        try:
            # TERM may be unset in some rooted/floating Termux windows.
            if os.environ.get("TERM"):
                subprocess.run(
                    ["clear"],
                    stdin=subprocess.DEVNULL,
                    stdout=sys.stdout,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                return
        except Exception:
            pass
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


class Dashboard:
    """Adaptive dashboard designed for Termux portrait and landscape modes."""

    def __init__(self, manager: RobloxManager, shutdown_event: threading.Event):
        self.manager = manager
        self.shutdown_event = shutdown_event
        self.show_logs = False

    @staticmethod
    def _fit(text: str, width: int) -> str:
        text = str(text).replace("\t", " ")
        if width <= 0:
            return ""
        if len(text) > width:
            if width == 1:
                return text[:1]
            return text[:width - 1] + "~"
        return text.ljust(width)

    @staticmethod
    def _uptime(online_since: Optional[float]) -> str:
        if online_since is None:
            return "--:--:--"
        seconds = max(0, int(time.monotonic() - online_since))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _width() -> int:
        cols = TerminalIO.terminal_columns(60)
        # Never build a giant box. It looks much cleaner in Android floating
        # windows and avoids accidental wrapping from bad COLUMNS values.
        return max(34, min(cols - 1, 78))

    def _draw_wide(self, width: int, rows: List[dict], cfg: dict) -> List[str]:
        inner = width - 2
        # At >=60 columns we can keep a real table.
        num_w = 2
        pid_w = 7
        online_w = 8
        status_w = 13
        package_w = max(12, inner - (num_w + pid_w + online_w + status_w + 8))

        def row_text(index: int, snap: dict) -> str:
            pids = snap["pids"]
            pid_text = str(pids[0]) if pids else "-"
            if len(pids) > 1:
                pid_text += f" +{len(pids)-1}"
            parts = [
                self._fit(index, num_w),
                self._fit(snap["package"], package_w),
                self._fit(pid_text, pid_w),
                self._fit(self._uptime(snap["online_since"]), online_w),
                self._fit(snap["status"], status_w),
            ]
            return " ".join(parts)

        header = " ".join([
            self._fit("#", num_w),
            self._fit("Package", package_w),
            self._fit("PID", pid_w),
            self._fit("Online", online_w),
            self._fit("Status", status_w),
        ])

        lines = [
            f"+{'=' * inner}+",
            f"|{self._fit(APP_NAME, inner)}|",
            f"|{self._fit('Place ID: '+str(cfg['place_id'])+'  |  Lobby: '+str(cfg['lobby_delay'])+'s', inner)}|",
            f"+{'-' * inner}+",
            f"|{self._fit(header, inner)}|",
            f"+{'-' * inner}+",
        ]
        if rows:
            for index, snap in enumerate(rows, start=1):
                lines.append(f"|{self._fit(row_text(index, snap), inner)}|")
        else:
            lines.append(f"|{self._fit('No active session.', inner)}|")
        return lines

    def _draw_compact(self, width: int, rows: List[dict], cfg: dict) -> List[str]:
        """Portrait mode: two short lines per clone, no horizontal wrapping."""
        inner = width - 2
        lines = [
            f"+{'=' * inner}+",
            f"|{self._fit(APP_NAME, inner)}|",
            f"|{self._fit('Place:'+str(cfg['place_id'])+'  Lobby:'+str(cfg['lobby_delay'])+'s', inner)}|",
            f"+{'-' * inner}+",
        ]
        if rows:
            for index, snap in enumerate(rows, start=1):
                pids = snap["pids"]
                pid_text = str(pids[0]) if pids else "-"
                if len(pids) > 1:
                    pid_text += f"+{len(pids)-1}"
                package = snap["package"]
                line1 = f"{index}. {package}"
                line2 = f"PID:{pid_text}  ON:{self._uptime(snap['online_since'])}"
                line2 += f"  {snap['status']}"
                lines.append(f"|{self._fit(line1, inner)}|")
                lines.append(f"|{self._fit(line2, inner)}|")
                lines.append(f"+{'-' * inner}+")
        else:
            lines.append(f"|{self._fit('No active session.', inner)}|")
            lines.append(f"+{'-' * inner}+")
        return lines

    def _draw(self) -> None:
        width = self._width()
        cfg = self.manager.config.load()
        snapshots = [i.snapshot() for i in self.manager.instances_snapshot()]

        if width >= 60:
            lines = self._draw_wide(width, snapshots, cfg)
        else:
            lines = self._draw_compact(width, snapshots, cfg)

        inner = width - 2
        lines.extend([
            f"|{self._fit('Keys: [q] Quit  [s] Stop  [r] Refresh  [l] Logs', inner)}|",
            f"+{'=' * inner}+",
        ])

        if self.show_logs:
            lines.append("Events:")
            for snap in snapshots:
                for event in snap["events"][-2:]:
                    lines.append(self._fit(f"{snap['package']}: {event}", width))

        # One write reduces flicker and prevents background output from
        # splitting the dashboard into multiple chunks.
        sys.stdout.write("\033[2J\033[H" + "\n".join(lines) + "\n")
        sys.stdout.flush()

    def run(self) -> str:
        """Return 'menu' or 'quit'."""
        raw_mode = TerminalIO.enter_cbreak()
        try:
            while not self.shutdown_event.is_set():
                self._draw()
                if raw_mode:
                    try:
                        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                    except (OSError, ValueError):
                        ready = []
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
                        elif key == "l":
                            self.show_logs = not self.show_logs
                else:
                    # Non-TTY mode: stay headless and do not attempt terminal input.
                    self.shutdown_event.wait(1.0)
        finally:
            TerminalIO.leave_cbreak()
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
            TerminalIO.restore_normal()
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
                input("")
            except EOFError:
                pass
        finally:
            self.shutdown()

    def _start_background_monitors(self) -> None:
        if not self.pid_monitor.is_alive():
            self.pid_monitor.start()
        if not self.logcat_sensor.is_alive():
            self.logcat_sensor.start()

    def _box(self, lines: List[str], title: Optional[str] = None) -> None:
        """Render a compact left-aligned box that survives portrait mode."""
        cols = TerminalIO.terminal_columns(60)
        width = max(34, min(cols - 1, 78))
        inner = width - 2
        output = [f"+{'=' * inner}+"]
        if title:
            output.append(f"|{Dashboard._fit(title, inner)}|")
            output.append(f"+{'-' * inner}+")
        for line in lines:
            output.append(f"|{Dashboard._fit(line, inner)}|")
        output.append(f"+{'=' * inner}+")
        sys.stdout.write("\n".join(output) + "\n")
        sys.stdout.flush()

    def _main_menu(self) -> None:
        while not self.shutdown_event.is_set():
            TerminalIO.clear()
            cfg = self.config.load()
            self._box(
                [
                    f"Packages terdeteksi : {len(self.packages)}",
                    f"Lobby delay        : {cfg['lobby_delay']} detik",
                    f"Place ID           : {cfg['place_id']}",
                    "",
                    "[1] Mulai / Start AFK",
                    "[2] Setting",
                    "[3] Keluar / Exit",
                ],
                APP_NAME,
            )

            try:
                choice = TerminalIO.safe_line_input("Pilih: ").strip()
            except (EOFError, KeyboardInterrupt):
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
        TerminalIO.clear()

        if not self.packages:
            self._box(["Tidak ada package Roblox yang terdeteksi.", "Tekan Enter untuk kembali."], "PILIH PACKAGE")
            try:
                input("")
            except (EOFError, KeyboardInterrupt):
                pass
            return

        cols = TerminalIO.terminal_columns(60)
        width = max(34, min(cols - 1, 78))
        inner = width - 2
        lines = []
        for idx, package in enumerate(self.packages, start=1):
            # Always keep the selection number visible even when portrait mode
            # is extremely narrow.
            lines.append(f"{idx:>2}. {package}")
        lines.extend(["", "Contoh: 1,2,4"])
        self._box(lines, "PILIH ROBLOX PACKAGE")

        try:
            raw = TerminalIO.safe_line_input("Pilih package: ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        selected = self._parse_selection(raw, len(self.packages))
        if not selected:
            self._safe_print("Tidak ada pilihan yang valid.")
            time.sleep(1.2)
            return

        selected_packages = [self.packages[i - 1] for i in selected]
        self.manager.start_sessions(selected_packages)
        dashboard = Dashboard(self.manager, self.shutdown_event)
        dashboard.run()

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
            TerminalIO.clear()
            self._box(
                [
                    f"1. Lobby delay : {cfg['lobby_delay']} detik",
                    f"2. Place ID    : {cfg['place_id']}",
                    "",
                    "0. Kembali",
                ],
                "SETTING",
            )

            try:
                choice = TerminalIO.safe_line_input("Pilih: ").strip()
            except (EOFError, KeyboardInterrupt):
                return

            if choice == "0":
                return
            if choice == "1":
                try:
                    value = int(TerminalIO.safe_line_input("Lobby delay (detik): ").strip())
                    if value < 0:
                        raise ValueError
                    self.config.update(value, cfg["place_id"])
                    self._safe_print("Lobby delay disimpan.")
                except (ValueError, KeyboardInterrupt, EOFError):
                    self._safe_print("Nilai lobby delay harus angka >= 0.")
                time.sleep(0.8)
            elif choice == "2":
                try:
                    place_id = TerminalIO.safe_line_input("Place ID: ").strip()
                except (KeyboardInterrupt, EOFError):
                    return
                if not place_id.isdigit():
                    self._safe_print("Place ID harus berupa angka.")
                else:
                    self.config.update(cfg["lobby_delay"], place_id)
                    self._safe_print("Place ID disimpan.")
                time.sleep(0.8)
            else:
                self._safe_print("Pilihan tidak valid.")
                time.sleep(0.8)

    def shutdown(self) -> None:
        self.shutdown_event.set()
        TerminalIO.leave_cbreak()
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
        TerminalIO.clear()

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
