"""Supervised, single-slot worker for side-effect-free collaboration tasks."""

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryFile
from threading import Timer
from time import monotonic, sleep

from sqlalchemy.exc import SQLAlchemyError

from agent_commons.collaboration import MAX_OUTPUT, validate_typed_output
from agent_commons.config import settings
from agent_commons.typed_tasks import Claim, authorize_start, claim, current, finish, sweep

RUNNER = Path(__file__).with_name("typed_handlers.py")
CHECK_INTERVAL = 0.1


def _command(skill: str) -> list[str]:
    return [sys.executable, "-I", "-S", str(RUNNER), skill, str(os.getpid())]


def check_isolation() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Typed worker requires Linux seccomp")
    completed = subprocess.run(
        _command("--check"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={},
        close_fds=True,
        timeout=3,
        check=True,
    )
    if completed.stdout != b"isolated\n":
        raise RuntimeError("Typed worker isolation is unavailable")


def execute(claimed: Claim, is_current: Callable[[Claim], bool] = current) -> dict | None:
    seconds = (claimed.deadline - datetime.now(UTC)).total_seconds()
    if seconds <= 0 or not is_current(claimed):
        return None
    deadline = monotonic() + seconds
    raw = json.dumps(claimed.data, ensure_ascii=False, separators=(",", ":")).encode()
    with TemporaryFile() as source, TemporaryFile() as output:
        source.write(raw)
        source.seek(0)
        with subprocess.Popen(
            _command(claimed.skill),
            stdin=source,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
        ) as process:
            timer = Timer(max(0, deadline - monotonic()), process.kill)
            timer.start()
            try:
                while (remaining := deadline - monotonic()) > 0:
                    try:
                        status = process.wait(timeout=min(CHECK_INTERVAL, remaining))
                        if status != 0 or monotonic() >= deadline:
                            return None
                        output.seek(0)
                        data = output.read(MAX_OUTPUT + 1)
                        if len(data) > MAX_OUTPUT:
                            return None
                        return validate_typed_output(claimed.skill, json.loads(data))
                    except subprocess.TimeoutExpired:
                        if not is_current(claimed):
                            return None
                    except (ValueError, RecursionError):
                        return None
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
    return None


def run_once() -> bool:
    sweep()
    claimed = claim()
    if claimed is None:
        return False
    if not authorize_start(claimed):
        return True
    try:
        outcome = execute(claimed)
    except OSError:
        outcome = None
    finish(claimed, outcome)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not settings.collaboration_execution:
        print("Collaboration execution is disabled.", file=sys.stderr)
        return 1
    try:
        check_isolation()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        print("Required Linux seccomp isolation is unavailable.", file=sys.stderr)
        return 1
    while True:
        try:
            active = run_once()
        except SQLAlchemyError:
            # Completion could not be committed. Never return the claim to
            # submitted. A later sweep expires and fences the attempt.
            if args.once:
                return 1
            active = False
        if args.once:
            return 0
        if not active:
            sleep(0.25)


if __name__ == "__main__":
    sys.exit(main())
