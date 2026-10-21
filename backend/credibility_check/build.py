"""Builds the two Lambda packages into backend/credibility_check/dist/.

    python backend/credibility_check/build.py

The Check API uses only the standard library and boto3, which the AWS Lambda
Python runtime provides. The worker also bundles the packages in
check_worker/requirements.txt (the Anthropic SDK), downloaded from PyPI as
Linux builds for the Python 3.13 x86_64 runtime.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"


def install_requirements(name, target):
    requirements = HERE / name / "requirements.txt"
    if not requirements.exists():
        return
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "--requirement", str(requirements),
         "--target", str(target), "--platform", "manylinux2014_x86_64", "--implementation", "cp",
         "--python-version", "3.13", "--only-binary=:all:", "--upgrade"],
        check=True,
    )


def build(name):
    DIST.mkdir(exist_ok=True)
    staging = DIST / f"{name}_package"
    shutil.rmtree(staging, ignore_errors=True)
    install_requirements(name, staging)
    out = DIST / f"{name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((HERE / name).iterdir()):
            if path.suffix in (".py", ".json"):
                archive.write(path, path.name)
        if staging.exists():
            for path in sorted(staging.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(staging).as_posix())
    shutil.rmtree(staging, ignore_errors=True)
    print(f"Built {out} ({out.stat().st_size / 1024 / 1024:.1f} MB)")
    return out


if __name__ == "__main__":
    build("check_api")
    build("check_worker")
