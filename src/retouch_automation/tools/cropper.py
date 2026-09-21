"""T5 トリミング。

計算順序は SPEC のとおり:
  1. 水平補正角度を取得
  2. 回転後の作品領域を計算
  3. 花・葉・枝先・花器を含む範囲を求める
  4. 構図上必要な余白を足す
  5. 回転で生じる画像外領域を考慮する
  6. 最終範囲を決める

全写真にトリミング設定を出すが、全写真を狭く切るという意味ではない。
作品が画像端に近い、あるいは検出に失敗した場合は元画像に近い広い範囲を採る。
作品を切り落としてまで黒い余白を除去しない。
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .. import geometry, overlay
from ..config import CropConfig
from ..models import ArtworkDetection, Box, Crop, RawImage, Space, Status, Straightening

FULL_FRAME = (0.0, 0.0, 1.0, 1.0)


class Cropper:
    name = "T5_cropper"

    def __init__(self, config: CropConfig) -> None:
        self.config = config

    def prepare(self) -> None:
        return None

    def release(self) -> None:
        return None

    def compute(
        self,
        raw: RawImage,
        detection: ArtworkDetection,
        straightening: Straightening,
        overlay_path: Path | None = None,
    ) -> Crop:
        angle = straightening.angle_degrees
        artwork_box = _artwork_box(detection)
        if artwork_box is None:
            return _full_frame(angle, "作品領域が無いため全画面を採用")

        artwork = geometry.preview_to_image(artwork_box, raw.preview_scale)
        center = (raw.image_size.width / 2.0, raw.image_size.height / 2.0)

        rotated = geometry.rotated_bounds(artwork, angle, *center)
        with_margin = _expand(rotated, self.config.composition_margin_ratio)

        if _touches_edge(with_margin, raw, self.config.edge_proximity_ratio):
            return _full_frame(angle, "作品が画像端に近いため全画面を採用")

        # 回転で生じる画像外領域（黒い余白）を避けられるか見る
        safe = geometry.largest_inscribed_rect(raw.image_size, angle)
        contains_void = not _contains(safe, with_margin)
        if contains_void and not self.config.allow_void:
            with_margin = _intersect(with_margin, safe)

        final = with_margin.clamped(raw.image_size)
        left, top, right, bottom = geometry.image_to_crop_norm(final, raw.image_size, angle)

        if overlay_path is not None:
            _draw(raw, final, overlay_path)

        return Crop(
            angle_degrees=angle,
            status=Status.SUCCESS if detection.status is Status.SUCCESS else Status.FALLBACK,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            keeps_artwork=True,
            contains_void=contains_void,
            overlay_path=overlay_path,
            note=None if detection.status is Status.SUCCESS else "作品検出がフォールバックのため広めに採用",
        )


def _artwork_box(detection: ArtworkDetection) -> Box | None:
    """マスクがあればマスクの外接矩形を、無ければ bbox を使う。

    マスクの方が枝先まで含むので優先する。
    """
    if detection.mask_path is not None and Path(detection.mask_path).exists():
        mask = cv2.imread(str(detection.mask_path), cv2.IMREAD_GRAYSCALE)
        ys, xs = np.nonzero(mask)
        if len(xs):
            return Box(
                space=Space.PREVIEW,
                left=float(xs.min()),
                top=float(ys.min()),
                right=float(xs.max()),
                bottom=float(ys.max()),
            )
    return detection.bbox


def _draw(raw: RawImage, final: Box, overlay_path: Path) -> None:
    """確認用にトリミング枠をプレビュー上へ描く。"""
    image = cv2.imread(str(raw.preview_path))
    if image is None:
        return
    box = geometry.image_to_preview(final, raw.preview_scale)
    overlay.save(overlay.draw_box(image, box), overlay_path)


def _full_frame(angle_degrees: float, note: str) -> Crop:
    left, top, right, bottom = FULL_FRAME
    return Crop(
        status=Status.FALLBACK,
        angle_degrees=angle_degrees,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        keeps_artwork=True,
        note=note,
    )


def _expand(box: Box, ratio: float) -> Box:
    """矩形を比率ぶん外側に広げる。"""
    dx = box.width * ratio
    dy = box.height * ratio
    return Box(
        space=box.space,
        left=box.left - dx,
        top=box.top - dy,
        right=box.right + dx,
        bottom=box.bottom + dy,
    )


def _touches_edge(box: Box, raw: RawImage, ratio: float) -> bool:
    mx = raw.image_size.width * ratio
    my = raw.image_size.height * ratio
    return (
        box.left <= mx
        or box.top <= my
        or box.right >= raw.image_size.width - mx
        or box.bottom >= raw.image_size.height - my
    )


def _contains(outer: Box, inner: Box) -> bool:
    return (
        outer.left <= inner.left
        and outer.top <= inner.top
        and outer.right >= inner.right
        and outer.bottom >= inner.bottom
    )


def _intersect(a: Box, b: Box) -> Box:
    return Box(
        space=a.space,
        left=max(a.left, b.left),
        top=max(a.top, b.top),
        right=min(a.right, b.right),
        bottom=min(a.bottom, b.bottom),
    )
