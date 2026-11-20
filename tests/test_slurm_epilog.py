"""Tests for files/slurm/tesp-slurm-epilog (issue #40).

The Epilog kills the processes a job left behind. Here it runs as the
current user against processes of the current user, so no root is needed:

    python3 -m pytest tests/test_slurm_epilog.py
"""

from __future__ import annotations

import os
import shutil
import sys
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


def orphans(argv: list[str], env: dict[str, str], sibling: bool = False,
            sibling_job: str = "") -> list[int]:
    """Starts argv orphaned (its parent exits at once) in a new session and
    returns [its PID]; with sibling, also a sleep in the same session (with
    SLURM_JOB_ID=sibling_job if given) and its PID."""
    script = '"$@" >/dev/null 2>&1 & echo $!'
    if sibling:
        job = f"SLURM_JOB_ID={sibling_job} " if sibling_job else ""
        script += f"; {job}sleep 300 >/dev/null 2>&1 & echo $!"
    out = subprocess.run(
        ["setsid", "sh", "-c", f"({script})", "sh", *argv],
        env=env, check=True, capture_output=True, text=True,
    )
    pids = [int(p) for p in out.stdout.split()]
    for _ in range(50):
        if Path(f"/proc/{pids[0]}/status").exists():
            break
        time.sleep(0.02)
    return pids


def orphan(argv: list[str], env: dict[str, str]) -> int:
    return orphans(argv, env)[0]


def fake_helper(tmp_path: Path) -> Path:
    """A stand-in for Apptainer's squashfuse_ll: sleeps for argv[1] seconds.
    A script, so that its comm is its name (a copy of sleep may be a
    multi-call binary that refuses an unknown name)."""
    helper = tmp_path / "squashfuse_ll"
    helper.write_text(f"#!{sys.executable}\nimport sys, time\ntime.sleep(int(sys.argv[1]))\n")
    helper.chmod(0o755)
    return helper


def assert_helper(pid: int) -> None:
    """The fake helper runs and looks like squashfuse_ll to the Epilog."""
    for _ in range(100):
        if alive(pid) and Path(f"/proc/{pid}/comm").read_text().strip() == "squashfuse_ll":
            return
        time.sleep(0.02)
    raise AssertionError(f"fake squashfuse_ll {pid} isn't running")


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
    helper = fake_helper(tmp_path)
    pid = orphan([str(helper), "300"], env_with())
    procs.append(pid)
    ppid = Path(f"/proc/{pid}/status").read_text().split("PPid:")[1].split()[0]
    if ppid != "1":
        pytest.skip(f"orphans go to a subreaper here (PPid {ppid}), not init")

    assert_helper(pid)
    assert run_epilog("7").returncode == 0
    for _ in range(50):
        if not alive(pid):
            break
        time.sleep(0.02)
    assert not alive(pid)


def test_spares_orphaned_squashfuse_ll_with_a_live_session(procs, tmp_path):
    # Its session still has a process: that job (or container) isn't over.
    helper = fake_helper(tmp_path)
    pid, sibling = orphans([str(helper), "300"], env_with(), sibling=True)
    procs += [pid, sibling]
    ppid = Path(f"/proc/{pid}/status").read_text().split("PPid:")[1].split()[0]
    if ppid != "1":
        pytest.skip(f"orphans go to a subreaper here (PPid {ppid}), not init")

    assert_helper(pid)
    assert run_epilog("7").returncode == 0
    time.sleep(0.2)
    assert alive(pid)


def test_kills_helper_once_the_jobs_own_processes_are_gone(procs, tmp_path):
    # The helper's session still holds a process of this job: the Epilog
    # kills that first, then the helper is alone.
    helper = fake_helper(tmp_path)
    pid, sibling = orphans([str(helper), "300"], env_with(), sibling=True,
                           sibling_job="4245")
    procs += [pid, sibling]
    ppid = Path(f"/proc/{pid}/status").read_text().split("PPid:")[1].split()[0]
    if ppid != "1":
        pytest.skip(f"orphans go to a subreaper here (PPid {ppid}), not init")
    # Like Apptainer's helper, it has no SLURM_JOB_ID; the sibling has.
    assert b"SLURM_JOB_ID" not in Path(f"/proc/{pid}/environ").read_bytes()
    assert b"SLURM_JOB_ID=4245" in Path(f"/proc/{sibling}/environ").read_bytes()

    assert_helper(pid)
    assert alive(sibling)
    assert run_epilog("4245").returncode == 0
    for _ in range(100):
        if not alive(pid) and not alive(sibling):
            break
        time.sleep(0.02)
    assert not alive(sibling)
    assert not alive(pid)


def test_works_without_path(procs):
    # slurmd runs the Epilog with no PATH at all.
    pid = orphan(["sleep", "300"], env_with(SLURM_JOB_ID="4244"))
    procs.append(pid)
    result = subprocess.run(
        [str(EPILOG)], env={"SLURM_JOB_ID": "4244", "SLURM_JOB_UID": str(os.getuid())},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0
    for _ in range(50):
        if not alive(pid):
            break
        time.sleep(0.02)
    assert not alive(pid)


def test_spares_squashfuse_ll_of_a_running_container(procs, tmp_path):
    # A running container's helper is the child of Apptainer, not of init.
    helper = fake_helper(tmp_path)
    child = subprocess.Popen([str(helper), "300"], env=env_with())
    procs.append(child.pid)

    assert_helper(child.pid)
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
