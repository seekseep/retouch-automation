"""T5 トリミング。

計算順序は SPEC のとおり:
  1. 水平補正角度を取得
  2. 回転後の作品領域を計算
  3. 花・葉・枝先・花器を含む範囲を求める
  4. 構図上必要な余白を足す
  5. 回転で生じる画像外領域を考慮する
  6. 最終範囲を決める。縦横比は Lightroom の比率プリセットのうちいちばん近いものに、
     枠を広げる向きで合わせる

全写真にトリミング設定を出すが、全写真を狭く切るという意味ではない。
作品が画像端に近い、あるいは検出に失敗した場合は元画像に近い広い範囲を採る。
作品を切り落としてまで黒い余白を除去しない。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

from .. import geometry, overlay
from ..config import CropConfig
from ..models import ArtworkDetection, Box, Crop, RawImage, Space, Status, Straightening

FULL_FRAME = (0.0, 0.0, 1.0, 1.0)

#: 枠が置き場所に収まるかを判定するときの許容誤差（px）
PLACEMENT_TOLERANCE = 1e-6


class AspectRatio(NamedTuple):
    label: str
    #: 幅 / 高さ
    value: float


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
        needs_void = not _contains(safe, with_margin)
        if needs_void and not self.config.allow_void:
            with_margin = _intersect(with_margin, safe)
        wanted = with_margin.clamped(raw.image_size)

        # 比率プリセットに合わせる。黒い余白を避けられる置き方を先に試す
        image = Box(
            space=Space.IMAGE,
            left=0.0,
            top=0.0,
            right=float(raw.image_size.width),
            bottom=float(raw.image_size.height),
        )
        areas = [safe, image] if self.config.allow_void else [safe]
        fitted = _fit_aspect(wanted, _aspect_ratios(self.config.aspect_ratios), areas)
        final, aspect_ratio = fitted if fitted is not None else (wanted, None)
        contains_void = needs_void or not _contains(safe, final)
        left, top, right, bottom = geometry.image_to_crop_norm(final, raw.image_size, angle)

        if overlay_path is not None:
            _draw(raw, final, overlay_path)

        notes = []
        if detection.status is not Status.SUCCESS:
            notes.append("作品検出がフォールバックのため広めに採用")
        if fitted is None and self.config.aspect_ratios:
            notes.append("どの比率プリセットも画像に収まらないため自由比率")

        return Crop(
            angle_degrees=angle,
            status=Status.SUCCESS if detection.status is Status.SUCCESS else Status.FALLBACK,
            left=left,
            top=top,
            right=right,
            bottom=bottom,
            aspect_ratio=aspect_ratio,
            keeps_artwork=True,
            contains_void=contains_void,
            overlay_path=overlay_path,
            note="、".join(notes) or None,
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


def _aspect_ratios(presets: list[tuple[float, float]]) -> list[AspectRatio]:
    """比率プリセットを縦長・横長の両方の向きに展開する。"""
    ratios = []
    for a, b in presets:
        short, long = sorted((a, b))
        name = f"{short:g}x{long:g}"
        if short == long:
            ratios.append(AspectRatio(name, 1.0))
            continue
        ratios.append(AspectRatio(f"{name} 横", long / short))
        ratios.append(AspectRatio(f"{name} 縦", short / long))
    return ratios


def _fit_aspect(
    box: Box, ratios: list[AspectRatio], areas: list[Box]
) -> tuple[Box, str] | None:
    """box を含み、縦横比がいちばん近いプリセットの枠を返す。

    枠は広げる向きにだけ変える。近さは比の対数の差で測るので、
    縦長と横長を同じ尺度で比べられる。いちばん近い比率が置けなければ
    次に近い比率を試し、どれも置けなければ None。
    areas は置き場所の候補で、先頭ほど優先する。
    """
    if box.width <= 0 or box.height <= 0:
        return None
    current = math.log(box.width / box.height)
    for ratio in sorted(ratios, key=lambda r: abs(math.log(r.value) - current)):
        width = max(box.width, box.height * ratio.value)
        height = width / ratio.value
        for area in areas:
            placed = _place(box, width, height, area)
            if placed is not None:
                return placed, ratio.label
    return None


def _place(inner: Box, width: float, height: float, area: Box) -> Box | None:
    """inner を含む width x height の枠を area の中に置く。置けなければ None。

    inner と中心を揃え、area からはみ出す分だけずらす。
    """
    left = _slide(inner.left, inner.right, width, area.left, area.right)
    top = _slide(inner.top, inner.bottom, height, area.top, area.bottom)
    if left is None or top is None:
        return None
    return Box(space=inner.space, left=left, top=top, right=left + width, bottom=top + height)


def _slide(start: float, end: float, length: float, lower: float, upper: float) -> float | None:
    """1 軸ぶんの _place。[start, end] を含み [lower, upper] に収まる区間の始点。"""
    earliest = max(end - length, lower)
    latest = min(start, upper - length)
    if earliest > latest + PLACEMENT_TOLERANCE:
        return None
    centered = (start + end - length) / 2.0
    return min(max(centered, earliest), latest)


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
