"""コマンドライン。"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from . import audit, logs
from .config import Config
from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

#: 詳細ログの置き場所（作業ディレクトリの中）
LOG_FILE_NAME = "process.log"


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
    verbosity = process.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="基準線 1 本ごとの角度など、細部まで画面に出す",
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true", help="要確認と警告だけを画面に出す"
    )

    check = subparsers.add_parser(
        "audit",
        help="Lightroom で付けた採否を読み、重複して採用した作品と全部却下した作品を出す",
    )
    check.add_argument("raw_dir", type=Path, help="ARW と XMP が入ったディレクトリ")
    check.add_argument("--config", type=Path, default=None, help="設定 JSON")
    check.add_argument(
        "--tag",
        action="store_true",
        help=(
            "見直すべき作品の写真に「確認が必要」のキーワードを付ける。"
            "見直しが済んだ写真からは外す。既定は読むだけで何も書かない"
        ),
    )

    args = parser.parse_args(argv)
    if args.command == "audit":
        return _audit(args)

    logs.setup(1 if args.verbose else -1 if args.quiet else 0)

    config = Config.load(args.config)
    if args.overwrite_xmp:
        config.xmp.on_existing = "overwrite"

    orchestrator = Orchestrator(args.raw_dir, config)
    # 画面の出し方にかかわらず、ファイルには細部まで残す
    log_path = orchestrator.workspace.root / LOG_FILE_NAME
    logs.attach_file(log_path)

    orchestrator.process()
    logger.info("詳細ログ %s", log_path)
    return 0


def _audit(args: argparse.Namespace) -> int:
    """採否を読む。作業ディレクトリもログも作らない。XMP に書くのは --tag のときだけ。"""
    config = Config.load(args.config)
    result = audit.audit(args.raw_dir, config.xmp.group_tag_prefix)
    print(audit.render(result, args.raw_dir.resolve().name))

    if args.tag:
        keyword = config.xmp.review_tag
        added, removed = audit.tag_for_review(result, keyword)
        print(f"「{keyword}」を付けた: {len(added)} 枚  外した: {len(removed)} 枚")
        if added or removed:
            print("Lightroom は動作中にサイドカーを読み直さない。再起動すると反映される")
    return 0
