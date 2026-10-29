"""The suite passes the reference agents and catches each defect a broken agent carries."""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from jaah_conformance.runner import find_spec, run_profile

SDK = Path(__file__).resolve().parents[2]
RAW = Path(__file__).with_name("agents") / "raw_agent.py"
#: Each reference agent's `command` (its jaah-agent.toml), run as a node runs it: from a copy of the agent's folder
#: alone, with the dependencies it names installed into it (`examples/vendor.py --install`, what the node does).
#: `-S -s -E` drops every site-packages and PYTHONPATH, leaving the script's own folder, so the Python agent imports
#: only what its folder holds.
REFERENCE = {
    "python": [sys.executable, "-S", "-s", "-E", "agent.py"],
    "typescript": ["node", "agent.mjs"],
}
#: What a node's bundle never holds: the install step's output and local caches.
NOT_BUNDLED = shutil.ignore_patterns("node_modules", "__pycache__")
VENDOR = SDK / "examples" / "vendor.py"


def run(profile, command):
    return asyncio.run(run_profile(profile, command, find_spec(), timeout=30))


def failed(report):
    return {c.name for c in report.checks if not c.passed}


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    """Each reference agent's folder copied alone, its dependencies installed into it as a node installs them."""
    roots = {}
    for language in REFERENCE:
        root = tmp_path_factory.mktemp("agents") / language
        shutil.copytree(SDK / "examples" / language, root, ignore=NOT_BUNDLED)
        subprocess.run([sys.executable, str(VENDOR), "--install", str(root)], check=True)
        roots[language] = root
    return roots


def bundle(language, installed, tmp_path, *, without=()):
    """A copy of the installed reference agent's folder, minus `without`; the command's paths resolved in it."""
    root = tmp_path / language
    shutil.copytree(installed[language], root)
    for name in without:
        shutil.rmtree(root / name)
    return [word if i == 0 or not (root / word).is_file() else str(root / word)
            for i, word in enumerate(REFERENCE[language])]


@pytest.mark.parametrize("language", sorted(REFERENCE))
@pytest.mark.parametrize("profile", ["acp-core", "jaah-extension"])
def test_reference_agents_pass_from_their_folder_alone(language, profile, installed, tmp_path):
    report = run(profile, bundle(language, installed, tmp_path))
    assert report.passed, report.render()


def test_a_folder_that_does_not_carry_the_sdk_fails(installed, tmp_path):
    """The control: the same Python agent with its `jaah_agent` removed cannot even start."""
    report = run("acp-core", bundle("python", installed, tmp_path, without=["jaah_agent"]))
    assert not report.passed


def test_a_folder_without_its_dependencies_fails(tmp_path):
    """The control: the Python agent as published, nothing installed into it, cannot even start."""
    root = tmp_path / "python"
    shutil.copytree(SDK / "examples" / "python", root, ignore=NOT_BUNDLED)
    report = run("acp-core", [*REFERENCE["python"][:-1], str(root / "agent.py")])
    assert not report.passed


@pytest.mark.parametrize("mode", ["ok", "other-vendor"])
def test_a_correct_raw_agent_passes(mode):
    report = run("jaah-extension", [sys.executable, str(RAW), mode])
    assert report.passed, report.render()


@pytest.mark.parametrize(
    ("mode", "check"),
    [
        ("silent", "plain ACP: the turn emits session/update"),
        ("unadvertised", "plain ACP: no jaah.ai call without the capability"),
        ("double-settle", "jaah.ai: settles exactly once"),
        ("bad-params", "jaah.ai: every call is an advertised method with schema-valid params"),
        ("no-settle", "jaah.ai: settles exactly once"),
        ("ignores-offer", "jaah.ai partial offer: every call is an advertised method with schema-valid params"),
    ],
)
def test_each_defect_fails_its_check(mode, check):
    report = run("jaah-extension", [sys.executable, str(RAW), mode])
    assert not report.passed
    assert check in failed(report), report.render()


def test_a_crashing_agent_is_a_failed_check_not_a_traceback():
    report = run("acp-core", [sys.executable, "-c", "raise SystemExit(3)"])
    assert not report.passed
    assert any("plain ACP" in name for name in failed(report))


def test_the_spec_is_found_from_a_copy_of_the_sdk(tmp_path):
    copy = tmp_path / "agent-sdk"
    shutil.copytree(SDK / "spec", copy / "spec")
    shutil.copytree(SDK / "conformance", copy / "conformance", ignore=shutil.ignore_patterns("__pycache__"))
    assert find_spec(copy / "conformance" / "jaah_conformance" / "runner.py") == copy / "spec" / "jaah-extension.json"


def test_an_installed_wheel_finds_its_bundled_spec(tmp_path):
    package = tmp_path / "site-packages" / "jaah_conformance"
    package.mkdir(parents=True)
    shutil.copy(SDK / "spec" / "jaah-extension.json", package / "jaah-extension.json")
    assert find_spec(package / "runner.py") == package / "jaah-extension.json"
