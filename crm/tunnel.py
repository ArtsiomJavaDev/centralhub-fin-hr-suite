"""SSH local port forward to AWS RDS (CRM MySQL)."""
from __future__ import annotations

import socket
import subprocess
import time
from typing import Optional

from crm.settings import CrmSettings


class SshTunnelError(RuntimeError):
    pass


class SshTunnel:
    """Manage `ssh -L local_port:rds_host:rds_port user@bastion -N` as subprocess."""

    def __init__(self, settings: CrmSettings) -> None:
        self._settings = settings
        self._proc: Optional[subprocess.Popen] = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, wait_seconds: float = 8.0) -> None:
        errors = self._settings.validate()
        if errors:
            raise SshTunnelError("; ".join(errors))

        if self.is_running:
            return

        cmd = self._settings.ssh_command_line()
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
        except FileNotFoundError:
            raise SshTunnelError(
                "ssh не найден. Установите OpenSSH Client (Параметры Windows → Приложения → Дополнительные компоненты)."
            ) from None

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self._proc.poll() is not None:
                err = (self._proc.stderr.read() or b"").decode("utf-8", errors="replace").strip()
                raise SshTunnelError(
                    f"SSH туннель завершился с кодом {self._proc.returncode}. {err or 'Проверьте ключ и whitelist IP.'}"
                )
            if self._port_open():
                return
            time.sleep(0.25)

        self.stop()
        raise SshTunnelError(
            f"Туннель не поднялся за {wait_seconds}s на порту {self._settings.local_port}. "
            f"Проверьте IP в whitelist сервера {self._settings.ssh_host} и SSH-ключ."
        )

    def stop(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def _port_open(self) -> bool:
        try:
            with socket.create_connection(
                (self._settings.mysql_host, self._settings.local_port),
                timeout=0.5,
            ):
                return True
        except OSError:
            return False

    def __enter__(self) -> "SshTunnel":
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()
