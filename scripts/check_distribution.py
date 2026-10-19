"""Fresh locked wheel/sdist/complete source, full and production-only qualification."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

from package_source import build

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
UV = os.environ.get("UV") or shutil.which("uv")
if not UV:
    raise RuntimeError("uv is required")
ENV = {
    k: os.environ[k]
    for k in (
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "SystemRoot",
        "WINDIR",
        "UV_CACHE_DIR",
    )
    if k in os.environ
}
ENV["UV"] = UV


def run(command, cwd, timeout=240):
    result = subprocess.run(
        list(map(str, command)),
        cwd=cwd,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed {command[0]}\n{result.stdout[-16000:]}\n{result.stderr[-8000:]}"
        )
    return result.stdout


def extract(path, destination):
    destination.mkdir()
    with tarfile.open(path) as archive:
        assert all(m.isfile() or m.isdir() for m in archive.getmembers())
        archive.extractall(destination, filter="data")
    (source,) = destination.iterdir()
    return source


def verify(source):
    manifest = json.loads((source / "source-manifest.json").read_text())
    assert {str(p.relative_to(source)) for p in source.rglob("*") if p.is_file()} == {
        r["path"] for r in manifest["files"]
    } | {"source-manifest.json"}
    for row in manifest["files"]:
        path = source / row["path"]
        assert (
            path.stat().st_size == row["bytes"]
            and path.stat().st_mode & 0o777 == row["mode"]
        )
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return manifest


def exercise(python, source, base, production=False):
    options = ["--production-only"] if production else []
    return json.loads(
        run(
            [python, "-I", source / "scripts/exercise_installed.py", base, *options],
            base.parent,
        )
    )


def main():
    bundle = build()
    wheel = (
        ROOT / "dist" / f"infrastructure_operations_agent-{VERSION}-py3-none-any.whl"
    )
    sdist = ROOT / "dist" / f"infrastructure_operations_agent-{VERSION}.tar.gz"
    proofs = {}
    with tempfile.TemporaryDirectory(
        prefix="infrastructure-distribution-"
    ) as directory:
        temp = Path(directory)
        assert not temp.is_relative_to(ROOT)
        assert build(temp / "repeat.tar.gz")["sha256"] == bundle["sha256"]
        with ZipFile(wheel) as archive:
            assert (
                archive.read("infra_ops/data/starter.json")
                == (ROOT / "fixtures/starter.json").read_bytes()
            )
            assert (
                archive.read("infra_ops/data/populations.json")
                == (ROOT / "src/infra_ops/data/populations.json").read_bytes()
            )
        requirements = temp / "production.txt"
        requirements.write_text(
            run(
                [
                    UV,
                    "export",
                    "--locked",
                    "--no-dev",
                    "--no-emit-project",
                    "--format",
                    "requirements-txt",
                ],
                ROOT,
            )
        )
        assert "--hash=sha256:" in requirements.read_text()

        def production_env(name, distribution):
            environment = temp / (name + "-env")
            run([UV, "venv", "--python", sys.executable, environment], temp)
            python = environment / "bin/python"
            run(
                [
                    UV,
                    "pip",
                    "sync",
                    "--python",
                    python,
                    "--require-hashes",
                    requirements,
                ],
                temp,
            )
            run(
                [UV, "pip", "install", "--python", python, "--no-deps", distribution],
                temp,
            )
            return python

        python = production_env("wheel", wheel)
        proofs["wheel_production"] = exercise(python, ROOT, temp / "wheel-case", True)
        source = extract(sdist, temp / "sdist")
        for name in (
            "uv.lock",
            "src/infra_ops/data/populations.json",
            "tests/test_native_failures.py",
            "tests/test_populations.py",
            "scripts/check_distribution.py",
            ".github/workflows/check.yml",
            "COMPATIBILITY.md",
            "scenarios/README.md",
        ):
            assert (source / name).is_file(), name
        run(
            [sys.executable, source / "scripts/build_populations.py", "--check"], source
        )
        python = production_env("sdist", source)
        proofs["sdist_production"] = exercise(python, source, temp / "sdist-case", True)
        complete = extract(bundle["path"], temp / "complete")
        manifest = verify(complete)
        run(
            [
                UV,
                "sync",
                "--locked",
                "--extra",
                "test",
                "--no-editable",
                "--project",
                complete,
            ],
            temp,
        )
        assert (complete/'firedrill/scenario.json').exists() and not (complete/'firedrill/node_modules').exists()
        run(['npm', 'ci', '--prefix', 'firedrill', '--ignore-scripts', '--no-audit', '--no-fund'], complete)
        run(['npm', 'test', '--prefix', 'firedrill'], complete)
        python = complete / ".venv/bin/python"
        run([python, "scripts/build_populations.py", "--check"], complete)
        proofs["complete_full"] = exercise(python, complete, temp / "complete-case")
        suite = run([python, "-m", "pytest", "-q"], complete)
        assert "130 passed" in suite, suite
        (ROOT / "dist/fresh-complete-pytest.log").write_text(suite)
        python = production_env("complete-production", complete)
        proofs["complete_production"] = exercise(
            python, complete, temp / "complete-production-case", True
        )
        for row in manifest["files"]:
            path = complete / row["path"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"], row[
                "path"
            ]
        assert build(temp / "final-repeat.tar.gz")["sha256"] == bundle["sha256"]
    report = {
        **bundle,
        "checks": "fresh locked wheel/sdist/complete source and production-only; all12 actual private CLI cases/reset; actual installed SDK/HTTP/CLI recovery, missing key, three signals, failed final after commit; fresh130check suite; unchanged source manifest; repeated portable archive bytes",
        "environments": proofs,
    }
    (ROOT / "dist/qualification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
