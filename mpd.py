"""Ask MPD to rescan one folder after genres were written."""

import socket

TIMEOUT = 10


class MpdError(Exception):
    """MPD could not be reached or refused the command."""


def quote(value: str) -> str:
    """Quote an MPD protocol argument."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update(host: str, port: int, folder: str, password: str = "") -> None:
    """Send 'update <folder>' to MPD; raise MpdError on any failure.

    folder is relative to MPD's music_directory.
    """
    if any(character in folder for character in "\r\n\0"):
        raise MpdError("folder contains a line break")
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as conn:
            stream = conn.makefile("rwb")
            greeting = stream.readline().decode("utf-8", "replace")
            if not greeting.startswith("OK MPD"):
                raise MpdError(f"unexpected greeting: {greeting.strip()}")
            commands = []
            if password:
                commands.append("password " + quote(password))
            commands.append("update " + quote(folder))
            for command in commands:
                stream.write(command.encode("utf-8") + b"\n")
                stream.flush()
                while True:
                    line = stream.readline().decode("utf-8", "replace")
                    if not line:
                        raise MpdError("connection closed")
                    if line.startswith("ACK"):
                        raise MpdError(line.strip())
                    if line.strip() == "OK":
                        break
            stream.write(b"close\n")
            stream.flush()
    except OSError as error:
        raise MpdError(str(error)) from error
