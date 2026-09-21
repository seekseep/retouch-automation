"""コマンドライン。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Config
from .orchestrator import Orchestrator


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="retouch-automation",
        description="生花作品の RAW を一括処理し、各 RAW と同名の XMP を書き出す",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    process = subparsers.add_parser("process", help="RAW ディレクトリを一括処理する")
    process.add_argument("raw_dir", type=Path, help="ARW が入ったディレクトリ")
    process.add_argument("--config", type=Path, default=None, help="設定 JSON")
    process.add_argument(
        "--overwrite-xmp",
        action="store_true",
        help=(
            "既存の XMP を丸ごと置き換える。"
            "既定は skip で、既存があれば触らない（人の手直しを守るため）"
        ),
    )

    args = parser.parse_args(argv)

    config = Config.load(args.config)
    if args.overwrite_xmp:
        config.xmp.on_existing = "overwrite"

    summary = Orchestrator(args.raw_dir, config).process()
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0
