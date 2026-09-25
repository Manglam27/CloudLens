"""Build photo_edits.zip for the AWS Lambda Python 3.13 (x86_64) runtime.

Run from any directory:
    python backend/photo_edits/build.py

Pillow contains compiled code, so this downloads the Linux build that Lambda
needs rather than the one for the machine running the script. The zip is
written with forward-slash paths, which Lambda requires.
"""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_DIR = HERE / "package"
ZIP_PATH = HERE / "photo_edits.zip"


def main():
    shutil.rmtree(PACKAGE_DIR, ignore_errors=True)
    ZIP_PATH.unlink(missing_ok=True)

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--requirement", str(HERE / "requirements.txt"),
            "--target", str(PACKAGE_DIR),
            "--platform", "manylinux2014_x86_64",
            "--implementation", "cp",
            "--python-version", "3.13",
            "--only-binary=:all:",
            "--upgrade",
        ],
        check=True,
    )
    shutil.copy2(HERE / "lambda_function.py", PACKAGE_DIR / "lambda_function.py")

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(PACKAGE_DIR).as_posix())

    shutil.rmtree(PACKAGE_DIR)
    print(f"Built {ZIP_PATH} ({ZIP_PATH.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
