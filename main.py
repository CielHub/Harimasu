#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Auto Rejoin Roblox
------------------
Android 10 + Termux + Magisk/KernelSU

Features:
- Automatic scan of installed Roblox clone packages.
- Adaptive Termux dashboard and sequential initial startup.
- Per-package PID, WindowManager, ActivityManager and Logcat health signals.
- Separate in-game rejoin and full relaunch recovery paths.
- Bounded per-package relaunch retries; recovery never uses SIGKILL.
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
from urllib.parse import parse_qs, urlencode, urlparse
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple


APP_NAME = "Auto Rejoin Roblox"
STARTUP_CONCURRENCY = 1
STARTUP_STABILIZATION_DELAY = 20.0
PID_MISSING_CONFIRMATIONS = 3
WINDOW_MISSING_CONFIRMATIONS = 3
WINDOW_SCAN_INTERVAL = 2.0
ACTIVITY_SCAN_INTERVAL = 8.0
ACTIVITY_PROBLEM_CONFIRMATIONS = 2
FULL_RELAUNCH_MAX_ATTEMPTS = 3
FULL_RELAUNCH_RETRY_BASE = 5.0
WINDOW_RESTORE_TIMEOUT = 12.0
PROCESS_LOST_RETRY_COOLDOWN = 30.0
IN_GAME_REJOIN_MAX_ATTEMPTS = 3
IN_GAME_REJOIN_RETRY_DELAY = 10.0
IN_GAME_REJOIN_CYCLE_COOLDOWN = 60.0
STARTUP_JOIN_RETRY_DELAY = 5.0
BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "lobby_delay": 30,
    "place_id": "123456789",
    "join_mode": "place_id",
    "private_server_link": "",
    "periodic_rejoin_enabled": True,
    "periodic_rejoin_interval_hours": 4,
    "periodic_rejoin_stagger_seconds": 45,
    "periodic_rejoin_verify_seconds": 15,
    "periodic_rejoin_retry_seconds": 60,
    "periodic_rejoin_max_retries": 2,
}


def private_server_deep_link(link: str) -> str:
    """Convert a supported Roblox private-server URL into an app deep link.

    Supported formats:
    - https://www.roblox.com/share?code=...&type=Server
    - roblox://navigation/share_links?code=...&type=Server
    - Legacy Roblox game URLs with a privateServerLinkCode/linkCode.

    Unknown formats are rejected rather than silently falling back to a public place.
    """
    raw = str(link or "").strip().replace("&amp;", "&")
    if not raw or "\n" in raw or "\r" in raw:
        raise ValueError("Link private server kosong atau tidak valid.")

    parsed = urlparse(raw)
    query = parse_qs(parsed.query, keep_blank_values=False)

    def one(name: str) -> str:
        values = query.get(name, [])
        return values[0].strip() if values else ""

    def valid_code(code: str) -> bool:
        return bool(code and len(code) <= 512 and re.fullmatch(r"[A-Za-z0-9_-]+", code))

    # Roblox app deep link for the modern share-link resolver.
    if (
        parsed.scheme.lower() == "roblox"
        and parsed.netloc.lower() == "navigation"
        and parsed.path.strip("/").lower() == "share_links"
    ):
        code = one("code")
        link_type = one("type")
        if not valid_code(code) or link_type.lower() != "server":
            raise ValueError("Deep link Roblox harus berisi code dan type=Server.")
        return "roblox://navigation/share_links?" + urlencode({"code": code, "type": "Server"})

    # Web URLs must be HTTPS on the actual Roblox domain.
    host = (parsed.hostname or "").lower().rstrip(".")
    is_roblox_host = host == "roblox.com" or host.endswith(".roblox.com")
    if parsed.scheme.lower() != "https" or not is_roblox_host:
        raise ValueError("Gunakan link HTTPS dari domain roblox.com atau deep link Roblox yang didukung.")

    path = parsed.path.rstrip("/").lower()
    if path == "/share":
        code = one("code")
        link_type = one("type")
        if not valid_code(code) or link_type.lower() != "server":
            raise ValueError("Link share harus memuat code=... dan type=Server.")
        return "roblox://navigation/share_links?" + urlencode({"code": code, "type": "Server"})

    # Older invite links commonly look like /games/<placeId>/...?...privateServerLinkCode=...
    match = re.match(r"^/games/(\d+)(?:/|$)", parsed.path, re.I)
    if match:
        place_id = match.group(1)
        link_code = one("privateServerLinkCode") or one("linkCode")
        access_code = one("accessCode")
        if valid_code(link_code):
            return "roblox://placeId=" + place_id + "&" + urlencode({"linkCode": link_code})
        if valid_code(access_code):
            return "roblox://placeId=" + place_id + "&" + urlencode({"accessCode": access_code})

    raise ValueError("Format link private server tidak dikenali. Tempel link Server dari Roblox.")


def join_target_label(cfg: dict) -> str:
    """Return a display-safe target label without exposing the invite code."""
    if cfg.get("join_mode") == "private_server_link":
        return "Private Server Link"
    return "Place ID " + str(cfg.get("place_id", DEFAULT_CONFIG["place_id"]))


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

# Specific Roblox popup signature shown by the user: "Connection Failed"
# together with "Error Code: 279". Requiring both the connection wording and
# code 279 avoids treating an unrelated standalone number as this error.
CONNECTION_FAILED_279_RE = re.compile(
    r"(?:connection\s+(?:failed|failure|lost)|connection(?:failed|failure|lost))"
    r".{0,100}\b(?:error\s*(?:code|id)\s*[:#= -]?\s*)?279\b"
    r"|\b(?:error\s*(?:code|id)\s*[:#= -]?\s*)?279\b"
    r".{0,100}(?:connection\s+(?:failed|failure|lost)|connection(?:failed|failure|lost))",
    re.I,
)

# A bare number or a generic tag named "Roblox" is not enough. The code must
# be near explicit error/code/disconnect/kick language to become a candidate.
ROBLOX_ERROR_CONTEXT_RE = re.compile(
    r"(?:\b(?:error(?:\s*(?:code|id))?|code|disconnect(?:ed|ion)?|"
    r"kicked|kick)\b.{0,24}\b(?:264|266|267|268|270|273|275|277|279|280|286|403|524|600)\b"
    r"|\b(?:264|266|267|268|270|273|275|277|279|280|286|403|524|600)\b.{0,24}"
    r"\b(?:error(?:\s*(?:code|id))?|code|disconnect(?:ed|ion)?|kicked|kick)\b)",
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
    re.compile(r"input dispatching timed out", re.I),
    re.compile(r"\banr(?:\s+in)?\b", re.I),
    re.compile(r"\boutofmemoryerror\b", re.I),
    re.compile(r"out\s*of\s*memory", re.I),
    re.compile(r"(?:lowmemorykiller|\blmkd\b).{0,100}\b(?:kill|killed|killing|oom)\b", re.I),
    re.compile(r"\b(?:killed|killing)\b.{0,100}\b(?:memory pressure|out of memory|oom)\b", re.I),
)

WINDOW_ENTRY_RE = re.compile(
    r"(?ms)^\s*Window #\d+\s+Window\{.*?(?=^\s*Window #\d+\s+Window\{|\Z)"
)
ACTIVITY_PROCESS_HEADER_RE = re.compile(r"^\s*(?:\*APP\*|ProcessRecord\{)", re.I)
ACTIVITY_BAD_STATE_RE = re.compile(
    r"(?:\bnotResponding\s*=\s*true\b|\bcrashing\s*=\s*true\b|"
    r"\bappNotResponding\b|input dispatching timed out|application not responding|"
    r"\bANR in\b)",
    re.I,
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

    @staticmethod
    def _normalize(data: dict) -> dict:
        if not isinstance(data, dict):
            data = {}

        try:
            lobby_delay = max(0, min(3600, int(data.get("lobby_delay", DEFAULT_CONFIG["lobby_delay"]))))
        except (TypeError, ValueError):
            lobby_delay = DEFAULT_CONFIG["lobby_delay"]

        place_id = str(data.get("place_id", DEFAULT_CONFIG["place_id"])).strip()
        if not place_id.isdigit():
            place_id = DEFAULT_CONFIG["place_id"]

        private_server_link = str(data.get("private_server_link", "") or "").strip()
        if private_server_link:
            try:
                private_server_deep_link(private_server_link)
            except ValueError:
                private_server_link = ""

        join_mode = data.get("join_mode", DEFAULT_CONFIG["join_mode"])
        if join_mode not in ("place_id", "private_server_link"):
            join_mode = "place_id"
        if join_mode == "private_server_link" and not private_server_link:
            join_mode = "place_id"

        enabled = data.get("periodic_rejoin_enabled", DEFAULT_CONFIG["periodic_rejoin_enabled"])
        if not isinstance(enabled, bool):
            enabled = DEFAULT_CONFIG["periodic_rejoin_enabled"]

        def bounded_int(key: str, minimum: int, maximum: int) -> int:
            try:
                value = int(data.get(key, DEFAULT_CONFIG[key]))
            except (TypeError, ValueError):
                value = DEFAULT_CONFIG[key]
            return max(minimum, min(maximum, value))

        return {
            "lobby_delay": lobby_delay,
            "place_id": place_id,
            "join_mode": join_mode,
            "private_server_link": private_server_link,
            "periodic_rejoin_enabled": enabled,
            "periodic_rejoin_interval_hours": bounded_int("periodic_rejoin_interval_hours", 1, 168),
            "periodic_rejoin_stagger_seconds": bounded_int("periodic_rejoin_stagger_seconds", 5, 3600),
            "periodic_rejoin_verify_seconds": bounded_int("periodic_rejoin_verify_seconds", 5, 60),
            "periodic_rejoin_retry_seconds": bounded_int("periodic_rejoin_retry_seconds", 10, 3600),
            "periodic_rejoin_max_retries": bounded_int("periodic_rejoin_max_retries", 0, 5),
        }

    def load(self) -> dict:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = DEFAULT_CONFIG.copy()
                self._atomic_write(data)
            normalized = self._normalize(data)
            if normalized != data:
                self._atomic_write(normalized)
            return normalized

    def update(
        self,
        lobby_delay: Optional[int] = None,
        place_id: Optional[str] = None,
        **updates,
    ) -> dict:
        """Update selected settings while preserving all unrelated config keys."""
        allowed = set(DEFAULT_CONFIG) - {"lobby_delay", "place_id"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unknown config setting(s): {', '.join(sorted(unknown))}")

        with self.lock:
            current = self.load()
            if lobby_delay is not None:
                try:
                    current["lobby_delay"] = max(0, min(3600, int(lobby_delay)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Lobby delay harus angka antara 0 sampai 3600 detik.") from exc
            if place_id is not None:
                normalized_place_id = str(place_id).strip()
                if not normalized_place_id.isdigit():
                    raise ValueError("Place ID harus berupa angka.")
                current["place_id"] = normalized_place_id

            next_link = str(updates.get("private_server_link", current.get("private_server_link", "")) or "").strip()
            next_mode = updates.get("join_mode", current.get("join_mode", "place_id"))
            if next_link:
                private_server_deep_link(next_link)
            if next_mode not in ("place_id", "private_server_link"):
                raise ValueError("Mode join tidak dikenal.")
            if next_mode == "private_server_link" and not next_link:
                raise ValueError("Masukkan link private server sebelum mengaktifkan mode ini.")

            current.update(updates)
            normalized = self._normalize(current)
            self._atomic_write(normalized)
            return normalized

    def _atomic_write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)


class AndroidShell:
    """Root command wrapper for package-scoped Android inspection and launch."""

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

    def window_packages(self, tracked_packages: Optional[List[str]] = None) -> Optional[set[str]]:
        """Return tracked packages with a visible WindowManager surface.

        None means the output could not be interpreted reliably. It is safer
        to skip one health sample than to mistake an Android-version format
        change for every Roblox window having disappeared.
        """
        result = self.run_su("dumpsys window windows", timeout=6)
        if result.code != 0 or not result.stdout:
            return None

        tracked = set(tracked_packages or [])
        if not tracked:
            return set()

        output = result.stdout
        blocks = WINDOW_ENTRY_RE.findall(output)
        visibility_fields_present = bool(re.search(
            r"\b(?:mHasSurface|isOnScreen|isVisible|mViewVisibility|isReadyForDisplay)\s*(?:\(\))?\s*=",
            output,
            re.I,
        ))
        if not visibility_fields_present:
            return None

        if not blocks:
            # Unknown format: don't translate parser failure into "all windows
            # are missing". That would cause a mass-relaunch on some OEM builds.
            return None

        found: set[str] = set()
        for block in blocks:
            packages_in_block = [
                package for package in tracked
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(package)}(?![A-Za-z0-9_])",
                    block,
                )
            ]
            if not packages_in_block:
                continue

            explicitly_hidden = bool(re.search(
                r"(?:\bisOnScreen\s*=\s*false\b|\bisVisible\s*=\s*false\b|"
                r"\bmViewVisibility\s*=\s*(?:0x8|8|GONE|INVISIBLE)\b|"
                r"\bisReadyForDisplay\(\)\s*=\s*false\b)",
                block,
                re.I,
            ))
            has_surface = bool(re.search(r"\bmHasSurface\s*=\s*true\b", block, re.I))
            visibly_present = bool(re.search(
                r"(?:\bisOnScreen\s*=\s*true\b|\bisVisible\s*=\s*true\b|"
                r"\bmViewVisibility\s*=\s*(?:0x0|0|VISIBLE)\b|"
                r"\bisReadyForDisplay\(\)\s*=\s*true\b)",
                block,
                re.I,
            ))
            # mHasSurface=true alone can describe a stale/off-screen surface.
            # Accept it only when WindowManager has no explicit hidden marker.
            if not explicitly_hidden and (visibly_present or has_surface):
                found.update(packages_in_block)

        return found

    def activity_problem_packages(self, tracked_packages: Optional[List[str]] = None) -> Optional[Dict[str, str]]:
        """Return packages explicitly marked crashing/not-responding by ActivityManager.

        The parser only reports positive failure flags associated with the same
        process record. Missing or unfamiliar dump structure returns None, not
        an empty set, so an unparseable dump cannot cause mass recovery.
        """
        result = self.run_su("dumpsys activity processes", timeout=8)
        if result.code != 0 or not result.stdout:
            return None

        tracked = set(tracked_packages or [])
        if not tracked:
            return {}

        lines = result.stdout.splitlines()
        header_indexes = [
            index for index, line in enumerate(lines)
            if ACTIVITY_PROCESS_HEADER_RE.search(line)
        ]
        if not header_indexes:
            return None

        problems: Dict[str, str] = {}
        for pos, start in enumerate(header_indexes):
            end = header_indexes[pos + 1] if pos + 1 < len(header_indexes) else len(lines)
            block = "\n".join(lines[start:end])
            matched_packages = [
                package for package in tracked
                if re.search(
                    rf"(?<![A-Za-z0-9_]){re.escape(package)}(?![A-Za-z0-9_])",
                    block,
                )
            ]
            if not matched_packages:
                continue
            bad_match = ACTIVITY_BAD_STATE_RE.search(block)
            if bad_match:
                signal = re.sub(r"\s+", " ", bad_match.group(0)).strip()
                for package in matched_packages:
                    problems[package] = signal
        return problems

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
        """Open the existing public Place ID target (backward-compatible helper)."""
        pkg = shlex.quote(package)
        uri = shlex.quote(f"roblox://placeId={place_id}")
        command = f"am start -a android.intent.action.VIEW -d {uri} {pkg}"
        return self.run_su(command, timeout=20)

    def open_join_target(self, package: str, cfg: dict) -> CommandResult:
        """Open the configured Place ID or private-server invitation for one clone."""
        mode = cfg.get("join_mode", "place_id")
        if mode == "private_server_link":
            try:
                target_uri = private_server_deep_link(cfg.get("private_server_link", ""))
            except ValueError as exc:
                return CommandResult(2, "", str(exc))
        else:
            place_id = str(cfg.get("place_id", "")).strip()
            if not place_id.isdigit():
                return CommandResult(2, "", "Place ID tidak valid.")
            target_uri = f"roblox://placeId={place_id}"

        # Quote both arguments so URL query separators cannot be interpreted by the shell.
        pkg = shlex.quote(package)
        uri = shlex.quote(target_uri)
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
        # Serializes start/deep-link operations for this package only. It never
        # locks sibling clones, and scheduled rejoin must not overlap a full relaunch.
        self.operation_lock = threading.RLock()
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
        self.last_known_pid_at = 0.0
        self.has_reached_running = False
        self.window_seen = False
        self.last_window_scan_serial = 0
        self.last_activity_scan_serial = 0
        self.missing_pid_checks = 0
        self.window_missing_checks = 0
        self.activity_problem_checks = 0
        self.activity_problem_reason = ""
        self.process_loss_queued = False
        self.process_loss_retry_after = 0.0
        # Scheduled-rejoin timing uses monotonic timestamps; these are runtime
        # values and intentionally are not persisted across app restarts.
        self.last_successful_join_at: Optional[float] = None
        self.periodic_schedule_anchor_at: Optional[float] = None
        self.next_periodic_rejoin_at: Optional[float] = None
        self.periodic_interval_seconds: Optional[int] = None
        self.periodic_schedule_enabled = False
        self.periodic_rejoin_in_progress = False
        self.periodic_rejoin_generation: Optional[int] = None
        self.periodic_rejoin_attempts = 0
        self.periodic_rejoin_retry_at: Optional[float] = None
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
                "has_reached_running": self.has_reached_running,
                "window_seen": self.window_seen,
                "process_loss_queued": self.process_loss_queued,
                "missing_pid_checks": self.missing_pid_checks,
                "window_missing_checks": self.window_missing_checks,
                "activity_problem_checks": self.activity_problem_checks,
                "last_successful_join_at": self.last_successful_join_at,
                "next_periodic_rejoin_at": self.next_periodic_rejoin_at,
                "periodic_rejoin_in_progress": self.periodic_rejoin_in_progress,
                "periodic_rejoin_attempts": self.periodic_rejoin_attempts,
                "periodic_rejoin_retry_at": self.periodic_rejoin_retry_at,
            }

    def update_pids(self, pids: List[int]) -> None:
        with self.lock:
            self.pids = list(pids)
            if self.pids:
                self.last_known_pid = self.pids[0]
                self.last_known_pid_at = time.monotonic()
                self.missing_pid_checks = 0
                # Do not release a full-relaunch latch just because the old PID
                # is still alive (especially after Window Lost). Only set_online
                # or an exhausted-retry path may release it.
                if self.status.startswith("PID Missing") and self.online_since is not None and not self.process_loss_queued:
                    self.status = "Running"

    def note_pid_missing(self) -> int:
        """Record one consecutive PID-missing check and return the count."""
        with self.lock:
            self.pids = []
            self.missing_pid_checks += 1
            return self.missing_pid_checks

    def note_window_missing(self, scan_serial: Optional[int] = None) -> int:
        """Count one fresh WindowManager sample, never a repeated cached result."""
        with self.lock:
            if scan_serial is not None:
                if scan_serial <= self.last_window_scan_serial:
                    return self.window_missing_checks
                self.last_window_scan_serial = scan_serial
            self.window_missing_checks += 1
            return self.window_missing_checks

    def note_window_present(self, scan_serial: Optional[int] = None) -> None:
        """Record a package window and clear the consecutive-missing streak."""
        with self.lock:
            if scan_serial is not None:
                if scan_serial < self.last_window_scan_serial:
                    return
                self.last_window_scan_serial = max(self.last_window_scan_serial, scan_serial)
            self.window_seen = True
            self.window_missing_checks = 0
            if self.status.startswith("Window Missing") and self.online_since is not None and not self.process_loss_queued:
                self.status = "Running"

    def note_activity_problem(self, reason: str, scan_serial: Optional[int] = None) -> int:
        with self.lock:
            if scan_serial is not None:
                if scan_serial <= self.last_activity_scan_serial:
                    return self.activity_problem_checks
                self.last_activity_scan_serial = scan_serial
            if self.activity_problem_reason == reason:
                self.activity_problem_checks += 1
            else:
                self.activity_problem_reason = reason
                self.activity_problem_checks = 1
            return self.activity_problem_checks

    def note_activity_healthy(self, scan_serial: Optional[int] = None) -> None:
        with self.lock:
            if scan_serial is not None:
                if scan_serial <= self.last_activity_scan_serial:
                    return
                self.last_activity_scan_serial = scan_serial
            self.activity_problem_checks = 0
            self.activity_problem_reason = ""
            if self.status.startswith("Activity Unhealthy") and self.online_since is not None and not self.process_loss_queued:
                self.status = "Running"

    def trigger_process_lost(self):
        """Latch a per-instance full relaunch and return its new generation."""
        with self.lock:
            if not self.active or self.process_loss_queued:
                return None
            if time.monotonic() < self.process_loss_retry_after:
                return None

            self.generation += 1
            self.online_since = None
            self.status = "Process Lost"
            self.process_loss_queued = True
            self.missing_pid_checks = 0
            self.window_missing_checks = 0
            self.activity_problem_checks = 0
            self.activity_problem_reason = ""
            return self.generation

    def finish_process_loss_failure(self, generation: int, cooldown: float = PROCESS_LOST_RETRY_COOLDOWN) -> bool:
        """Release the relaunch latch after failed attempts and arm a cooldown."""
        with self.lock:
            if not self.active or self.generation != generation:
                return False
            self.process_loss_queued = False
            self.online_since = None
            self.status = "Recovery Failed"
            self.missing_pid_checks = 0
            self.window_missing_checks = 0
            self.activity_problem_checks = 0
            self.activity_problem_reason = ""
            self.process_loss_retry_after = time.monotonic() + max(0.0, cooldown)
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

    def set_online(
        self,
        periodic_interval_seconds: Optional[int] = None,
        periodic_enabled: bool = False,
    ) -> None:
        """Mark a successful join and anchor the next scheduled rejoin."""
        now = time.monotonic()
        with self.lock:
            if not self.active:
                return
            self.online_since = now
            self.status = "Running"
            self.has_reached_running = True
            self.missing_pid_checks = 0
            self.window_missing_checks = 0
            self.activity_problem_checks = 0
            self.activity_problem_reason = ""
            self.process_loss_queued = False
            self.process_loss_retry_after = 0.0

            self.last_successful_join_at = now
            self.periodic_schedule_anchor_at = now
            self.periodic_schedule_enabled = bool(periodic_enabled and periodic_interval_seconds)
            self.periodic_interval_seconds = (
                max(1, int(periodic_interval_seconds))
                if self.periodic_schedule_enabled else None
            )
            self.next_periodic_rejoin_at = (
                now + self.periodic_interval_seconds
                if self.periodic_schedule_enabled and self.periodic_interval_seconds is not None
                else None
            )
            self.periodic_rejoin_in_progress = False
            self.periodic_rejoin_generation = None
            self.periodic_rejoin_attempts = 0
            self.periodic_rejoin_retry_at = None

    def sync_periodic_schedule(
        self,
        enabled: bool,
        interval_seconds: int,
        now: Optional[float] = None,
    ) -> None:
        """Apply live config changes without resetting a successful-join clock."""
        if now is None:
            now = time.monotonic()
        interval_seconds = max(1, int(interval_seconds))
        with self.lock:
            if not self.active or self.periodic_rejoin_in_progress:
                return

            if not enabled:
                self.periodic_schedule_enabled = False
                self.periodic_interval_seconds = interval_seconds
                self.next_periodic_rejoin_at = None
                self.periodic_rejoin_retry_at = None
                self.periodic_rejoin_attempts = 0
                return

            if self.last_successful_join_at is None:
                # Instances that have not joined successfully yet get their
                # first timer from the eventual set_online() call.
                return

            if not self.periodic_schedule_enabled:
                # Re-enabling the feature starts a fresh full interval instead
                # of surprising the user with an immediate catch-up rejoin.
                self.periodic_schedule_enabled = True
                self.periodic_interval_seconds = interval_seconds
                self.periodic_schedule_anchor_at = now
                self.next_periodic_rejoin_at = now + interval_seconds
                self.periodic_rejoin_attempts = 0
                self.periodic_rejoin_retry_at = None
                return

            if self.periodic_interval_seconds != interval_seconds:
                self.periodic_interval_seconds = interval_seconds
                anchor = self.periodic_schedule_anchor_at or self.last_successful_join_at
                self.next_periodic_rejoin_at = anchor + interval_seconds

    def begin_periodic_rejoin(self, now: Optional[float] = None) -> Optional[int]:
        """Atomically claim a due scheduled rejoin for this package."""
        if now is None:
            now = time.monotonic()
        with self.lock:
            if not self.active or not self.has_reached_running:
                return None
            if self.online_since is None or not self.pids:
                return None
            if self.process_loss_queued or self.periodic_rejoin_in_progress:
                return None
            if not self.periodic_schedule_enabled or self.next_periodic_rejoin_at is None:
                return None
            if now < self.next_periodic_rejoin_at:
                return None
            if self.periodic_rejoin_retry_at is not None and now < self.periodic_rejoin_retry_at:
                return None
            if self.status != "Running" and not self.status.startswith("Scheduled Retry"):
                return None
            if not self.recovery_queue.empty():
                return None

            self.periodic_rejoin_in_progress = True
            self.periodic_rejoin_generation = self.generation
            self.periodic_rejoin_attempts += 1
            self.periodic_rejoin_retry_at = None
            self.status = "Scheduled Rejoin"
            return self.generation

    def finish_periodic_rejoin(
        self,
        generation: int,
        success: bool,
        periodic_enabled: bool,
        interval_seconds: int,
        retry_seconds: int,
        max_retries: int,
        error: str = "",
    ) -> bool:
        """Finish an attempt; stale attempts cannot overwrite emergency recovery."""
        now = time.monotonic()
        interval_seconds = max(1, int(interval_seconds))
        retry_seconds = max(1, int(retry_seconds))
        max_retries = max(0, int(max_retries))
        with self.lock:
            # Always release this attempt's in-flight flag, even when a higher
            # priority watchdog changed generation while an intent was pending.
            if self.periodic_rejoin_generation == generation:
                self.periodic_rejoin_in_progress = False
                self.periodic_rejoin_generation = None

            if not self.active or self.generation != generation or self.process_loss_queued:
                return False

            if not periodic_enabled:
                self.periodic_schedule_enabled = False
                self.periodic_interval_seconds = interval_seconds
                self.next_periodic_rejoin_at = None
                self.periodic_rejoin_retry_at = None
                self.periodic_rejoin_attempts = 0
                self.status = "Running" if self.online_since is not None else "Periodic Rejoin Paused"
                return False

            self.periodic_schedule_enabled = True
            self.periodic_interval_seconds = interval_seconds

            if success:
                self.online_since = now
                self.status = "Running"
                self.has_reached_running = True
                self.missing_pid_checks = 0
                self.window_missing_checks = 0
                self.activity_problem_checks = 0
                self.activity_problem_reason = ""
                self.process_loss_retry_after = 0.0
                self.last_successful_join_at = now
                self.periodic_schedule_anchor_at = now
                self.next_periodic_rejoin_at = now + interval_seconds
                self.periodic_rejoin_attempts = 0
                self.periodic_rejoin_retry_at = None
                return True

            if self.periodic_rejoin_attempts <= max_retries:
                self.periodic_rejoin_retry_at = now + retry_seconds
                self.status = f"Scheduled Retry {self.periodic_rejoin_attempts}/{max_retries}"
                return False

            # Retry budget exhausted. Keep the last-success timestamp truthful,
            # but anchor the next normal scheduled attempt in the future to avoid
            # hammering a device that is already under load.
            self.periodic_rejoin_attempts = 0
            self.periodic_rejoin_retry_at = None
            self.periodic_schedule_anchor_at = now
            self.next_periodic_rejoin_at = now + interval_seconds
            self.status = "Running" if self.online_since is not None else "Periodic Rejoin Failed"
            return False

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
        self._last_window_scan = 0.0
        self._last_window_scan_success = False
        self._window_packages_cache: set[str] = set()
        self._window_scan_serial = 0
        self._window_scan_in_progress = False
        self._last_activity_scan = 0.0
        self._last_activity_scan_success = False
        self._activity_problems_cache: Dict[str, str] = {}
        self._activity_scan_serial = 0
        self._activity_scan_in_progress = False

    def instances_snapshot(self) -> List[RobloxInstance]:
        with self.lock:
            return list(self.instances.values())

    def _window_scan_worker(self, packages: List[str], session_id: int) -> None:
        try:
            result = self.shell.window_packages(packages)
        except Exception:
            result = None
        with self.lock:
            self._window_scan_in_progress = False
            if session_id != self.startup_run_id:
                # A session was replaced while dumpsys was running. Discard its
                # result so an old window set cannot affect the new selection.
                self._last_window_scan = 0.0
                return
            if result is None:
                self._last_window_scan_success = False
            else:
                self._window_packages_cache = set(result)
                self._last_window_scan_success = True
                self._window_scan_serial += 1

    def _activity_scan_worker(self, packages: List[str], session_id: int) -> None:
        try:
            result = self.shell.activity_problem_packages(packages)
        except Exception:
            result = None
        with self.lock:
            self._activity_scan_in_progress = False
            if session_id != self.startup_run_id:
                self._last_activity_scan = 0.0
                return
            if result is None:
                self._last_activity_scan_success = False
            else:
                self._activity_problems_cache = dict(result)
                self._last_activity_scan_success = True
                self._activity_scan_serial += 1

    def _schedule_health_scans(self, tracked_packages: List[str], now: float) -> None:
        if not tracked_packages:
            return

        start_window = False
        start_activity = False
        with self.lock:
            session_id = self.startup_run_id
            if (
                not self._window_scan_in_progress
                and now - self._last_window_scan >= WINDOW_SCAN_INTERVAL
            ):
                self._window_scan_in_progress = True
                self._last_window_scan = now
                start_window = True
            if (
                not self._activity_scan_in_progress
                and now - self._last_activity_scan >= ACTIVITY_SCAN_INTERVAL
            ):
                self._activity_scan_in_progress = True
                self._last_activity_scan = now
                start_activity = True

        if start_window:
            try:
                threading.Thread(
                    target=self._window_scan_worker,
                    args=(list(tracked_packages), session_id),
                    name="window-health-scan",
                    daemon=True,
                ).start()
            except Exception:
                with self.lock:
                    self._window_scan_in_progress = False
                    self._last_window_scan = 0.0

        if start_activity:
            try:
                threading.Thread(
                    target=self._activity_scan_worker,
                    args=(list(tracked_packages), session_id),
                    name="activity-health-scan",
                    daemon=True,
                ).start()
            except Exception:
                with self.lock:
                    self._activity_scan_in_progress = False
                    self._last_activity_scan = 0.0

    def start_sessions(self, packages: List[str]) -> None:
        self.stop_sessions(kill=False, clear=True)
        with self.lock:
            self.startup_run_id += 1
            startup_run_id = self.startup_run_id
            self.startup_in_progress = True
            self._last_window_scan = 0.0
            self._last_window_scan_success = False
            self._window_packages_cache = set()
            self._window_scan_serial = 0
            # Give WindowManager its first lightweight sample immediately and
            # stagger the heavier ActivityManager dump a few seconds later.
            self._last_activity_scan = time.monotonic()
            self._last_activity_scan_success = False
            self._activity_problems_cache = {}
            self._activity_scan_serial = 0
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

        Each package completes its normal launch flow (including lobby delay,
        deep-link retries, and PID verification) before the next package starts.
        After the join intent is accepted and the target process is verified,
        wait 20 seconds for the game to settle before continuing to the next clone.

        Logcat recovery is intentionally disarmed for this entire initial
        startup window. This prevents CPU/RAM loading noise from invalidating
        the launch generation and leaving a clone stuck in the lobby.
        """
        try:
            for instance in self.instances_snapshot():
                if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                    return

                launch_succeeded = self._launch_flow_entry(instance)

                if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                    return

                # Use the launch-flow result instead of the mutable display status:
                # the health monitor may update status during loading even though
                # the join flow itself succeeded.
                snap = instance.snapshot()
                if launch_succeeded and snap["online_since"] is not None:
                    delay = int(STARTUP_STABILIZATION_DELAY)
                    instance.add_event(
                        f"Join accepted; wait {delay}s before next package"
                    )
                    deadline = time.monotonic() + STARTUP_STABILIZATION_DELAY
                    while True:
                        if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                            return
                        remaining = max(0.0, deadline - time.monotonic())
                        if remaining <= 0:
                            break
                        # Do not overwrite the instance status every second here:
                        # the PID/ActivityManager watchdog must remain free to
                        # expose a real warning while the startup delay runs.
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

    def _launch_flow_entry(self, instance: RobloxInstance) -> bool:
        return self._launch_flow(instance, instance.current_generation())

    def _periodic_rejoin_settings(self, cfg: Optional[dict] = None) -> Tuple[bool, int]:
        if cfg is None:
            cfg = self.config.load()
        enabled = bool(cfg.get("periodic_rejoin_enabled", False))
        interval_seconds = max(1, int(cfg.get("periodic_rejoin_interval_hours", 4))) * 3600
        return enabled, interval_seconds

    def _mark_instance_online(self, instance: RobloxInstance) -> None:
        cfg = self.config.load()
        enabled, interval_seconds = self._periodic_rejoin_settings(cfg)
        instance.set_online(
            periodic_interval_seconds=interval_seconds if enabled else None,
            periodic_enabled=enabled,
        )

    def _launch_flow(self, instance: RobloxInstance, generation: int) -> bool:
        """Serialize full launch flows per package without blocking sibling clones."""
        with instance.operation_lock:
            if not instance.is_current(generation) or self.shutdown_event.is_set():
                return False
            return self._launch_flow_impl(instance, generation)

    def _open_join_target_with_retries(
        self,
        instance: RobloxInstance,
        generation: int,
        cfg: dict,
        *,
        max_attempts: int,
        retry_delay: float,
        context: str,
    ) -> Optional[CommandResult]:
        """Send a package-scoped join intent with bounded, cancellable retries.

        None means the attempt was superseded by a newer generation or shutdown;
        a CommandResult means the attempt budget was completed or a command was
        accepted. The per-package operation lock prevents concurrent join intents
        for one clone, while siblings remain independent.
        """
        max_attempts = max(1, int(max_attempts))
        retry_delay = max(0.0, float(retry_delay))
        last_result: Optional[CommandResult] = None

        with instance.operation_lock:
            for attempt in range(1, max_attempts + 1):
                if self.shutdown_event.is_set() or not instance.is_current(generation):
                    return None

                try:
                    # Keep launch commands serialized with other startup operations,
                    # but never hold the global semaphore during a retry delay.
                    with self.startup_semaphore:
                        result = self.shell.open_join_target(instance.package, cfg)
                except Exception as exc:
                    result = CommandResult(1, "", f"{type(exc).__name__}: {exc}")
                last_result = result

                if result.code == 0:
                    if attempt > 1:
                        instance.add_event(f"{context}: deep link accepted on attempt {attempt}/{max_attempts}")
                    return result

                instance.add_event(
                    f"{context}: deep-link attempt {attempt}/{max_attempts} failed ({result.code})"
                )
                if attempt >= max_attempts:
                    break

                instance.set_status(f"Join Retry {attempt + 1}/{max_attempts}")
                if not self._wait_with_generation(
                    instance, generation, retry_delay, "Join retry"
                ):
                    return None

        return last_result

    def _launch_flow_impl(self, instance: RobloxInstance, generation: int) -> bool:
        """Run the normal full launch flow for one package only.

        Returns True only after launch, lobby wait, deep-link and PID checks
        succeed. If this is a recovery from a previously observed missing
        window, also wait briefly for WindowManager to report that package again
        when WindowManager inspection is supported on this device.
        """
        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return False

        initial_state = instance.snapshot()
        require_window_restore = bool(
            initial_state.get("process_loss_queued")
            and initial_state.get("window_seen")
        )
        cfg = self.config.load()
        package = instance.package
        instance.add_event("Launching app")
        instance.set_status("Starting")

        with self.startup_semaphore:
            launch_result = self.shell.launch_normal(package)
        if launch_result.code != 0:
            instance.add_event(f"Launch failed ({launch_result.code})")
            instance.set_status("Launch Failed")
            return False

        # Wait for this exact package's process to appear before starting the
        # lobby timer. This works both for a genuinely dead app and a still-live
        # app whose launcher/activity needs to be brought back.
        pid_deadline = time.monotonic() + 12.0
        pids: List[int] = []
        while time.monotonic() < pid_deadline:
            if not instance.is_current(generation) or self.shutdown_event.is_set():
                return False
            pids = self.shell.pidof(package)
            instance.update_pids(pids)
            if pids:
                break
            self.shutdown_event.wait(0.5)

        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return False

        if not pids:
            instance.add_event("Launch failed: PID did not appear")
            instance.set_status("Launch Failed")
            return False

        delay = int(cfg.get("lobby_delay", DEFAULT_CONFIG["lobby_delay"]))
        instance.add_event(f"Lobby wait {delay}s")
        instance.set_status(f"Lobby {delay}s")
        if not self._wait_with_generation(instance, generation, float(delay), "Lobby"):
            return False

        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return False

        instance.add_event(f"Joining {join_target_label(cfg)}")
        instance.set_status("Joining")
        join_result = self._open_join_target_with_retries(
            instance,
            generation,
            cfg,
            max_attempts=IN_GAME_REJOIN_MAX_ATTEMPTS,
            retry_delay=STARTUP_JOIN_RETRY_DELAY,
            context="Startup join",
        )
        if join_result is None:
            return False
        if join_result.code != 0:
            instance.add_event(
                f"Startup deep link failed after {IN_GAME_REJOIN_MAX_ATTEMPTS} attempts"
            )
            instance.set_status("Join Failed")
            return False

        # Confirm the target package still has a process after the intent. A
        # successful `am start` only means Android accepted the command; it is
        # not proof that Roblox remained alive.
        verify_deadline = time.monotonic() + 8.0
        pids = []
        while time.monotonic() < verify_deadline:
            if not instance.is_current(generation) or self.shutdown_event.is_set():
                return False
            pids = self.shell.pidof(package)
            instance.update_pids(pids)
            if pids:
                break
            self.shutdown_event.wait(0.5)

        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return False

        if not pids:
            instance.add_event("Join failed: PID verification timeout")
            instance.set_status("Join Failed")
            return False

        # If this recovery was triggered because a window previously visible
        # for this package disappeared, verify that it returns. Unknown parser
        # output disables this one check rather than falsely declaring failure.
        if require_window_restore:
            window_deadline = time.monotonic() + WINDOW_RESTORE_TIMEOUT
            window_confirmed = False
            reliable_window_sample = False
            while time.monotonic() < window_deadline:
                if not instance.is_current(generation) or self.shutdown_event.is_set():
                    return False
                try:
                    observed = self.shell.window_packages([package])
                except Exception:
                    observed = None
                if observed is None:
                    instance.add_event("Window verification unavailable; PID verified")
                    break
                reliable_window_sample = True
                if package in observed:
                    window_confirmed = True
                    break
                self.shutdown_event.wait(1.0)

            if reliable_window_sample and not window_confirmed:
                instance.add_event("Recovery incomplete: package window did not return")
                instance.set_status("Window Restore Failed")
                return False

        if not instance.is_current(generation) or self.shutdown_event.is_set():
            return False

        self._mark_instance_online(instance)
        instance.add_event("Session online; launch flow verified")
        return True

    def _start_full_relaunch(
        self,
        instance: RobloxInstance,
        reason: str,
        generation: Optional[int] = None,
    ) -> bool:
        """Start one de-duplicated full launch flow for a single instance."""
        if generation is None:
            generation = instance.trigger_process_lost()
        elif not instance.is_current(generation):
            return False
        if generation is None:
            return False

        instance.add_event(f"{reason}; full relaunch generation {generation}")
        try:
            thread = threading.Thread(
                target=self._full_relaunch_worker,
                args=(instance, generation, reason),
                name=f"full-relaunch-{instance.package}",
                daemon=True,
            )
            thread.start()
            return True
        except Exception as exc:
            instance.add_event(f"Could not start recovery thread: {exc}")
            instance.finish_process_loss_failure(generation)
            return False

    def _full_relaunch_worker(
        self,
        instance: RobloxInstance,
        generation: int,
        reason: str,
    ) -> None:
        """Retry a full relaunch a bounded number of times without touching siblings."""
        for attempt in range(1, FULL_RELAUNCH_MAX_ATTEMPTS + 1):
            if self.shutdown_event.is_set() or not instance.is_current(generation):
                return

            instance.add_event(
                f"Full relaunch attempt {attempt}/{FULL_RELAUNCH_MAX_ATTEMPTS}: {reason}"
            )
            try:
                succeeded = self._launch_flow(instance, generation)
            except Exception as exc:
                succeeded = False
                instance.add_event(f"Relaunch exception: {type(exc).__name__}: {exc}")
                instance.set_status("Launch Failed")

            if succeeded:
                # _launch_flow -> set_online() releases the in-flight latch.
                return
            if self.shutdown_event.is_set() or not instance.is_current(generation):
                return

            if attempt < FULL_RELAUNCH_MAX_ATTEMPTS:
                wait_seconds = FULL_RELAUNCH_RETRY_BASE * attempt
                instance.add_event(f"Relaunch retry in {int(wait_seconds)}s")
                instance.set_status(f"Retry {attempt + 1}/{FULL_RELAUNCH_MAX_ATTEMPTS}")
                deadline = time.monotonic() + wait_seconds
                while time.monotonic() < deadline:
                    if self.shutdown_event.is_set() or not instance.is_current(generation):
                        return
                    self.shutdown_event.wait(min(0.25, deadline - time.monotonic()))

        if instance.finish_process_loss_failure(generation):
            instance.add_event(
                f"Full relaunch failed after {FULL_RELAUNCH_MAX_ATTEMPTS} attempts; watchdog retry armed"
            )
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
        """Long-lived per-package worker for in-game deep-link recovery.

        A newer event increments the package generation. That invalidates only
        the current attempt; it must never terminate this worker. Each recovery
        cycle tries the deep link up to three times, then waits a cooldown before
        starting another cycle while the same generation remains current.
        """
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

                # The event is queued and generation incremented under the
                # instance lock. Snapshot it only after dequeue to avoid racing
                # the enqueue operation itself.
                generation = instance.current_generation()
                if not self._wait_with_generation(
                    instance, generation, 25.0, "Error settle"
                ):
                    if self.shutdown_event.is_set() or not instance.snapshot()["active"]:
                        return
                    # A newer event superseded this one. Keep the worker alive
                    # and consume the new queued event on the next outer loop.
                    continue

                recovered = False
                while (
                    not self.shutdown_event.is_set()
                    and instance.snapshot()["active"]
                    and instance.is_current(generation)
                ):
                    cfg = self.config.load()
                    instance.add_event(f"Rejoining {join_target_label(cfg)}")
                    instance.set_status("Rejoining")

                    join_result = self._open_join_target_with_retries(
                        instance,
                        generation,
                        cfg,
                        max_attempts=IN_GAME_REJOIN_MAX_ATTEMPTS,
                        retry_delay=IN_GAME_REJOIN_RETRY_DELAY,
                        context="Recovery",
                    )

                    if join_result is None:
                        # Shutdown, deactivation, or a newer recovery event.
                        break

                    if join_result.code == 0:
                        if not instance.is_current(generation):
                            break
                        self._mark_instance_online(instance)
                        instance.add_event("Rejoin successful; package process retained")
                        recovered = True
                        break

                    # Bounded attempts per cycle avoid hammering Android/Roblox.
                    # Unlike the old flow, failure is not terminal: wait 60s and
                    # retry a fresh cycle until success or a newer event supersedes it.
                    instance.set_status(
                        f"Join Failed; retry in {int(IN_GAME_REJOIN_CYCLE_COOLDOWN)}s"
                    )
                    instance.add_event(
                        f"Deep link failed after {IN_GAME_REJOIN_MAX_ATTEMPTS} attempts; "
                        f"next cycle in {int(IN_GAME_REJOIN_CYCLE_COOLDOWN)}s"
                    )
                    if not self._wait_with_generation(
                        instance,
                        generation,
                        IN_GAME_REJOIN_CYCLE_COOLDOWN,
                        "Recovery cooldown",
                    ):
                        break
                    # Config is reloaded at the beginning of each cycle so a
                    # changed Place ID/private link takes effect without restart.

                if (
                    not recovered
                    and not self.shutdown_event.is_set()
                    and instance.snapshot()["active"]
                    and not instance.is_current(generation)
                ):
                    instance.add_event("Recovery superseded; worker remains active")

            except Exception as exc:
                # Keep a transient shell/config/programming exception from killing
                # the package's only recovery consumer.
                instance.add_event(
                    f"Recovery worker exception: {type(exc).__name__}: {exc}"
                )
                instance.set_status("Recovery Error")
                if not self.shutdown_event.is_set():
                    self.shutdown_event.wait(1.0)
            finally:
                instance.recovery_queue.task_done()

    def _package_named_in_log(self, raw_line: str) -> Optional[str]:
        """Resolve an exact selected package name embedded in a system crash line."""
        with self.lock:
            packages = [pkg for pkg, instance in self.instances.items() if instance.snapshot()["active"]]
        matches = [
            package for package in packages
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(package)}(?![A-Za-z0-9_])", raw_line)
        ]
        return matches[0] if len(matches) == 1 else None

    def handle_sensor_event(self, pid: int, reason: str, raw_line: str) -> None:
        # Keep the initial startup shield, but don't let logcat from an unrelated
        # process invalidate an in-flight per-package full relaunch.
        with self.lock:
            if self.startup_in_progress:
                return

        package = self.resolve_pid(pid)
        if not package and reason.startswith("Crash:"):
            # ActivityManager/lmkd often logs the *system* PID while embedding
            # the actual app package in its message (for example, "Process
            # com.roblox.clienu ... has died"). Resolve only an unambiguous
            # exact package name from strong crash-pattern lines.
            package = self._package_named_in_log(raw_line)
        if not package:
            return

        with self.lock:
            instance = self.instances.get(package)
        if not instance:
            return

        snap = instance.snapshot()
        if not snap["active"] or snap["process_loss_queued"]:
            return

        # Crash/ANR/OOM signatures use full launch recovery, not the in-game
        # deep-link-only queue. PID ownership or an exact package mention in a
        # strong system crash line must be established first.
        if reason.startswith("Crash:"):
            if not snap["has_reached_running"]:
                return
            self._start_full_relaunch(instance, f"Crash/ANR signal: {reason}")
            return

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
        """Poll PIDs promptly and consume asynchronously sampled UI/process health."""
        new_map: Dict[int, str] = {}
        now = time.monotonic()

        with self.lock:
            startup_in_progress = self.startup_in_progress

        instances = self.instances_snapshot()
        tracked_packages = [
            instance.package
            for instance in instances
            if instance.snapshot()["active"]
        ]
        # Never block the one-second PID watchdog on expensive `dumpsys` calls.
        # WindowManager and ActivityManager scans run in separate daemon threads.
        self._schedule_health_scans(tracked_packages, now)
        with self.lock:
            window_packages: Optional[set[str]] = (
                set(self._window_packages_cache)
                if self._last_window_scan_success else None
            )
            window_scan_serial = self._window_scan_serial
            activity_problems: Optional[Dict[str, str]] = (
                dict(self._activity_problems_cache)
                if self._last_activity_scan_success else None
            )
            activity_scan_serial = self._activity_scan_serial

        for instance in instances:
            snap = instance.snapshot()
            if not snap["active"]:
                continue

            # If an earlier full relaunch exhausted its bounded retries, arm a
            # fresh attempt after its cooldown. This is per-instance and cannot
            # restart a healthy sibling.
            if (
                snap["status"] == "Recovery Failed"
                and not snap["process_loss_queued"]
                and now >= instance.process_loss_retry_after
            ):
                self._start_full_relaunch(instance, "Watchdog retry after recovery failure")
                snap = instance.snapshot()

            package = instance.package
            try:
                pids = self.shell.pidof(package)
            except Exception:
                # A command error is indistinguishable from no PID at this
                # call site, but three checks plus prior Running state protects
                # against one transient failure.
                pids = []

            if not pids:
                # Keep the displayed PID honest even while the recovery latch is
                # set. update_pids([]) does not release that latch.
                instance.update_pids([])
                # Only instances that previously reached Running are eligible.
                # A global staggered startup must not shield an already-online
                # sibling from crash recovery.
                has_been_online = snap["has_reached_running"]
                if not has_been_online:
                    instance.update_pids([])
                elif not snap["process_loss_queued"]:
                    missing_count = instance.note_pid_missing()
                    if missing_count < PID_MISSING_CONFIRMATIONS:
                        instance.set_status(
                            f"PID Missing {missing_count}/{PID_MISSING_CONFIRMATIONS}"
                        )
                    else:
                        self._start_full_relaunch(
                            instance,
                            f"Process Lost: PID missing {PID_MISSING_CONFIRMATIONS} consecutive checks",
                        )
            else:
                instance.update_pids(pids)
                if window_packages is not None and package in window_packages:
                    # Learn whether this package is represented by WindowManager
                    # at all before using absence as a watchdog signal.
                    instance.note_window_present(window_scan_serial)
                current = instance.snapshot()

                # Explicit ActivityManager notResponding/crashing flags are a
                # second signal. Require two fresh positive dumps to avoid
                # reacting to stale or momentary activity-state transitions.
                if (
                    activity_problems is not None
                    and activity_scan_serial > 0
                    and current["has_reached_running"]
                    and not current["process_loss_queued"]
                ):
                    activity_reason = activity_problems.get(package)
                    if activity_reason:
                        problem_count = instance.note_activity_problem(
                            activity_reason, activity_scan_serial
                        )
                        if problem_count < ACTIVITY_PROBLEM_CONFIRMATIONS:
                            instance.set_status(
                                f"Activity Unhealthy {problem_count}/{ACTIVITY_PROBLEM_CONFIRMATIONS}"
                            )
                        else:
                            started = self._start_full_relaunch(
                                instance,
                                f"ActivityManager unhealthy: {activity_reason}",
                            )
                            if started:
                                current = instance.snapshot()
                    else:
                        instance.note_activity_healthy(activity_scan_serial)

                current = instance.snapshot()
                has_been_online = current["has_reached_running"]
                in_full_relaunch = current["process_loss_queued"]
                online_since = current["online_since"]
                stabilization_complete = (
                    online_since is None
                    or now - online_since >= STARTUP_STABILIZATION_DELAY
                )
                window_watch_armed = (
                    has_been_online
                    and current["window_seen"]
                    and not in_full_relaunch
                    and (not startup_in_progress or stabilization_complete)
                    and window_packages is not None
                )

                if window_watch_armed:
                    if package in window_packages:
                        # Presence from cached output is safe to use to clear a
                        # streak; absence only counts on a fresh successful scan.
                        instance.note_window_present(window_scan_serial)
                    else:
                        missing_count = instance.note_window_missing(window_scan_serial)
                        if missing_count < WINDOW_MISSING_CONFIRMATIONS:
                            instance.set_status(
                                f"Window Missing {missing_count}/{WINDOW_MISSING_CONFIRMATIONS}"
                            )
                        else:
                            self._start_full_relaunch(
                                instance,
                                f"Window Lost: absent from {WINDOW_MISSING_CONFIRMATIONS} fresh WindowManager scans",
                            )

            for pid in pids:
                new_map[pid] = package

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


class ScheduledRejoinWorker(threading.Thread):
    """Rejoin healthy Roblox instances periodically without killing processes.

    One worker serializes scheduled rejoin requests globally. Full relaunch and
    Logcat recovery retain priority, and each intent is scoped to one package.
    """

    def __init__(self, manager: RobloxManager, stop_event: threading.Event):
        super().__init__(name="scheduled-rejoin", daemon=True)
        self.manager = manager
        self.stop_event = stop_event
        self.last_dispatch_at = 0.0
        self.cursor = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                cfg = self.manager.config.load()
                enabled, interval_seconds = self.manager._periodic_rejoin_settings(cfg)
                now = time.monotonic()

                instances = self.manager.instances_snapshot()
                due = []
                for instance in instances:
                    instance.sync_periodic_schedule(enabled, interval_seconds, now)
                    snap = instance.snapshot()
                    due_at = snap.get("next_periodic_rejoin_at")
                    retry_at = snap.get("periodic_rejoin_retry_at")
                    if (
                        snap["active"]
                        and snap["has_reached_running"]
                        and due_at is not None
                        and due_at <= now
                        and (retry_at is None or retry_at <= now)
                        and not snap["periodic_rejoin_in_progress"]
                        and not snap["process_loss_queued"]
                    ):
                        due.append((due_at, instance.package, instance))

                if due:
                    stagger = max(5, int(cfg.get("periodic_rejoin_stagger_seconds", 45)))
                    wait_left = self.last_dispatch_at + stagger - now
                    if self.last_dispatch_at and wait_left > 0:
                        self.stop_event.wait(min(1.0, wait_left))
                        continue

                    due.sort(key=lambda item: (item[0], item[1]))
                    # Rotate ties to avoid repeatedly favoring the same package
                    # after shared retries or config changes.
                    if len(due) > 1:
                        offset = self.cursor % len(due)
                        due = due[offset:] + due[:offset]
                    claimed = False
                    for _due_at, _package, instance in due:
                        generation = instance.begin_periodic_rejoin(time.monotonic())
                        if generation is None:
                            continue
                        self.cursor += 1
                        self.last_dispatch_at = time.monotonic()
                        self._perform_rejoin(instance, generation, cfg)
                        claimed = True
                        break
                    if claimed:
                        continue
            except Exception as exc:
                # Keep the periodic worker alive for long AFK runs, while leaving
                # a breadcrumb rather than silently losing the scheduling loop.
                try:
                    for instance in self.manager.instances_snapshot():
                        if instance.snapshot()["active"]:
                            instance.add_event(
                                f"Scheduled rejoin worker error: {type(exc).__name__}: {exc}"
                            )
                            break
                except Exception:
                    pass
            self.stop_event.wait(1.0)

    def _perform_rejoin(self, instance: RobloxInstance, generation: int, cfg: dict) -> None:
        success = False
        error = ""
        package = instance.package
        try:
            if self.stop_event.is_set() or not instance.is_current(generation):
                error = "cancelled by higher-priority recovery"
                return

            cfg = self.manager.config.load()
            enabled, interval_seconds = self.manager._periodic_rejoin_settings(cfg)
            if not enabled:
                error = "scheduled rejoin disabled before dispatch"
                return

            instance.add_event(
                f"Scheduled rejoin attempt {instance.snapshot()['periodic_rejoin_attempts']}"
            )
            instance.set_status("Scheduled Rejoin")

            # Serialize just this instance's action against full relaunch and
            # Logcat deep-link operations. Sibling packages are never locked.
            with instance.operation_lock:
                if self.stop_event.is_set() or not instance.is_current(generation):
                    error = "cancelled before deep link"
                    return
                result = self.manager.shell.open_join_target(package, cfg)
                if result.code != 0:
                    error = f"deep-link command failed ({result.code})"
                    return

                verify_seconds = max(5, int(cfg.get("periodic_rejoin_verify_seconds", 15)))
                deadline = time.monotonic() + verify_seconds
                observed_pids: List[int] = []
                while time.monotonic() < deadline:
                    if self.stop_event.is_set() or not instance.is_current(generation):
                        error = "cancelled during verification"
                        return
                    observed_pids = self.manager.shell.pidof(package)
                    if observed_pids:
                        break
                    self.stop_event.wait(min(1.0, max(0.0, deadline - time.monotonic())))

                if not observed_pids:
                    error = f"PID not present after {verify_seconds}s verification"
                    return

                instance.update_pids(observed_pids)
                # A live PID plus an accepted VIEW intent is the portable
                # minimum verification. WindowManager remains the separate
                # health watchdog; a flaky dumpsys parser must not create a
                # false scheduled-rejoin failure loop.
                success = True
                instance.add_event("Scheduled deep link accepted; PID verified")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            current_cfg = self.manager.config.load()
            enabled, interval_seconds = self.manager._periodic_rejoin_settings(current_cfg)
            retry_seconds = max(10, int(current_cfg.get("periodic_rejoin_retry_seconds", 60)))
            max_retries = max(0, int(current_cfg.get("periodic_rejoin_max_retries", 2)))
            finished = instance.finish_periodic_rejoin(
                generation=generation,
                success=success,
                periodic_enabled=enabled,
                interval_seconds=interval_seconds,
                retry_seconds=retry_seconds,
                max_retries=max_retries,
                error=error,
            )
            if success and finished:
                instance.add_event("Scheduled rejoin completed; next interval armed")
            elif error:
                snap = instance.snapshot()
                if snap["active"] and snap["generation"] == generation and not snap["process_loss_queued"]:
                    instance.add_event(f"Scheduled rejoin failed: {error}")


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

        # Treat the visible Roblox "Connection Failed (Error Code: 279)"
        # signature as a strong, specific signal before generic code matching.
        if CONNECTION_FAILED_279_RE.search(text):
            return "Connection Failed 279"

        # Find codes only when their local text context explicitly describes a
        # Roblox/error/disconnect condition. Iterate contextual matches instead
        # of trusting the first number in a line. If no valid code context is
        # found, still inspect crash signatures below rather than returning early.
        for context_match in ROBLOX_ERROR_CONTEXT_RE.finditer(text):
            code_match = ROBLOX_ERROR_RE.search(context_match.group(0))
            if not code_match:
                continue
            code = code_match.group(1)
            context_text = context_match.group(0)
            if code in {"403", "524"} and HTTP_OR_STATUS_RE.search(context_text):
                continue
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
        and may trigger immediately. High-confidence crash/ANR/OOM signatures
        also pass immediately because they can appear only once before exit;
        ordinary error candidates require two matching events from the same PID.
        """
        if reason in {"Error 267", "Disconnect 277", "Connection Failed 279"}:
            return True
        if reason.startswith("Crash:"):
            # Crash signatures often occur only once immediately before the
            # process exits, so requiring a second line can miss the recovery.
            # The manager still validates PID/package ownership before acting.
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
            f"|{self._fit('Join: '+join_target_label(cfg)+' | Lobby: '+str(cfg['lobby_delay'])+'s | Rejoin: '+('ON' if cfg['periodic_rejoin_enabled'] else 'OFF')+' '+str(cfg['periodic_rejoin_interval_hours'])+'h', inner)}|",
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
            f"|{self._fit('Join:'+('Private' if cfg.get('join_mode') == 'private_server_link' else str(cfg['place_id']))+' Lobby:'+str(cfg['lobby_delay'])+'s R:'+('ON' if cfg['periodic_rejoin_enabled'] else 'OFF')+str(cfg['periodic_rejoin_interval_hours'])+'h', inner)}|",
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
        self.scheduled_rejoin_worker = ScheduledRejoinWorker(self.manager, self.shutdown_event)
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
        if not self.scheduled_rejoin_worker.is_alive():
            self.scheduled_rejoin_worker.start()

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
                    f"Join Target        : {join_target_label(cfg)}",
                    f"Scheduled Rejoin   : {'ON' if cfg['periodic_rejoin_enabled'] else 'OFF'} ({cfg['periodic_rejoin_interval_hours']} jam)",
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
            mode_label = "Private Server Link" if cfg["join_mode"] == "private_server_link" else "Place ID"
            TerminalIO.clear()
            self._box(
                [
                    f"1. Lobby delay : {cfg['lobby_delay']} detik",
                    f"2. Place ID    : {cfg['place_id']}",
                    f"3. Join mode   : {mode_label}",
                    "4. Set private server link",
                    f"5. Scheduled Rejoin : {'ON' if cfg['periodic_rejoin_enabled'] else 'OFF'}",
                    f"6. Rejoin interval  : {cfg['periodic_rejoin_interval_hours']} jam",
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
                    value = int(TerminalIO.safe_line_input("Lobby delay (0-3600 detik): ").strip())
                    if not 0 <= value <= 3600:
                        raise ValueError
                    self.config.update(value, cfg["place_id"])
                    self._safe_print("Lobby delay disimpan.")
                except (ValueError, KeyboardInterrupt, EOFError):
                    self._safe_print("Lobby delay harus angka 0 sampai 3600 detik.")
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
            elif choice == "3":
                if cfg["join_mode"] == "private_server_link":
                    self.config.update(
                        cfg["lobby_delay"], cfg["place_id"], join_mode="place_id"
                    )
                    self._safe_print("Mode join diubah ke Place ID.")
                    time.sleep(0.8)
                    continue

                link = cfg.get("private_server_link", "")
                if not link:
                    try:
                        link = TerminalIO.safe_line_input(
                            "Tempel link private server Roblox: "
                        ).strip()
                    except (KeyboardInterrupt, EOFError):
                        return
                try:
                    private_server_deep_link(link)
                    self.config.update(
                        cfg["lobby_delay"],
                        cfg["place_id"],
                        join_mode="private_server_link",
                        private_server_link=link,
                    )
                    self._safe_print("Mode Private Server Link diaktifkan.")
                except ValueError as exc:
                    self._safe_print(str(exc))
                time.sleep(1.2)
            elif choice == "4":
                try:
                    link = TerminalIO.safe_line_input(
                        "Tempel link private server Roblox: "
                    ).strip()
                    private_server_deep_link(link)
                    self.config.update(
                        cfg["lobby_delay"],
                        cfg["place_id"],
                        join_mode="private_server_link",
                        private_server_link=link,
                    )
                    self._safe_print("Link disimpan. Mode Private Server Link aktif.")
                except ValueError as exc:
                    self._safe_print(str(exc))
                except (KeyboardInterrupt, EOFError):
                    return
                time.sleep(1.2)
            elif choice == "5":
                enabled = not cfg["periodic_rejoin_enabled"]
                self.config.update(
                    cfg["lobby_delay"],
                    cfg["place_id"],
                    periodic_rejoin_enabled=enabled,
                )
                self._safe_print(
                    f"Scheduled Rejoin {'diaktifkan' if enabled else 'dinonaktifkan'}."
                )
                time.sleep(0.8)
            elif choice == "6":
                try:
                    hours = int(TerminalIO.safe_line_input("Interval rejoin (1-168 jam): ").strip())
                    if not 1 <= hours <= 168:
                        raise ValueError
                    self.config.update(
                        cfg["lobby_delay"],
                        cfg["place_id"],
                        periodic_rejoin_interval_hours=hours,
                    )
                    self._safe_print(f"Interval Scheduled Rejoin disimpan: {hours} jam.")
                except (ValueError, KeyboardInterrupt, EOFError):
                    self._safe_print("Interval harus angka antara 1 sampai 168 jam.")
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

        for thread in (self.pid_monitor, self.logcat_sensor, self.scheduled_rejoin_worker):
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
