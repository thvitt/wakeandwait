#!/usr/bin/env python3

"""
Wake network hosts and wait until they are ready.

Usage:
    wakeandwait [options] (MAC|IP|HOST|Port) ...

This command first broadcasts the wake on lan magic package to all MAC adresses given on the command line. It then waits until all services are up and finally exits.
"""

from datetime import timedelta
import logging
import re
import sys
from argparse import ArgumentParser
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import Future, wait
from concurrent.futures.thread import ThreadPoolExecutor
from pathlib import Path
from socket import create_connection
from time import monotonic, sleep
from typing import Literal, TypedDict, overload

from rich.console import Console, Group
from rich.live import Live
from rich.logging import RichHandler
from rich.spinner import SPINNERS  # noqa
from rich.status import Status
from tomlkit import dump, load
from wakeonlan import send_magic_packet
from xdg.BaseDirectory import load_config_paths, save_config_path
from unittest.mock import Mock
import shlex
import subprocess
from subprocess import CalledProcessError

logger = Mock()

DEFAULT_PORT = 22
APP_NAME = "wakeandwait"
SPINNERS["ok"] = {"interval": 1000, "frames": ["✔"]}
SPINNERS["pulsedot"] = {"interval": 200, "frames": "·•●•·"}

console = Console()
QUIET = True


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
    start: float
    duration: float = 0
    tries: int = 0
    status_widget: Status | None = None

    def __init__(self, host: str, port: int, rich: bool = False) -> None:
        self.host = str(host)
        self.port = int(port)
        self.start = monotonic()
        self.tries = 0
        if rich:
            self.status_widget = Status(self, spinner="pulsedot")

    def update_status(
        self, ok: bool, /, msg: str | None = None, error: Exception | None = None
    ):
        self.ok = ok
        self.answer = msg
        self.error = error
        if self.status_widget and self.ok:
            self.status_widget.update(self, spinner="ok", spinner_style="green")

    def check1(self):
        try:
            logger.debug("Connecting to %s (%s:%s)", self, self.host, self.port)
            self.tries = self.tries + 1
            connection = create_connection((self.host, self.port))
            logger.debug("Connected: Reading from %s", self)
            answer_b = connection.recv(4096)
            answer = answer_b.decode(encoding="utf-8", errors="replace")
            self.duration = monotonic() - self.start
            logger.info(
                "%s is available (%s)",
                self,
                answer,
            )
            self.update_status(True, msg=answer)
        except OSError as e:
            self.duration = monotonic() - self.start
            logger.info(
                "%s is not yet available (%s)",
                self,
                e,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            self.update_status(False, error=e)
        return self.ok

    def wait(self):
        logger.debug("Waiting for %s", self)
        try:
            while not self.check1():
                sleep(1)
            return self
        except Exception as e:
            logger.error("Service %s failed: %s", self, e, exc_info=True)
            raise

    @property
    def perfdata(self) -> str:
        return f"{self.tries}, {timedelta(seconds=self.duration)}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port} ({self.perfdata})"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.host!r}, {self.port!r})"

    def __rich__(self) -> str:
        color = "green" if self.ok else "red"
        return f"[bold]{self.host}[/bold]:{self.port:<5}\t[{color}]{self.answer.strip( ).replace('\n', '|') or "Connected" if self.ok else self.error or 'Connecting ...'} [/{color}] ({self.perfdata})"


class Command(Service):

    command: list[str]

    def __init__(self, command: str, rich: bool = False) -> None:
        super().__init__("", 0, rich=rich)
        self.command = shlex.split(command)

    def check1(self):
        try:
            logger.debug("Running command %s", self)
            self.tries = self.tries + 1
            result = subprocess.run(
                self.command, capture_output=True, check=True, text=True
            )
            self.answer = result.stdout
            self.duration = monotonic() - self.start
            logger.info(
                "Command %s finished with %s",
                self,
                self.answer,
            )
            self.update_status(True, msg=self.answer)
        except OSError as e:
            self.duration = monotonic() - self.start
            logger.info(
                "Command %s failed with %s",
                self,
                e,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            self.update_status(False, error=e)
        except CalledProcessError as e:
            self.duration = monotonic() - self.start
            logger.info(
                "Command %s failed with %d (%s)",
                self,
                e.returncode,
                e.stderr or e.stdout,
                exc_info=logger.isEnabledFor(logging.DEBUG),
            )
            self.update_status(False, error=e)
        return self.ok

    @property
    def cmd_str(self) -> str:
        return shlex.join(self.command)

    def __str__(self) -> str:
        return f"{shlex.join(self.command)} ({self.perfdata})"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({shlex.join(self.command)!r}))"

    def __rich__(self) -> str:
        if self.tries < 1:
            status = "[dim gray]Waiting ...[/dim gray]"
        elif self.ok:
            status = f"[green]{self.answer.strip().replace('\n', '|')[:80] or 'Success'}[/green]"
        else:
            status = f"[red]{self.error or 'Connecting ...'}[/red]"
        return f"[bold]{self.cmd_str}[/bold] \t{status}\t({self.perfdata})"


class OneConfig(TypedDict):
    wake: list[str]
    check: list[tuple[str, int]]
    run: list[str]


def load_all_settings():
    config = {}
    for dir in reversed(list(load_config_paths(APP_NAME))):
        config_path = Path(dir, "config.toml")
        if config_path.exists():
            logger.debug("Loading configuration from %s", config_path)
            with config_path.open("r") as f:
                config.update(load(f))
    logger.debug("Final configuration: %s", config)
    return config


@overload
def save_settings(
    destinations: OneConfig, /, name: str
) -> dict[str, OneConfig | str]: ...


@overload
def save_settings(
    destinations: OneConfig | None = None, /, name: str | None = None, default: str = ""
) -> dict[str, OneConfig | str]: ...


def save_settings(
    destinations: OneConfig | None = None,
    /,
    name: str | None = None,
    default: str | None = None,
) -> dict[str, OneConfig | str]:
    settings_file = Path(save_config_path(APP_NAME), "config.toml")
    if settings_file.exists():
        with settings_file.open("r", encoding="utf-8") as f:
            settings = load(f)
    else:
        settings = {}
    if default:
        settings["default"] = default
    if destinations and default and not name:
        name = default
    if destinations and name:
        settings[name] = destinations
    with settings_file.open("w", encoding="utf-8") as f:
        dump(settings, f)
    logger.info("Saved settings %s to %s", settings, settings_file)
    return settings


def parse_dests(dests: Sequence[str], config: dict[str, str | OneConfig]) -> OneConfig:
    macs = []
    old_host = host = None
    port = None
    services = []
    commands = []
    args = list(reversed(dests))
    while args:
        arg = args.pop()
        if arg in config:
            value = config[arg]
            if isinstance(value, Mapping):
                macs.extend(value.get("wake", []))
                services.extend(value.get("check", []))
            elif value in config:
                args.append(value)
            else:
                logger.error(
                    "Configuration entry %s has an invalid value %s. Skipping.",
                    arg,
                    value,
                )
        elif is_mac(arg):
            macs.append(arg)
        elif is_port(arg):
            port = int(arg)
            if host is not None:
                services.append((host, port))
        elif arg.startswith("!"):
            commands.append(arg[1:])
        else:
            old_host, host = host, arg
            if port is not None:
                services.append((host, port))
            elif old_host is not None:
                services.append((old_host, DEFAULT_PORT))
    return OneConfig(wake=macs, check=services, run=commands)


def parse_args(argv=None):
    parser = ArgumentParser(description=__doc__)
    # wol = parser.add_argument_group(title="Wake on LAN options")
    # wol.add_argument(
    #     "-i",
    #     "--destination-ip",
    #     help="Destination IP for the WOL package, default 255.255.255.255",
    # )
    # wol.add_argument(
    #     "-p", "--port", nargs=1, type=int, help="Destination port (default: 9)"
    # )
    parser.add_argument("-s", "--save", metavar="NAME", help="Save as setting NAME")
    parser.add_argument(
        "-d",
        "--default",
        metavar="NAME",
        help="Use settings NAME as default.",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", help="increase verbosity", default=0
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", default=False, help="no rich output"
    )
    parser.add_argument(
        "-n",
        "--notify",
        action="store_true",
        default=False,
        help="Desktop notification when everything is awake",
    )
    parser.add_argument(
        "destinations",
        nargs="*",
        action="extend",
        help="""Destinations to use. These can be configuration names,
                MAC adresses (which will be sent a wake message), host
                names or host IP adresses, and TCP port numbers.""",
    )
    return parser.parse_args(argv)


def configure_logging(verbosity: int, quiet: bool):
    global QUIET, logger
    QUIET = quiet and console.is_terminal
    level = logging.ERROR - (10 * verbosity)
    logging.basicConfig(
        level=level, format="%(message)s", handlers=[RichHandler(console=console)]
    )
    logger = logging.getLogger("__name__")


def describe_future(
    future: Future | Iterable[Future], method: Literal["str", "repr"] = "str"
) -> str:
    if not isinstance(future, Future):
        return ", ".join(describe_future(f, method) for f in future)

    if method == "str":
        convert = str
    else:
        convert = repr
    if future is None:
        return "<no future>"
    elif future.done():
        try:
            return convert(future.result())
        except Exception as e:
            return convert(e)
    else:
        return convert(future)


def waitandwake(destinations: OneConfig):
    logger.info("Destinations: %s", destinations)
    wake_status = None
    macs = destinations.get("wake", [])
    service_specs = destinations.get("check", [])
    command_specs = destinations.get("run", [])
    live = None
    try:
        if not QUIET:
            wake_status = Status(
                f"Sending WOL magic packet to {len(macs)} devices ...",
                spinner="pulsedot",
            )
            services = [Service(*spec, rich=True) for spec in service_specs]
            commands = [Command(spec, rich=True) for spec in command_specs]
            live = Live(
                Group(
                    wake_status,
                    *(service.status_widget for service in services),
                    *(command.status_widget for command in commands),
                ),
                console=console,
            )
            live.start()
        else:
            services = [Service(*spec) for spec in service_specs]
            commands = [Command(spec) for spec in command_specs]
            live = None

        # wake
        if macs:
            send_magic_packet(*macs)
            macs_str = ", ".join(macs)
            logger.info("Sent WOL magic packet to %s", macs_str)
            if wake_status is not None:
                wake_status.update(
                    f"Sent WOL magic packet to {macs_str}.", spinner="ok"
                )
                wake_status.stop()
        else:
            logger.info("No MACs to wake up")
            if wake_status is not None:
                wake_status.update("No MACs to wake up")
                wake_status.stop()

        executor = ThreadPoolExecutor()

        # def cancel(signal, trace):
        #     logger.info("Shutting down ...")
        #     executor.shutdown(True, cancel_futures=True)
        #
        # signal(SIGINT, cancel)

        # wait
        if services:
            logger.info("Starting services %s", services)
            futures = [executor.submit(service.wait) for service in services]
            done, failed = wait(futures)
            if failed:
                logger.error("Some services failed: %s", describe_future(failed))
                sys.exit(1)
            else:
                logger.debug("All services are up: %s", describe_future(done))
        else:
            logger.info("No services to wait for.")

        # run
        if commands:
            logger.info("Starting commands %s", commands)
            futures = [executor.submit(command.wait) for command in commands]
            done, failed = wait(futures)
            if failed:
                logger.error("Some commands failed: %s", failed)
                sys.exit(2)
            else:
                logger.debug("All commands have run successfully: %s", done)

    finally:
        if live is not None:
            live.stop()
    if services:
        return services


def notify(destinations: OneConfig | Iterable[Service]):
    import desktop_notify
    import asyncio

    body = "\n".join(str(destination) for destination in destinations)
    logger.debug("Notifying about %s using %s", destinations, body)
    n = desktop_notify.Notify(
        "WOL Devices are available",
        body,
        "network-server",
    )
    asyncio.run(n.show())


def main():
    options = parse_args()
    configure_logging(options.verbose, options.quiet)
    logger.debug("Options: %s", options)
    all_settings = load_all_settings()
    destinations = parse_dests(options.destinations, all_settings)

    if options.save:
        all_settings.update(save_settings(destinations, name=options.save))
    if options.default:
        if options.default in all_settings:
            all_settings.update(save_settings(default=options.default))
        elif options.save:
            logger.error("There is no setting %s: Not setting as default")
        elif destinations:
            all_settings.update(
                save_settings(
                    destinations, name=options.default, default=options.default
                )
            )
        else:
            logger.error(
                "There is no setting %s, and no configuration passed: Not setting as default"
            )
    if not options.save or options.default:
        if not destinations:
            destinations = parse_dests("default", all_settings)
            if destinations:
                logger.info("No destinations on the command line, using default config")
            else:
                logger.critical(
                    "No destinations on the command line and no default config."
                )
                sys.exit(2)

    services = waitandwake(destinations)
    if options.notify:
        notify(services or destinations)


if __name__ == "__main__":
    main()
