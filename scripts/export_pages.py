#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


REQUIRED_REPORT_KEYS = {
    "status",
    "generated_at",
    "target_count",
    "fetched_count",
    "analyzable_count",
    "coverage_pct",
    "failed_instruments",
    "signals",
    "message",
}


def export_site(report_path: Path, template_path: Path, output_dir: Path) -> tuple[Path, Path]:
    payload: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    missing = sorted(REQUIRED_REPORT_KEYS - payload.keys())
    if missing:
        raise ValueError(f"latest report missing required fields: {', '.join(missing)}")
    if not isinstance(payload["signals"], list):
        raise ValueError("latest report signals must be a list")
    if not isinstance(payload["failed_instruments"], dict):
        raise ValueError("latest report failed_instruments must be an object")

    data_dir = output_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    index_path = output_dir / "index.html"
    latest_path = data_dir / "latest.json"
    shutil.copyfile(template_path, index_path)
    for asset_name in (
        "manifest.webmanifest",
        "service-worker.js",
        "radar-icon.svg",
    ):
        asset = template_path.parent / asset_name
        if asset.exists():
            shutil.copyfile(asset, output_dir / asset_name)
    latest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / ".nojekyll").write_text("", encoding="utf-8")
    return index_path, latest_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export the latest radar report as a GitHub Pages site")
    parser.add_argument("--report", default="data/latest.json")
    parser.add_argument("--template", default="radar/static/pages.html")
    parser.add_argument("--output", default="site")
    args = parser.parse_args()
    index_path, latest_path = export_site(
        Path(args.report),
        Path(args.template),
        Path(args.output),
    )
    print(f"Exported {index_path} and {latest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
