"""NOX_KIT 2026-11-18d — read-only project status, no application imports."""
from __future__ import annotations
import argparse
import csv
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def status(root):
    root = Path(root)
    path = root / "work/segments.csv"
    if not path.is_file():
        return {"root": str(root), "prepared": False, "note": "Книга ещё не подготовлена"}
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    from nox_freeze import read_protection
    state = read_protection(root, rows, check_audio=False)
    return {"root": str(root), "prepared": True, "phrases": len(rows),
            "fixed_parts": len(state.ranges), "protected_phrases": len(state.protected_indices),
            "draft_wavs": sum(1 for p in (root / "work/wav").glob("*.wav") if p.is_file()),
            "read_only": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    try:
        value = status(args.root)
        if args.json:
            print(json.dumps(value, ensure_ascii=False))
        elif value["prepared"]:
            print(f"Книга: {value['root']}")
            print(f"Фраз: {value['phrases']}; закреплённых частей: {value['fixed_parts']}; защищено фраз: {value['protected_phrases']}")
            print(f"Файлов черновой озвучки: {value['draft_wavs']} (это число файлов, не число проверенных фраз).")
        else:
            print(value["note"])
        return 0
    except Exception as exc:
        print("Статус не определён: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
