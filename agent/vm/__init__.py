from __future__ import annotations

import subprocess
import asyncio
import os
from functools import partial
from pathlib import Path

from threading import Lock

from ..config import (
    UPLOAD_DIR,
    VM_IMAGE,
    PERSIST_VMS,
    VM_STATE_DIR,
    HARD_TIMEOUT,
    VM_DOCKER_HOST,
)
from ..utils.helpers import limit_chars

from ..utils.logging import get_logger

_LOG = get_logger(__name__)


def _sanitize(name: str) -> str:
    """Return a Docker-safe name fragment."""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    return "".join(c if c in allowed else "_" for c in name)


class LinuxVM:
    """Manage a lightweight Docker-based VM.

    The default image provides Python and pip so packages can be installed
    immediately. A custom image can be supplied via ``VM_IMAGE``.
    """

    def __init__(
        self,
        username: str,
        image: str = VM_IMAGE,
        host_dir: str = UPLOAD_DIR,
    ) -> None:
        self._image = image
        self._name = f"chat-vm-{_sanitize(username)}"
        self._running = False
        self._host_dir = Path(host_dir)
        self._host_dir.mkdir(parents=True, exist_ok=True)
        self._state_dir = Path(VM_STATE_DIR) / _sanitize(username)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._env = {}
        if VM_DOCKER_HOST:
            _LOG.debug("Using custom Docker host: %s", VM_DOCKER_HOST)
            self._env["DOCKER_HOST"] = VM_DOCKER_HOST

    def start(self) -> None:
        """Start the VM if it is not already running."""
        if self._running:
            return

        try:
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._name],
                capture_output=True,
                text=True,
                env=self._env if self._env else None,
            )
            if inspect.returncode == 0:
                if inspect.stdout.strip() == "true":
                    self._running = True
                    return
                subprocess.run(
                    ["docker", "start", self._name],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=self._env if self._env else None,
                )
                self._running = True
                return

            subprocess.run(
                ["docker", "pull", self._image],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env if self._env else None,
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    self._name,
                    "-v",
                    f"{self._host_dir}:/data",
                    "-v",
                    f"{self._state_dir}:/state",
                    self._image,
                    "sleep",
                    "infinity",
                ],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env if self._env else None,
            )
            self._running = True
        except Exception as exc:  # pragma: no cover - runtime failures
            _LOG.error("Failed to start VM: %s", exc)
            raise RuntimeError(f"Failed to start VM: {exc}") from exc

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = 3,
        detach: bool = False,
        stdin_data: str | bytes | None = None,
    ) -> str:
        """Execute a command inside the running VM.

        Parameters
        ----------
        command:
            The shell command to run inside the container.
        timeout:
            Maximum time in seconds to wait for completion. Set to ``None``
            to wait indefinitely. Ignored when ``detach`` is ``True``.
        detach:
            Run the command in the background without waiting for it to finish.
        """
        if not self._running:
            raise RuntimeError("VM is not running")

        cmd = [
            "docker",
            "exec",
            "-i",
        ]
        if detach:
            cmd.append("-d")
        cmd.extend(
            [
                self._name,
                "bash",
                "-lc",
                command,
            ]
        )

        try:
            completed = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=isinstance(stdin_data, str),
                timeout=HARD_TIMEOUT,
                env=self._env if self._env else None,
            )
        except subprocess.TimeoutExpired as exc:
            return f"Command timed out after {timeout}s: {exc.cmd}"
        except Exception as exc:  # pragma: no cover - unforeseen errors
            return f"Failed to execute command: {exc}"

        output = completed.stdout
        if completed.stderr:
            output = f"{output}\n{completed.stderr}" if output else completed.stderr
        return limit_chars(output)

    async def execute_async(
        self,
        command: str,
        *,
        timeout: int | None = 3,
        detach: bool = False,
        stdin_data: str | bytes | None = None,
    ) -> str:
        """Asynchronously execute ``command`` inside the running VM."""
        loop = asyncio.get_running_loop()
        func = partial(
            self.execute,
            command,
            timeout=HARD_TIMEOUT,
            detach=detach,
            stdin_data=stdin_data,
        )
        return await loop.run_in_executor(None, func)

    def stop(self) -> None:
        """Terminate the VM if running."""
        if not self._running:
            return

        if PERSIST_VMS:
            subprocess.run(
                ["docker", "stop", self._name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env if self._env else None,
            )
        else:
            subprocess.run(
                ["docker", "rm", "-f", self._name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self._env if self._env else None,
            )
        self._running = False

    def __enter__(self) -> "LinuxVM":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


class VMRegistry:
    """Manage Linux VM instances on a per-user basis."""

    _vms: dict[str, LinuxVM] = {}
    _counts: dict[str, int] = {}
    _lock = Lock()

    @classmethod
    def acquire(cls, username: str) -> LinuxVM:
        """Return a running VM for ``username``, creating it if needed."""

        with cls._lock:
            vm = cls._vms.get(username)
            if vm is None:
                vm = LinuxVM(
                    username,
                    host_dir=str(Path(UPLOAD_DIR) / username),
                )
                cls._vms[username] = vm
                cls._counts[username] = 0
            cls._counts[username] += 1

        vm.start()
        return vm

    @classmethod
    def release(cls, username: str) -> None:
        """Release one reference to ``username``'s VM and stop it if unused."""

        with cls._lock:
            vm = cls._vms.get(username)
            if vm is None:
                return

            cls._counts[username] -= 1
            if cls._counts[username] <= 0:
                cls._counts[username] = 0
                if not PERSIST_VMS:
                    vm.stop()
                del cls._vms[username]
                del cls._counts[username]

    @classmethod
    def shutdown_all(cls) -> None:
        """Stop and remove all managed VMs."""

        with cls._lock:
            if not PERSIST_VMS:
                for vm in cls._vms.values():
                    vm.stop()
            cls._vms.clear()
            cls._counts.clear()

from ..utils.debug import debug_all
debug_all(globals())

