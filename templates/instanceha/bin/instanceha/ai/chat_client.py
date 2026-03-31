#!/usr/bin/env python3
"""Interactive CLI client for the InstanceHA chat interface.

Connect to the InstanceHA chat server over its Unix domain socket and
provide an interactive REPL for operators.

Usage:
    oc rsh <pod> python3 /var/lib/instanceha/scripts/instanceha/ai/chat_client.py

Or if installed as a script:
    oc rsh <pod> instanceha-chat
"""

import json
import os
import readline  # noqa: F401 -- enables line editing and history in input()
import signal
import socket
import sys


SOCKET_PATH = "/var/run/instanceha/agent.sock"
MAX_MESSAGE_SIZE = 65536
PROMPT = "instanceha> "


def connect(socket_path: str) -> socket.socket:
    """Connect to the chat server Unix socket."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
    except FileNotFoundError:
        print(f"Error: Socket not found at {socket_path}", file=sys.stderr)
        print("Is the InstanceHA service running?", file=sys.stderr)
        sys.exit(1)
    except ConnectionRefusedError:
        print(f"Error: Connection refused at {socket_path}", file=sys.stderr)
        print("The chat server may not be started yet.", file=sys.stderr)
        sys.exit(1)
    except PermissionError:
        print(f"Error: Permission denied for {socket_path}", file=sys.stderr)
        sys.exit(1)
    return sock


def recv_responses(sock: socket.socket):
    """Receive and yield parsed JSON responses from the server."""
    buf = b""
    while True:
        try:
            data = sock.recv(MAX_MESSAGE_SIZE)
        except (ConnectionResetError, OSError):
            return
        if not data:
            return

        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue


def send_query(sock: socket.socket, message: str):
    """Send a query message to the server."""
    request = json.dumps({"type": "query", "message": message}) + "\n"
    sock.sendall(request.encode("utf-8"))


def send_approval(sock: socket.socket, approval_id: str, approved: bool):
    """Send an approval/denial message to the server."""
    request = json.dumps({
        "type": "approval",
        "approval_id": approval_id,
        "approved": approved,
    }) + "\n"
    sock.sendall(request.encode("utf-8"))


def display_response(response: dict) -> bool:
    """Display a server response. Returns True if a prompt should be shown."""
    error = response.get("error")
    message = response.get("message", "")

    if error:
        print(f"\033[31mError: {error}\033[0m")
    elif message:
        # Color approval-required messages yellow
        if response.get("requires_approval"):
            print(f"\033[33m{message}\033[0m")
        else:
            print(message)

    return response.get("prompt", False)


def main():
    socket_path = os.environ.get("INSTANCEHA_SOCKET", SOCKET_PATH)

    # Handle Ctrl+C gracefully
    signal.signal(signal.SIGINT, lambda *_: None)

    sock = connect(socket_path)

    try:
        # Receive the welcome message
        for response in recv_responses(sock):
            show_prompt = display_response(response)
            if show_prompt:
                break

        # Main REPL
        while True:
            try:
                user_input = input(PROMPT)
            except (EOFError, KeyboardInterrupt):
                print()
                break

            user_input = user_input.strip()
            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit", "q"):
                send_query(sock, user_input)
                # Read final goodbye
                for response in recv_responses(sock):
                    display_response(response)
                    break
                break

            send_query(sock, user_input)

            # Read responses until we get one with prompt=True
            for response in recv_responses(sock):
                show_prompt = display_response(response)
                if show_prompt:
                    break

    except (ConnectionResetError, BrokenPipeError):
        print("\nConnection lost.", file=sys.stderr)
    finally:
        try:
            sock.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
