"""Process, state, and integrity helpers; never invoke a shell."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from .model import PipelineError


def run(arguments, *, cwd=None, log=None, timeout=1800, env=None):
    if log:
        path = Path(log)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as stream:
            result = subprocess.run(
                [str(value) for value in arguments],
                cwd=cwd,
                env=env,
                text=True,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        if result.returncode:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - 6000))
                detail = stream.read().decode(errors="replace")
            raise PipelineError(
                f"Command failed ({result.returncode}): {arguments[0]}; log: {path}\n{detail}"
            )
        return ""
    result = subprocess.run(
        [str(value) for value in arguments],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise PipelineError(
            f"Command failed ({result.returncode}): {arguments[0]}\n"
            f"{result.stderr[-4000:]}\n{result.stdout[-2000:]}"
        )
    return result.stdout.strip()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=".pending-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=4, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text())


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@contextmanager
def lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "pipeline.lock").open("w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PipelineError("Another pipeline run holds this state directory") from error
        yield
