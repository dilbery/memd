import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _data():
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_requires_python_pins_313_not_314():
    rp = _data()["project"]["requires-python"]
    assert rp == ">=3.13,<3.14", rp


def test_mcp_and_test_deps_declared():
    deps = " ".join(_data()["project"].get("dependencies", []))
    assert "mcp" in deps, "MCP server dependency missing"
    optional = _data()["project"].get("optional-dependencies", {})
    devdeps = " ".join(optional.get("dev", []))
    assert "pytest-httpx" in devdeps, "pytest-httpx (hermetic HTTP) missing from dev extras"


def test_mem_mcp_console_script_declared():
    scripts = _data()["project"].get("scripts", {})
    assert scripts.get("mem-mcp") == "memd.mcp:main", scripts
