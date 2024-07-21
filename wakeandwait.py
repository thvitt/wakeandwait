#!/usr/bin/env python3

"""
Wake network hosts and wait until they are ready.

Usage:
    wakeandwait [options] (MAC|IP|HOST|Port) ...

This command first broadcasts the wake on lan magic package to all MAC adresses given on the command line. It then waits until all services are up and finally exits.
"""

from concurrent.futures import wait
from threading import Thread
from time import sleep
from wakeonlan import send_magic_packet
from socket import create_connection
import re
import sys
from rich.console import Console, Group
from rich.spinner import SPINNERS
from rich.status import Status
from rich.live import Live
from concurrent.futures.thread import ThreadPoolExecutor

DEFAULT_PORT = 22

SPINNERS["ok"] = {"interval": 1000, "frames": ["✔"]}
SPINNERS["pulsedot"] = {"interval": 80, "frames": "·•●•·"}


def is_mac(arg: str) -> bool:
    return re.match(r"([0-9a-fA-F]{2}[:-]?){5}[0-9a-fA-F]{2}", arg) is not None


def is_port(arg: str) -> int | None:
    if arg.isdigit():
        port = int(arg)
        if 0 < port <= 0xFFFF:
            return port
    return None


class Service:
    host: str
    port: int
    ok: bool = False
    answer: str | None = None
    error: Exception | None = None

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port

    def update_status(
        self, ok: bool, /, msg: str | None = None, error: Exception | None = None
    ):
        self.ok = ok
        self.answer = msg
        self.error = error

    def check1(self):
        try:
            connection = create_connection((self.host, self.port))
            answer_b = connection.recv(4096)
            self.update_status(
                True, msg=answer_b.decode(encoding="utf-8", errors="replace")
            )
        except OSError as e:
            self.update_status(False, error=e)
        return self.ok

    def wait(self):
        while not self.check1():
            pass


class RichService(Service):

    def __init__(self, host: str, port: int) -> None:
        super().__init__(host, port)
        self.status = Status(self, spinner="pulsedot")

    def update_status(
        self, ok: bool, /, msg: str | None = None, error: Exception | None = None
    ):
        super().update_status(ok, msg, error)
        if self.ok:
            self.status.update(self, spinner="ok", spinner_style="green")
            self.status.stop()

    def __rich__(self) -> str:
        color = "green" if self.ok else "red"
        return f"[bold]{self.host}[/bold]:{self.port:<5}\t[{color}]{self.answer.strip().replace('\n', '|') or "Connected" if self.ok else self.error or 'Connecting ...'}"


def parse_cmd_line(argv=sys.argv):
    macs = []
    old_host = host = None
    port = None
    services = []
    args = argv[-1:0:-1]
    while args:
        arg = args.pop()
        if is_mac(arg):
            macs.append(arg)
        elif is_port(arg):
            port = int(arg)
            if host is not None:
                services.append((host, port))
        else:
            old_host, host = host, arg
            if port is not None:
                services.append((host, port))
            elif old_host is not None:
                services.append((old_host, DEFAULT_PORT))
    return macs, services


def main():
    macs, service_specs = parse_cmd_line()
    console = Console()
    wake_status = None
    if console.is_terminal:
        wake_status = Status(
            f"Sending WOL magic packet to {len(macs)} devices ...", spinner="pulsedot"
        )
        services = [RichService(*spec) for spec in service_specs]
        live = Live(Group(wake_status, *(service.status for service in services)))
        live.start()
    else:
        services = [Service(*spec) for spec in service_specs]
        live = None

    send_magic_packet(*macs)
    if wake_status is not None:
        wake_status.update("Sent WOL magic packet", spinner="ok")
        wake_status.stop()
    executor = ThreadPoolExecutor()
    futures = [executor.submit(service.wait) for service in services]
    done, failed = wait(futures)
    if live is not None:
        live.stop()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
