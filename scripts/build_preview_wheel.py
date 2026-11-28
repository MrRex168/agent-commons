"""Build and inspect a GitHub preview wheel from one explicit Git commit."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from email.parser import BytesParser
from importlib.metadata import version
from pathlib import Path
from zipfile import ZipFile

BACKEND = "1.29.0"
REQUIRED = {
    "agent_commons/cli.py",
    "agent_commons/client.py",
    "agent_commons/main.py",
    "agent_commons/mcp_server.py",
    "agent_commons/typed_worker.py",
    "agent_commons/typed_tasks.py",
    "agent_commons/typed_handlers.py",
    "agent_commons/collaboration_history.py",
}


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def build(commit: str, output: Path) -> None:
    if not re.fullmatch(r"[a-f0-9]{40}", commit) or commit != git(
        "rev-parse", "--verify", f"{commit}^{{commit}}"
    ):
        raise ValueError("Supply the full reviewed source commit SHA")
    if version("hatchling") != BACKEND:
        raise ValueError(f"Install hatchling=={BACKEND} in the build environment")
    output = output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use an empty artifact directory")

    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        tree = scratch / "source"
        tree.mkdir()
        archive = scratch / "source.tar"
        subprocess.run(["git", "archive", "-o", str(archive), commit], check=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(tree)], check=True)
        with (tree / "pyproject.toml").open("rb") as stream:
            release = tomllib.load(stream)["project"]["version"]
        epoch = git("show", "-s", "--format=%ct", commit)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--no-index",
                "--wheel-dir",
                str(scratch),
                str(tree),
            ],
            check=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": epoch, "PYTHONHASHSEED": "0"},
        )
        (wheel,) = scratch.glob("*.whl")
        with ZipFile(wheel) as package:
            members = set(package.namelist())
            (metadata_path,) = [name for name in members if name.endswith(".dist-info/METADATA")]
            metadata = BytesParser().parsebytes(package.read(metadata_path))
            entrypoints = package.read(metadata_path.replace("METADATA", "entry_points.txt"))
            source_modules = {
                str(path.relative_to(tree / "src"))
                for path in (tree / "src/agent_commons").rglob("*.py")
            }
            if (
                metadata["Name"] != "agent-commons"
                or metadata["Version"] != release
                or wheel.name != f"agent_commons-{release}-py3-none-any.whl"
                or not (REQUIRED | source_modules) <= members
                or b"agent-commons = agent_commons.cli:main" not in entrypoints
                or b"agent-commons-mcp = agent_commons.mcp_server:main" not in entrypoints
                or any(
                    Path(member).name.startswith(".env")
                    or Path(member).suffix in {".key", ".pem", ".p12", ".pfx"}
                    for member in members
                )
            ):
                raise ValueError("Wheel contents or metadata do not match the requested source")
        payload = wheel.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        (output / wheel.name).write_bytes(payload)
        (output / "SHA256SUMS").write_text(f"{digest}  {wheel.name}\n", encoding="ascii")
        (output / "release-build.json").write_text(
            json.dumps(
                {
                    "source_commit": commit,
                    "version": release,
                    "proposed_tag": f"v{release}",
                    "wheel": wheel.name,
                    "sha256": digest,
                    "source_date_epoch": int(epoch),
                    "python": sys.version.split()[0],
                    "build_toolchain": {
                        name: version(name)
                        for name in (
                            "pip",
                            "hatchling",
                            "packaging",
                            "pathspec",
                            "pluggy",
                            "trove-classifiers",
                        )
                    },
                },
                indent=2,
            )
            + "\n",
            encoding="ascii",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    build(args.commit, args.output)


if __name__ == "__main__":
    main()
