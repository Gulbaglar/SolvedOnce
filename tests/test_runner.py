"""Tests for the SolvedOnce sandbox runner and the strict patch applier.

Fast unit tests always run. End-to-end tests:
  - local-unsafe backend: run unless SOLVEDONCE_SKIP_E2E=1 (needs pip access)
  - docker backend: run only when SOLVEDONCE_DOCKER_E2E=1 (CI sets it)
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sandbox"))
sys.path.insert(0, str(ROOT / "validator"))

import apply_patch  # noqa: E402
import solvedonce_run as runner  # noqa: E402

EXAMPLE_PATH = ROOT / "examples" / "example-packet.json"
EXAMPLE = json.loads(EXAMPLE_PATH.read_text(encoding="utf-8"))
FIXTURE = ROOT / "examples" / "fixtures" / "tinycsv"


# ---------------------------------------------------------------- apply_patch


def fresh_fixture(tmp_path: Path) -> Path:
    dst = tmp_path / "pkg"
    shutil.copytree(FIXTURE, dst)
    return dst


def test_applies_example_patch(tmp_path):
    root = fresh_fixture(tmp_path)
    assert apply_patch.apply(root, EXAMPLE["fix"]["patch"]) == ["tinycsv/_fast.py"]
    assert "line.rstrip('\\r\\n')\n" in (root / "tinycsv" / "_fast.py").read_text()


def test_rejects_second_application(tmp_path):
    root = fresh_fixture(tmp_path)
    apply_patch.apply(root, EXAMPLE["fix"]["patch"])
    with pytest.raises(apply_patch.PatchError, match="context mismatch"):
        apply_patch.apply(root, EXAMPLE["fix"]["patch"])


@pytest.mark.parametrize("old,new,match", [
    ("--- a/tinycsv/_fast.py\n+++ b/tinycsv/_fast.py", "--- a/../x.py\n+++ b/../x.py", "escapes"),
    ("--- a/tinycsv/_fast.py\n+++ b/tinycsv/_fast.py", "--- /dev/null\n+++ b/tinycsv/_fast.py", "renames|creating"),
    ("--- a/tinycsv/_fast.py\n+++ b/tinycsv/_fast.py", "--- a/tinycsv/missing.py\n+++ b/tinycsv/missing.py", "does not exist"),
    ("@@ -10,2 +10,2 @@", "@@ -10,3 +10,3 @@", "line counts"),
])
def test_rejects_bad_patches(tmp_path, old, new, match):
    root = fresh_fixture(tmp_path)
    with pytest.raises(apply_patch.PatchError, match=match):
        apply_patch.apply(root, EXAMPLE["fix"]["patch"].replace(old, new))


def test_atomic_when_second_file_fails(tmp_path):
    root = fresh_fixture(tmp_path)
    before = (root / "tinycsv" / "_fast.py").read_text()
    bad_second = ("--- a/tinycsv/__init__.py\n+++ b/tinycsv/__init__.py\n"
                  "@@ -1,1 +1,1 @@\n-this line does not exist\n+x\n")
    with pytest.raises(apply_patch.PatchError):
        apply_patch.apply(root, EXAMPLE["fix"]["patch"] + bad_second)
    assert (root / "tinycsv" / "_fast.py").read_text() == before  # nothing written


def test_preserves_crlf(tmp_path):
    root = fresh_fixture(tmp_path)
    f = root / "tinycsv" / "_fast.py"
    f.write_bytes(f.read_bytes().replace(b"\n", b"\r\n"))
    apply_patch.apply(root, EXAMPLE["fix"]["patch"])
    data = f.read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


# ---------------------------------------------------------------- runner units


def test_run_argv_maps_pytest_and_rejects_other_programs():
    assert runner.run_argv(EXAMPLE)[:3] == ["python", "-m", "pytest"]
    bad = copy.deepcopy(EXAMPLE)
    bad["reproduction"]["run_command"] = "bash -c id"
    with pytest.raises(runner.RunnerError):
        runner.run_argv(bad)


def test_rejects_unsafe_test_file_name():
    with pytest.raises(runner.RunnerError):
        runner.test_file_name(["python", "-m", "pytest", "../../etc/x.py"])


@pytest.mark.parametrize("runtime,expected", [("CPython 3.13", "3.13"), ("Python 3.10.4", "3.10"), ("", "3.12")])
def test_python_version(runtime, expected):
    p = copy.deepcopy(EXAMPLE)
    p["environment"]["runtime"] = runtime
    assert runner.python_version(p) == expected


def test_python_version_rejects_unsupported():
    p = copy.deepcopy(EXAMPLE)
    p["environment"]["runtime"] = "CPython 2.7"
    with pytest.raises(runner.RunnerError):
        runner.python_version(p)


@pytest.mark.parametrize("code,timed_out,expected", [
    (0, False, "pass"), (1, False, "fail"), (2, False, "error"), (5, False, "error"), (None, True, "timeout"),
])
def test_classify(code, timed_out, expected):
    assert runner.classify(code, timed_out) == expected


def _result() -> runner.Result:
    return runner.Result(runner_version="t", result_id="r", packet_id="p", packet_sha256="s",
                         backend="test", isolated=True, environment={}, started_at="t")


def test_decide_verified():
    r = _result()
    r.before_fix = runner.RunResult(1, "fail", 0, "")
    r.after_fix = runner.RunResult(0, "pass", 0, "")
    r.patch_applied = True
    runner.decide(EXAMPLE, r)
    assert r.verdict == "verified" and r.claims_match


def test_decide_rejects_when_bug_does_not_reproduce():
    r = _result()
    r.before_fix = runner.RunResult(0, "pass", 0, "")
    r.after_fix = runner.RunResult(0, "pass", 0, "")
    r.patch_applied = True
    runner.decide(EXAMPLE, r)
    assert r.verdict == "not_verified" and not r.claims_match


def test_decide_rejects_collection_errors_as_failure():
    r = _result()
    r.before_fix = runner.RunResult(2, "error", 0, "ImportError")  # test never ran: not a real failure
    r.after_fix = runner.RunResult(0, "pass", 0, "")
    r.patch_applied = True
    runner.decide(EXAMPLE, r)
    assert r.verdict == "not_verified"


def test_decide_reproduced_only_for_workarounds():
    p = copy.deepcopy(EXAMPLE)
    p["fix"].update(kind="user_side_workaround", patch=None)
    r = _result()
    r.before_fix = runner.RunResult(1, "fail", 0, "")
    r.after_fix = runner.RunResult(None, "not_applicable", 0, "")
    runner.decide(p, r)
    assert r.verdict == "reproduced_only"


def test_refuses_invalid_packet(tmp_path):
    bad = copy.deepcopy(EXAMPLE)
    bad["reproduction"]["run_command"] = "pytest -q test_repro.py; rm -rf /"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(runner.RunnerError, match="refusing to run"):
        runner.run_packet(path, "local-unsafe", [f"tinycsv={FIXTURE}"], 60, None)


# ---------------------------------------------------------------- signing

crypto = pytest.importorskip("cryptography")


def test_sign_and_verify_roundtrip(tmp_path):
    priv, pub = runner.keygen(tmp_path)
    record = {"verdict": "verified", "packet_sha256": "abc", "nested": {"a": [1, 2]}}
    record["signature"] = runner.sign_record(record, priv.read_bytes())
    ok, _ = runner.verify_record(record, pub.read_bytes())
    assert ok


def test_tampering_breaks_signature(tmp_path):
    priv, pub = runner.keygen(tmp_path)
    record = {"verdict": "not_verified"}
    record["signature"] = runner.sign_record(record, priv.read_bytes())
    record["verdict"] = "verified"
    ok, msg = runner.verify_record(record, pub.read_bytes())
    assert not ok and "modified" in msg


def test_wrong_key_is_rejected(tmp_path):
    priv, _ = runner.keygen(tmp_path / "a")
    _, other_pub = runner.keygen(tmp_path / "b")
    record = {"verdict": "verified"}
    record["signature"] = runner.sign_record(record, priv.read_bytes())
    ok, msg = runner.verify_record(record, other_pub.read_bytes())
    assert not ok and "different key" in msg


# ---------------------------------------------------------------- end to end

E2E_LOCAL = pytest.mark.skipif(os.environ.get("SOLVEDONCE_SKIP_E2E") == "1", reason="SOLVEDONCE_SKIP_E2E=1")
E2E_DOCKER = pytest.mark.skipif(os.environ.get("SOLVEDONCE_DOCKER_E2E") != "1",
                                reason="set SOLVEDONCE_DOCKER_E2E=1 to run Docker end-to-end tests")


def _write(tmp_path: Path, packet: dict) -> Path:
    p = tmp_path / "packet.json"
    p.write_text(json.dumps(packet), encoding="utf-8")
    return p


def _wrong_patch_packet() -> dict:
    p = copy.deepcopy(EXAMPLE)
    p["fix"]["patch"] = p["fix"]["patch"].replace(
        "+    line = line.rstrip('\\r\\n')", "+    line = line.rstrip('\\r\\n' + delimiter + ' ')")
    return p


@pytest.mark.parametrize("backend", [
    pytest.param("local-unsafe", marks=E2E_LOCAL),
    pytest.param("docker", marks=E2E_DOCKER),
])
def test_e2e_example_is_verified(tmp_path, backend):
    rec = runner.run_packet(EXAMPLE_PATH, backend, [f"tinycsv={FIXTURE}"], 120, None)
    assert rec["verdict"] == "verified", rec["reasons"]
    assert rec["before_fix"]["outcome"] == "fail" and rec["after_fix"]["outcome"] == "pass"
    assert rec["isolated"] is (backend == "docker")


@pytest.mark.parametrize("backend", [
    pytest.param("local-unsafe", marks=E2E_LOCAL),
    pytest.param("docker", marks=E2E_DOCKER),
])
def test_e2e_wrong_fix_is_not_verified(tmp_path, backend):
    rec = runner.run_packet(_write(tmp_path, _wrong_patch_packet()), backend, [f"tinycsv={FIXTURE}"], 120, None)
    assert rec["verdict"] == "not_verified"
    assert rec["after_fix"]["outcome"] == "fail"


@pytest.mark.parametrize("backend", [
    pytest.param("local-unsafe", marks=E2E_LOCAL),
    pytest.param("docker", marks=E2E_DOCKER),
])
def test_e2e_lying_packet_is_caught(tmp_path, backend):
    # The packet claims the bug exists, but the package it runs against is already fixed.
    fixed = fresh_fixture(tmp_path)
    apply_patch.apply(fixed, EXAMPLE["fix"]["patch"])
    rec = runner.run_packet(EXAMPLE_PATH, backend, [f"tinycsv={fixed}"], 120, None)
    assert rec["verdict"] == "not_verified"
    assert any("before fix" in r for r in rec["reasons"])


@E2E_DOCKER
def test_e2e_docker_test_has_no_network(tmp_path):
    p = copy.deepcopy(EXAMPLE)
    p["reproduction"]["test_code"] = (
        "import socket\n\n"
        "def test_network_is_blocked():\n"
        "    s = socket.socket()\n"
        "    s.settimeout(3)\n"
        "    try:\n"
        "        s.connect(('1.1.1.1', 53))\n"
        "    except OSError:\n"
        "        raise AssertionError('network blocked, as expected')\n"
    )
    rec = runner.run_packet(_write(tmp_path, p), "docker", [f"tinycsv={FIXTURE}"], 60, None)
    assert rec["before_fix"]["outcome"] == "fail"
    assert "network blocked" in rec["before_fix"]["output_excerpt"]
