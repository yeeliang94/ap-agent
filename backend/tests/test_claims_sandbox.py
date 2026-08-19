"""H8 — the SandboxPort: disabled unless an OS-level runner is declared and
isolation asserted; the production adapter's own controls (cleared env,
read-only inputs, empty output dir, wall-time kill of the process tree,
output caps, second-run determinism check, redacted audit record) hold
even with a plain-Python runner in the tests; scenario J: abuse and
timeouts are killed, audited, and leave no partial output; the harness
and the investigator turn limits into SANDBOX_LIMIT, never a run failure.
"""
from __future__ import annotations

import os
import sys

import pytest

from app import config
from app.claims.investigator import contracts as C
from app.claims.investigator import investigator as inv
from app.claims.investigator.proposal import InvestigationProposal
from app.claims.tools import sandbox as sbx
from app.claims.tools.contracts import SandboxLimits, SandboxPort
from app.claims.tools.fake import InMemoryTools
from app.claims.tools.harness import ToolHarness

posix_only = pytest.mark.skipif(os.name != "posix", reason="process-group and rlimit mechanics are POSIX here")


def test_default_is_unavailable_and_says_why(monkeypatch):
    monkeypatch.setattr(config, "CLAIMS_SANDBOX_RUNNER", "")
    s = sbx.production_sandbox()
    ok, why = s.available()
    assert not ok and "runner" in why
    monkeypatch.setattr(config, "CLAIMS_SANDBOX_RUNNER", "docker run --rm sandbox-image")
    monkeypatch.setattr(config, "CLAIMS_SANDBOX_ISOLATED", False)
    s = sbx.production_sandbox()
    assert isinstance(s, sbx.RunnerSandbox) and isinstance(s, SandboxPort)
    ok, why = s.available()
    assert not ok and "asserted" in why


@pytest.mark.asyncio
async def test_unavailable_never_runs(tmp_path):
    s = sbx.UnavailableSandbox()
    r = await s.run("print(1)", {}, tmp_path / "out", SandboxLimits())
    assert not r.ok and r.error_code == "TOOL_UNAVAILABLE"
    assert not (tmp_path / "out").exists()


@pytest.fixture()
def local_runner():
    """The mechanics under test with a plain-Python 'runner' — declared
    isolated for the test only. Nothing here is a claim that a bare
    interpreter isolates."""
    return sbx.RunnerSandbox(f'"{sys.executable}"', isolated=True)


@posix_only
@pytest.mark.asyncio
async def test_a_clean_run_is_deterministic_recorded_and_sealed(local_runner, tmp_path):
    src = tmp_path / "in.txt"
    src.write_text("24.00\n26.50\n45.00\n")
    out = tmp_path / "out"
    code = (
        "import os, sys\n"
        "d = os.environ['CLAIMS_SANDBOX_INPUT']\n"
        "vals = [l.strip() for l in open(os.path.join(d, 'A', 'in.txt')) if l.strip()]\n"
        "from decimal import Decimal\n"
        "print(sum(Decimal(v) for v in vals))\n"
        "open('total.txt', 'w').write(str(sum(Decimal(v) for v in vals)))\n"
        "print('KEY', os.environ.get('OPENAI_API_KEY', 'absent'), 'card 1234567890123456')\n"
        "try:\n"
        "    open(os.path.join(d, 'A', 'in.txt'), 'a').write('x')\n"
        "    print('WROTE INPUT')\n"
        "except PermissionError:\n"
        "    print('input read-only')\n"
    )
    r = await local_runner.run(code, {"A/in.txt": src}, out, SandboxLimits(wall_seconds=20))
    assert r.ok, r.error
    assert "95.50" in r.stdout and "input read-only" in r.stdout
    assert "KEY absent" in r.stdout            # the environment carries no secrets
    assert "1234567890123456" not in r.stdout and "<number>" in r.stdout  # redacted record
    assert r.output_files == {"total.txt": r.output_files["total.txt"]} and (out / "total.txt").read_text() == "95.50"
    assert r.output_hash and r.versions["python"] and r.exit_status == 0
    assert src.read_text() == "24.00\n26.50\n45.00\n"  # the snapshot is untouched
    # Same code, same inputs → same output hash (the record replays).
    r2 = await local_runner.run(code, {"A/in.txt": src}, tmp_path / "out2", SandboxLimits(wall_seconds=20))
    assert r2.output_hash == r.output_hash


@posix_only
@pytest.mark.asyncio
async def test_timeout_kills_the_tree_and_keeps_nothing(local_runner, tmp_path):
    out = tmp_path / "out"
    code = "open('partial.txt','w').write('half')\nimport time\nwhile True:\n    time.sleep(0.05)\n"
    r = await local_runner.run(code, {}, out, SandboxLimits(wall_seconds=1))
    assert not r.ok and r.killed and r.limit_hit == "wall" and r.error_code == "SANDBOX_LIMIT"
    assert list(out.iterdir()) == []   # partial output wiped
    assert r.elapsed_ms >= 900


@posix_only
@pytest.mark.asyncio
async def test_output_cap_and_nondeterminism_fail_closed(local_runner, tmp_path):
    r = await local_runner.run("print('x' * 5000)", {}, tmp_path / "o1", SandboxLimits(max_output_bytes=1000))
    assert not r.ok and r.limit_hit == "output" and r.error_code == "SANDBOX_LIMIT"
    r = await local_runner.run("open('big.bin','wb').write(b'0' * 4000)", {}, tmp_path / "o2", SandboxLimits(max_output_bytes=1000))
    assert not r.ok and r.limit_hit == "output" and list((tmp_path / "o2").iterdir()) == []
    r = await local_runner.run("import random\nprint(random.random())", {}, tmp_path / "o3", SandboxLimits())
    assert not r.ok and "not produce the same output" in r.error
    r = await local_runner.run("raise SystemExit(3)", {}, tmp_path / "o4", SandboxLimits())
    assert not r.ok and r.exit_status == 3 and r.error_code == "TOOL_FAILED"
    big = tmp_path / "big.txt"
    big.write_bytes(b"0" * 2000)
    r = await local_runner.run("print(1)", {"big.txt": big}, tmp_path / "o5", SandboxLimits(max_input_bytes=100))
    assert not r.ok and r.limit_hit == "input"


@posix_only
@pytest.mark.asyncio
async def test_harness_and_investigator_surface_sandbox_limits(local_runner, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "files").mkdir(parents=True)
    (ws / "files" / "x.txt").write_text("hi")
    manifest = [C.ManifestEntry(id="a1", path="x.txt", media_type="other", sha256="h", size=2)]
    tools = ToolHarness(ws, manifest, sandbox=local_runner, python_enabled=True, sandbox_limits=SandboxLimits(wall_seconds=1))
    r = await tools.run_python("import time\nwhile True: time.sleep(0.05)", ["a1"])
    assert not r.ok and r.error_code == "SANDBOX_LIMIT"
    ok = await tools.run_python("import os\nprint(open(os.path.join(os.environ['CLAIMS_SANDBOX_INPUT'],'x.txt')).read())", ["a1"])
    assert ok.ok and ok.data["stdout"].strip() == "hi" and ok.provenance["artifact_ids"] == ["a1"]
    assert (ws / "tool_output").is_dir()
    # A limit becomes a SANDBOX_LIMIT flag on the result, not a run failure.
    req = C.InvestigationRequest(run_id="r", workspace=str(ws), manifest=manifest)
    result = inv.normalize(InvestigationProposal(), req, tools, [], "full_dump", 1)
    assert [f.code for f in result.flags if f.code == "SANDBOX_LIMIT"] == ["SANDBOX_LIMIT"]
    # Off switch: not bound, not available.
    tools_off = ToolHarness(ws, manifest, sandbox=local_runner, python_enabled=False)
    assert (await tools_off.run_python("print(1)", [])).error_code == "TOOL_UNAVAILABLE"
    fake = InMemoryTools(manifest)
    assert (await fake.run_python("print(1)", [])).error_code == "TOOL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_in_memory_sandbox_records_calls(tmp_path):
    s = sbx.InMemorySandbox([sbx.SandboxResult(ok=False, error_code="SANDBOX_LIMIT", limit_hit="memory", killed=True)])
    r = await s.run("x", {}, tmp_path / "o", SandboxLimits())
    assert not r.ok and r.limit_hit == "memory" and s.calls[0]["code"] == "x"
    r = await s.run("y", {}, tmp_path / "o", SandboxLimits())
    assert r.ok and r.output_hash
    assert sbx.redact("token sk-abcdefghijklmnop and 12345678901") == "<token> and <number>"


@posix_only
@pytest.mark.asyncio
async def test_output_is_streamed_cancel_kills_and_failures_keep_nothing(local_runner, tmp_path):
    """A child that floods stdout is killed once the cap is passed — the
    host keeps at most the cap, never the flood; a running child is killed
    by cancel() (the harness's cancel reaches it); a failed or
    non-deterministic second run leaves nothing of the first behind."""
    import asyncio
    import time

    # 1. flood: 200 MB would be written; the child is killed at the cap and
    #    well inside the wall time — nothing is buffered past the cap.
    flood = "import sys\nfor _ in range(200):\n    sys.stdout.write('x' * (1 << 20)); sys.stdout.flush()\n"
    t0 = time.monotonic()
    r = await local_runner.run(flood, {}, tmp_path / "f", SandboxLimits(max_output_bytes=4096, wall_seconds=30))
    assert not r.ok and r.limit_hit == "output" and r.killed and len(r.stdout) <= 2000
    assert time.monotonic() - t0 < 10
    # 2. cancel: a child that would live 30 s is killed at once
    sleeper = "import time\nopen('partial.txt','w').write('x')\ntime.sleep(30)\n"
    task = asyncio.create_task(local_runner.run(sleeper, {}, tmp_path / "c", SandboxLimits(wall_seconds=30)))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if local_runner._live:
            break
    assert local_runner._live, "the child never started"
    tools = ToolHarness(tmp_path, [], sandbox=local_runner, python_enabled=True)
    tools.cancel()  # the harness cancel reaches the running sandbox child
    t0 = time.monotonic()
    r = await task
    assert time.monotonic() - t0 < 10
    assert not r.ok and "cancelled" in r.error and list((tmp_path / "c").iterdir()) == []
    assert (await local_runner.run("print(1)", {}, tmp_path / "after", SandboxLimits())).error == "the investigation was cancelled"
    # 3. non-determinism: the first run's files are wiped too
    fresh = sbx.RunnerSandbox(f'"{sys.executable}"', isolated=True)
    nondet = "import random\nopen('r.txt','w').write(str(random.random()))\n"
    r = await fresh.run(nondet, {}, tmp_path / "n", SandboxLimits())
    assert not r.ok and "nothing kept" in r.error and r.output_files == {} and list((tmp_path / "n").iterdir()) == []


@posix_only
@pytest.mark.asyncio
async def test_a_symlink_in_the_output_directory_refuses_the_run(local_runner, tmp_path):
    """Review #10: `out/` was walked with `p.is_file()` / `p.stat()`, which
    FOLLOW symlinks — a script that dropped a link to any file the host can
    read got that file stat'ed, hashed, and its sha256 handed back to the
    model as if the sandbox had produced it. `out/` must hold regular files
    and nothing else: a link (to a host file, to a directory, or dangling),
    a special file, or a path escaping out/ refuses the whole run and keeps
    nothing."""
    secret = tmp_path / "host_secret.txt"
    secret.write_text("an arbitrary readable file on the host")

    scripts = {
        "file link": f"import os\nos.symlink({str(secret)!r}, 'looks_like_output.txt')\n",
        "dir link": f"import os\nos.symlink({str(tmp_path)!r}, 'peek')\n",
        "dangling link": "import os\nos.symlink('/no/such/file', 'gone.txt')\n",
        "fifo": "import os\nos.mkfifo('pipe')\n",
    }
    for what, code in scripts.items():
        out = tmp_path / f"out-{what.replace(' ', '-')}"
        r = await local_runner.run(code, {}, out, SandboxLimits())
        assert not r.ok, what
        assert r.error_code == "TOOL_FAILED" and "nothing kept" in r.error, (what, r.error)
        assert "symbolic link" in r.error or "special file" in r.error, (what, r.error)
        assert r.output_files == {}, what
        assert list(out.iterdir()) == [], what
        assert secret.read_text() == "an arbitrary readable file on the host"

    # a run that writes an ordinary file is still fine
    out = tmp_path / "ok"
    r = await local_runner.run("open('real.txt','w').write('x')\n", {}, out, SandboxLimits())
    assert r.ok and list(r.output_files) == ["real.txt"]


@posix_only
@pytest.mark.asyncio
async def test_a_bare_sigkill_is_not_reported_as_memory(local_runner, tmp_path):
    """A SIGKILL used to be reported as the MEMORY limit — but the OOM
    killer, the operator, the runner and an unnamed container limit all
    end that way. Memory is claimed only with evidence in stderr."""
    killed = "import os, signal\nos.kill(os.getpid(), signal.SIGKILL)\n"
    r = await local_runner.run(killed, {}, tmp_path / "k", SandboxLimits())
    assert not r.ok and r.killed and r.limit_hit == "" and r.error_code == "TOOL_FAILED"
    assert "no limit evidence" in r.error and "memory" not in r.error

    oom = "import sys\nsys.stderr.write('MemoryError\\n')\nsys.exit(1)\n"
    r = await local_runner.run(oom, {}, tmp_path / "m", SandboxLimits())
    assert not r.ok and r.limit_hit == "memory" and r.error_code == "SANDBOX_LIMIT"

    # a plain non-zero exit is neither a kill nor a limit — and an error
    # message that merely CONTAINS "oom" ("boom", "room", "zoom") is not
    # evidence of memory pressure
    r = await local_runner.run("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n", {}, tmp_path / "e", SandboxLimits())
    assert not r.ok and not r.killed and r.limit_hit == "" and "exit status 3" in r.error
    assert sbx._limit_from_exit(-9, "boom in the zoom room") == ("killed", True)
    assert sbx._limit_from_exit(-9, "MemoryError") == ("memory", True)
    assert sbx._limit_from_exit(137, "Killed process 1 (python) OOM") == ("memory", True)
    assert sbx._limit_from_exit(0, "") == ("", False)


@posix_only
@pytest.mark.asyncio
async def test_the_input_tree_is_read_only(local_runner, tmp_path):
    """`in/` holds read-only COPIES in read-only directories: a script
    cannot rewrite an input, and cannot add, rename or delete beside it."""
    src = tmp_path / "claim.txt"
    src.write_text("RM 24.00")
    code = (
        "import os, pathlib\n"
        "d = pathlib.Path(os.environ['CLAIMS_SANDBOX_INPUT'])\n"
        "results = []\n"
        "for what, fn in (('rewrite', lambda: (d/'claim.txt').write_text('RM 999.00')),\n"
        "                 ('create', lambda: (d/'new.txt').write_text('x')),\n"
        "                 ('delete', lambda: (d/'claim.txt').unlink())):\n"
        "    try:\n"
        "        fn(); results.append(what + ':ALLOWED')\n"
        "    except OSError:\n"
        "        results.append(what + ':refused')\n"
        "open('r.txt','w').write(' '.join(results))\n"
    )
    out = tmp_path / "o"
    r = await local_runner.run(code, {"claim.txt": src}, out, SandboxLimits())
    assert r.ok, r.error
    assert (out / "r.txt").read_text() == "rewrite:refused create:refused delete:refused"
    assert src.read_text() == "RM 24.00"
