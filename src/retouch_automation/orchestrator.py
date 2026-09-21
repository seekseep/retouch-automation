"""T8 オーケストレーター。

実行順序はツール間の依存で決まる。

    T1（全写真） → T3（全写真） → T2（1 回・全写真横断） → T4 → T5 → T6 → T7

T2 は作品領域を使うので T3 の後に置く。T2 だけが全写真を横断する。
1 枚の失敗で他を止めない。失敗した写真も可能な範囲で XMP を生成する。
ただし T1 が失敗した写真は XMP を作らず、失敗として扱う。
"""

from __future__ import annotations

from pathlib import Path

from . import report
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
        for tool in self.tools:
            tool.prepare()
        try:
            results = self._run(raw_paths)
        finally:
            for tool in self.tools:
                tool.release()

        return report.write(results, self.workspace.root / "report.json")

    def _run(self, raw_paths: list[Path]) -> list[PhotoResult]:
        results = [self._new_result(p) for p in raw_paths]
        by_id = {r.photo_id: r for r in results}

        # T1: RAW 読み込みとプレビュー生成
        for result in results:
            preview_path = self.workspace.photo_dir(result.photo_id) / "preview.jpg"
            result.raw = self.raw_loader.load(result.raw_path, preview_path)

        loaded = [r for r in results if r.raw is not None and r.raw.status is not Status.FAILED]

        # T3: 作品検出
        for result in loaded:
            photo_dir = self.workspace.photo_dir(result.photo_id)
            result.detection = self.detector.detect(
                result.raw,
                mask_path=photo_dir / "mask.png",
                overlay_path=self._overlay(result.photo_id, "detection.jpg"),
            )

        # T2: 分類（全写真を横断するのでここだけ一括）
        classifications = self.classifier.group(
            [r.raw for r in loaded],
            {r.photo_id: r.detection for r in loaded if r.detection},
        )
        for photo_id, classification in classifications.items():
            by_id[photo_id].classification = classification

        # T4 → T5 → T6
        for result in loaded:
            result.straightening = self.straightener.estimate(
                result.raw,
                result.detection,
                overlay_path=self._overlay(result.photo_id, "straighten.jpg"),
            )
            result.crop = self.cropper.compute(
                result.raw,
                result.detection,
                result.straightening,
                overlay_path=self._overlay(result.photo_id, "crop.jpg"),
            )
            result.develop = self.developer.develop(
                result.raw, result.detection, result.classification
            )

        # T7: XMP 書き出し
        for result in loaded:
            result.xmp_written = self.xmp_writer.write(result)
            self.workspace.save(result)

        return results

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
