"""Tests for files/slurm/tesp-slurm-epilog (issue #40).

The Epilog kills the processes a job left behind. Here it runs as the
current user against processes of the current user, so no root is needed:

    python3 -m pytest tests/test_slurm_epilog.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

EPILOG = Path(__file__).resolve().parent.parent / "files/slurm/tesp-slurm-epilog"


def alive(pid: int) -> bool:
    """True while pid exists and isn't a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


def orphan(argv: list[str], env: dict[str, str]) -> int:
    """Starts argv orphaned (its parent exits at once), returns its PID."""
    out = subprocess.run(
        ["sh", "-c", '"$@" >/dev/null 2>&1 & echo $!', "sh", *argv],
        env=env, check=True, capture_output=True, text=True,
    )
    pid = int(out.stdout)
    for _ in range(50):
        if Path(f"/proc/{pid}/status").exists():
            break
        time.sleep(0.02)
    return pid


def run_epilog(job: str, uid: str | None = None) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "SLURM_JOB_ID": job,
           "SLURM_JOB_UID": str(os.getuid()) if uid is None else uid}
    return subprocess.run([str(EPILOG)], env=env, capture_output=True,
                          text=True, timeout=60)


@pytest.fixture
def procs():
    started: list[int] = []
    yield started
    for pid in started:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


def env_with(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLURM_")}
    env.update(extra)
    return env


def test_kills_only_this_jobs_processes(procs):
    mine = orphan(["sleep", "300"], env_with(SLURM_JOB_ID="4242"))
    other_job = orphan(["sleep", "300"], env_with(SLURM_JOB_ID="42421"))
    no_job = orphan(["sleep", "300"], env_with())
    procs += [mine, other_job, no_job]

    result = run_epilog("4242")

    assert result.returncode == 0
    for _ in range(50):
        if not alive(mine):
            break
        time.sleep(0.02)
    assert not alive(mine)
    assert alive(other_job)
    assert alive(no_job)


def test_kills_orphaned_squashfuse_ll(procs, tmp_path):
    helper = tmp_path / "squashfuse_ll"
    shutil.copy(shutil.which("sleep"), helper)
    pid = orphan([str(helper), "300"], env_with())
    procs.append(pid)
    ppid = Path(f"/proc/{pid}/status").read_text().split("PPid:")[1].split()[0]
    if ppid != "1":
        pytest.skip(f"orphans go to a subreaper here (PPid {ppid}), not init")

    assert run_epilog("7").returncode == 0
    for _ in range(50):
        if not alive(pid):
            break
        time.sleep(0.02)
    assert not alive(pid)


def test_spares_squashfuse_ll_of_a_running_container(procs, tmp_path):
    # A running container's helper is the child of Apptainer, not of init.
    helper = tmp_path / "squashfuse_ll"
    shutil.copy(shutil.which("sleep"), helper)
    child = subprocess.Popen([str(helper), "300"], env=env_with())
    procs.append(child.pid)

    assert run_epilog("7").returncode == 0
    time.sleep(0.2)
    assert child.poll() is None


@pytest.mark.parametrize(
    "job, uid",
    [("", None), ("12a", None), ("1; rm -rf /", None),
     ("4243", ""), ("4243", "0"), ("4243", "x1")],
)
def test_bad_or_root_input_kills_nothing_and_exits_0(procs, job, uid):
    pid = orphan(["sleep", "300"], env_with(SLURM_JOB_ID="4243"))
    procs.append(pid)

    assert run_epilog(job, uid).returncode == 0
    time.sleep(0.2)
    assert alive(pid)
