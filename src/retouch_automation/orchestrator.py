"""T8 オーケストレーター。

実行順序はツール間の依存で決まる。

    T1（全写真） → T3（全写真） → T2（1 回・全写真横断） → T4 → T5 → T6 → T7

T2 は作品領域を使うので T3 の後に置く。T2 だけが全写真を横断する。
1 枚の失敗で他を止めない。失敗した写真も可能な範囲で XMP を生成する。
ただし T1 が失敗した写真は XMP を作らず、失敗として扱う。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import logs, report
from .config import Config
from .models import PhotoResult, Status
from .tools.artwork_detector import ArtworkDetector
from .tools.classifier import Classifier
from .tools.cropper import Cropper
from .tools.developer import Developer
from .tools.raw_loader import RawLoader, find_raw_files
from .tools.straightener import Straightener
from .tools.xmp_writer import XmpWriter, xmp_path_for
from .workspace import Workspace

logger = logging.getLogger(__name__)

#: 既存の XMP をどう扱うか。ログで何が起きたかを言葉にする
XMP_ACTIONS = {
    "skip": "既存の XMP を残してスキップ",
    "merge": "既存の XMP に差し込み",
    "overwrite": "既存の XMP を置き換え",
}


class Orchestrator:
    def __init__(self, raw_dir: Path, config: Config) -> None:
        self.raw_dir = raw_dir
        self.config = config
        self.workspace = Workspace(raw_dir, config.work_dir_name)

        self.raw_loader = RawLoader(config.preview)
        self.detector = ArtworkDetector(config.detection, config.models)
        self.classifier = Classifier(
            config.xmp.base_tags,
            config.xmp.group_tag_prefix,
            config.classify,
            config.models,
        )
        self.straightener = Straightener(config.straighten)
        self.cropper = Cropper(config.crop)
        self.developer = Developer(config.develop)
        self.xmp_writer = XmpWriter(config.xmp)

    @property
    def tools(self) -> list[object]:
        return [
            self.raw_loader,
            self.detector,
            self.classifier,
            self.straightener,
            self.cropper,
            self.developer,
            self.xmp_writer,
        ]

    def process(self) -> dict[str, object]:
        raw_paths = find_raw_files(self.raw_dir)
        logger.info("RAW %d 枚  %s", len(raw_paths), self.raw_dir)
        logger.info(
            "既存の XMP: %s（%s）",
            self.config.xmp.on_existing,
            XMP_ACTIONS.get(self.config.xmp.on_existing, "?"),
        )

        with _phase("準備"):
            for tool in self.tools:
                tool.prepare()
        logger.info(
            "バックエンド  T3 作品検出: %s / T2 分類: %s",
            self.detector.backend,
            self.classifier.backend,
        )
        try:
            results = self._run(raw_paths)
        finally:
            for tool in self.tools:
                tool.release()

        summary = report.write(results, self.workspace.root / "report.json")
        self._log_summary(summary, results)
        return summary

    def _run(self, raw_paths: list[Path]) -> list[PhotoResult]:
        results = [self._new_result(p) for p in raw_paths]
        by_id = {r.photo_id: r for r in results}

        with _phase("T1 RAW 読み込み"):
            for i, result in enumerate(results, 1):
                preview_path = self.workspace.photo_dir(result.photo_id) / "preview.jpg"
                result.raw = self.raw_loader.load(result.raw_path, preview_path)
                _log_photo(i, len(results), result.photo_id, "T1", result.raw)

        loaded = [r for r in results if r.raw is not None and r.raw.status is not Status.FAILED]
        total = len(loaded)

        with _phase("T3 作品検出"):
            for i, result in enumerate(loaded, 1):
                photo_dir = self.workspace.photo_dir(result.photo_id)
                result.detection = self.detector.detect(
                    result.raw,
                    mask_path=photo_dir / "mask.png",
                    overlay_path=self._overlay(result.photo_id, "detection.jpg"),
                )
                _log_photo(i, total, result.photo_id, "T3", result.detection)

        # 全写真を横断するのでここだけ一括
        with _phase("T2 分類"):
            classifications = self.classifier.group(
                [r.raw for r in loaded],
                {r.photo_id: r.detection for r in loaded if r.detection},
            )
            for photo_id, classification in classifications.items():
                by_id[photo_id].classification = classification

            groups = {c.group_id for c in classifications.values()}
            logger.info(
                "%d 枚 → %d 作品（%s、しきい値 %.2f）",
                len(classifications),
                len(groups),
                self.classifier.backend,
                self.classifier.similarity_threshold,
            )
            # 撮影順に並んでいるので、類似度の落ち込みが作品の切れ目として読める
            for i, (photo_id, classification) in enumerate(classifications.items(), 1):
                _log_photo(i, total, photo_id, "T2", classification)

        with _phase("T4 水平推定 → T5 トリミング → T6 レタッチ"):
            for i, result in enumerate(loaded, 1):
                result.straightening = self.straightener.estimate(
                    result.raw,
                    result.detection,
                    overlay_path=self._overlay(result.photo_id, "straighten.jpg"),
                )
                _log_photo(i, total, result.photo_id, "T4", result.straightening)

                result.crop = self.cropper.compute(
                    result.raw,
                    result.detection,
                    result.straightening,
                    overlay_path=self._overlay(result.photo_id, "crop.jpg"),
                )
                _log_photo(i, total, result.photo_id, "T5", result.crop)

                result.develop = self.developer.develop(
                    result.raw, result.detection, result.classification
                )
                _log_photo(i, total, result.photo_id, "T6", result.develop)

        with _phase("T7 XMP 書き出し"):
            for i, result in enumerate(loaded, 1):
                action = self._xmp_action(result)
                result.xmp_written = self.xmp_writer.write(result)
                self.workspace.save(result)
                tag = _tag(i, total, result.photo_id)
                logger.info("%s T7  %s  %s", tag, action, result.xmp_path.name)

        return results

    def _xmp_action(self, result: PhotoResult) -> str:
        if not result.xmp_path.exists():
            return "新規に書き込み"
        return XMP_ACTIONS.get(self.config.xmp.on_existing, self.config.xmp.on_existing)

    def _log_summary(self, summary: dict[str, object], results: list[PhotoResult]) -> None:
        logger.info("完了  %d 枚中 XMP 書き込み %d 枚", summary["total"], summary["xmp_written"])

        skipped = [r.photo_id for r in results if r.raw is not None and not r.xmp_written]
        if skipped:
            logger.warning(
                "既存の XMP があるため %d 枚は書き換えていない。"
                "作り直すには --overwrite-xmp か、設定で xmp.on_existing を merge にする",
                len(skipped),
            )
        needs_review = summary["needs_review"]
        if needs_review:
            logger.warning("要確認 %d 枚: %s", len(needs_review), ", ".join(needs_review))
        logger.info("レポート %s", self.workspace.root / "report.json")

    def _overlay(self, photo_id: str, filename: str) -> Path | None:
        if not self.config.write_overlays:
            return None
        return self.workspace.overlay_dir(photo_id) / filename

    def _new_result(self, raw_path: Path) -> PhotoResult:
        return PhotoResult(
            photo_id=raw_path.stem,
            raw_path=raw_path,
            xmp_path=xmp_path_for(raw_path),
        )


@contextmanager
def _phase(title: str) -> Iterator[None]:
    """工程の区切りと所要時間を出す。モデルを使う工程は 1 分近くかかる。"""
    logger.info("── %s", title)
    started = time.perf_counter()
    yield
    logger.info("── %s 完了（%.1f 秒）", title, time.perf_counter() - started)


def _log_photo(index: int, total: int, photo_id: str, tool: str, part: logs.Part) -> None:
    """写真 1 枚・ツール 1 つの結果を 1 行で出す。要確認のものは WARNING にする。"""
    tag = _tag(index, total, photo_id)
    logger.log(logs.level_for(part.status), "%s %s  %s", tag, tool, logs.describe(part))


def _tag(index: int, total: int, photo_id: str) -> str:
    return f"[{index:>{len(str(total))}}/{total}] {photo_id}"
