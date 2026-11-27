"""Fixed local handlers, invoked only in a restricted child process."""

import ctypes
import json
import os
import resource
import signal
import sys

MAX_BYTES = 8192


def isolate() -> None:
    if sys.platform != "linux":
        raise RuntimeError("Linux seccomp is required")
    lib = ctypes.CDLL("libseccomp.so.2")
    for limit, maximum in (
        (resource.RLIMIT_AS, 128 * 1024 * 1024),
        (resource.RLIMIT_CPU, 2),
        (resource.RLIMIT_FSIZE, MAX_BYTES),
        (resource.RLIMIT_NOFILE, 3),
        (resource.RLIMIT_CORE, 0),
    ):
        resource.setrlimit(limit, (maximum, maximum))
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    policy = lib.seccomp_init(0x80000000)  # SCMP_ACT_KILL_PROCESS
    if not policy:
        raise RuntimeError("Cannot initialize seccomp")
    try:
        # No open, exec, socket, fork, ioctl, or filesystem mutation syscalls.
        for syscall in (
            "read",
            "write",
            "close",
            "brk",
            "mmap",
            "munmap",
            "mremap",
            "mprotect",
            "madvise",
            "rt_sigreturn",
            "exit",
            "exit_group",
        ):
            number = lib.seccomp_syscall_resolve_name(syscall.encode("ascii"))
            if number >= 0 and lib.seccomp_rule_add(policy, 0x7FFF0000, number, 0) != 0:
                raise RuntimeError("Cannot set seccomp rule")
        if lib.seccomp_load(policy) != 0:
            raise RuntimeError("Cannot enforce seccomp")
    finally:
        lib.seccomp_release(policy)


def run(skill: str, data: dict) -> dict:
    if skill == "analysis.summary@1":
        values = data["values"]
        return {
            "count": len(values),
            "total": sum(values),
            "minimum": min(values),
            "maximum": max(values),
        }
    if skill == "report.markdown@1":
        # Produce a text artifact without turning hostile titles into raw HTML.
        title = "".join(c if c.isalnum() or c in " -_" else " " for c in data["title"])
        summary = data["summary"]
        metrics = "\n".join(
            f"- {key.title()}: {summary[key]}" for key in ("count", "total", "minimum", "maximum")
        )
        return {"markdown": f"# {title}\n\n{metrics}"}
    raise ValueError("Unknown handler")


def main() -> None:
    # Terminate if the supervisor exits, including an unhandled process crash.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != int(sys.argv[2]):
        os._exit(1)
    isolate()
    skill = sys.argv[1]
    if skill == "--check":
        os.write(1, b"isolated\n")
        os._exit(0)
    raw = bytearray()
    while chunk := os.read(0, MAX_BYTES + 1 - len(raw)):
        raw.extend(chunk)
        if len(raw) > MAX_BYTES:
            os._exit(1)
    result = json.dumps(run(skill, json.loads(raw)), ensure_ascii=False).encode()
    if len(result) > MAX_BYTES:
        raise ValueError("Output exceeds limit")
    os.write(1, result)
    os._exit(0)


if __name__ == "__main__":
    main()
