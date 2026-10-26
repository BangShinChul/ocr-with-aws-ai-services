"""
Create an empty truth (answer) file from a schema, for a person to fill in.

    ./run.sh template scan_01.png        -> data/truth/scan_01.json from data/schema/scan_01.json
    ./run.sh template scan_01.png --rows 4   (arrays get 4 empty items)

Leave a value as null to exclude that field from scoring.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.paths import schema_path, truth_path  # noqa: E402


def skeleton(node, rows):
    kind = node.get("type")
    if kind == "object" or "properties" in node:
        return {k: skeleton(v, rows) for k, v in node.get("properties", {}).items()}
    if kind == "array":
        item = node.get("items", {})
        return [skeleton(item, rows) for _ in range(rows)] if item.get("type") == "object" or "properties" in item else []
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("document", help="Document file name in data/images (or its base name)")
    p.add_argument("--rows", type=int, default=1, help="Empty items to create for each array of objects")
    p.add_argument("--force", action="store_true", help="Overwrite an existing truth file")
    a = p.parse_args()

    src, dst = schema_path(a.document), truth_path(a.document)
    if not src.is_file():
        sys.exit(f"Schema not found: {src}")
    if dst.exists() and not a.force:
        sys.exit(f"{dst} already exists (use --force to overwrite)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(skeleton(json.loads(src.read_text(encoding="utf-8")), a.rows),
                              ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {dst} — fill in the values a person has verified against the original document.")


if __name__ == "__main__":
    main()
