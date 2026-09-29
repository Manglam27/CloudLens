"""Builds the two Lambda packages into backend/credibility_check/dist/.

    python backend/credibility_check/build.py

Both functions use only the Python standard library and boto3, which the
AWS Lambda Python runtime already provides, so nothing is downloaded.
"""

import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"


def build(name):
    DIST.mkdir(exist_ok=True)
    out = DIST / f"{name}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted((HERE / name).glob("*.py")):
            archive.write(path, path.name)
    print(f"Built {out}")
    return out


if __name__ == "__main__":
    build("check_api")
    build("check_worker")
