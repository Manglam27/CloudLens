"""Manage the CloudLensTrustedSources table: the sites CloudLens trusts and how much.

    python backend/credibility_check/sources.py list
    python backend/credibility_check/sources.py add apnews.com "Associated Press" wire_service
    python backend/credibility_check/sources.py disable cnn.com
    python backend/credibility_check/sources.py enable cnn.com
    python backend/credibility_check/sources.py set-weight bbc.com 0.8
    python backend/credibility_check/sources.py seed      add missing entries from trusted_sources.json

Categories and their default weights: fact_checker 1.0, wire_service 1.0, news_outlet 0.6.
The worker picks up changes within 5 minutes; no redeploy is needed. Needs the AWS CLI signed in.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "check_worker"))
from deploy import SOURCES_TABLE, aws  # noqa: E402
from evidence import CATEGORY_WEIGHTS, load_seed  # noqa: E402


def to_item(entry):
    return {"domain": {"S": entry["domain"]}, "name": {"S": entry["name"]}, "category": {"S": entry["category"]},
            "weight": {"N": str(entry["weight"])}, "enabled": {"BOOL": bool(entry.get("enabled", True))}}


def scan():
    items, start = [], None
    while True:
        args = ["dynamodb", "scan", "--table-name", SOURCES_TABLE]
        if start:
            args += ["--exclusive-start-key", json.dumps(start)]
        page = aws(*args)
        items += page.get("Items", [])
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def seed_missing():
    """Adds seed entries that are not in the table yet; never overwrites the team's edits."""
    existing = {i["domain"]["S"] for i in scan()}
    missing = [e for d, e in load_seed().items() if d not in existing]
    for i in range(0, len(missing), 25):  # BatchWriteItem takes at most 25 items
        batch = [{"PutRequest": {"Item": to_item(e)}} for e in missing[i:i + 25]]
        aws("dynamodb", "batch-write-item", "--request-items", json.dumps({SOURCES_TABLE: batch}))
    return len(missing)


def set_fields(domain, **fields):
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    aws("dynamodb", "update-item", "--table-name", SOURCES_TABLE, "--key", json.dumps({"domain": {"S": domain}}),
        "--update-expression", "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
        "--condition-expression", "attribute_exists(#d)",
        "--expression-attribute-names", json.dumps({**names, "#d": "domain"}),
        "--expression-attribute-values", json.dumps(values))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    command, args = argv[0], argv[1:]
    if command == "list":
        rows = sorted(scan(), key=lambda i: (i["category"]["S"], i["domain"]["S"]))
        for i in rows:
            state = "enabled " if i["enabled"]["BOOL"] else "DISABLED"
            print(f"{state} {float(i['weight']['N']):.1f}  {i['category']['S']:<13} {i['domain']['S']:<24} {i['name']['S']}")
        print(f"{len(rows)} sources")
    elif command == "add" and len(args) == 3:
        domain, name, category = args
        if category not in CATEGORY_WEIGHTS:
            sys.exit(f"category must be one of: {', '.join(CATEGORY_WEIGHTS)}")
        aws("dynamodb", "put-item", "--table-name", SOURCES_TABLE, "--item", json.dumps(to_item(
            {"domain": domain.lower(), "name": name, "category": category, "weight": CATEGORY_WEIGHTS[category]})))
        print(f"added {domain} ({category}, weight {CATEGORY_WEIGHTS[category]})")
    elif command in ("enable", "disable") and len(args) == 1:
        set_fields(args[0].lower(), enabled={"BOOL": command == "enable"})
        print(f"{command}d {args[0]}")
    elif command == "set-weight" and len(args) == 2:
        weight = float(args[1])
        if not 0 <= weight <= 1:
            sys.exit("weight must be between 0 and 1")
        set_fields(args[0].lower(), weight={"N": str(weight)})
        print(f"{args[0]} weight set to {weight}")
    elif command == "seed":
        print(f"added {seed_missing()} missing sources")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
