"""T4 水平推定。

背景の壁・床・建具の直線から傾きを求める。

作品の枝や花器の輪郭は基準にしない。T3 のマスクを膨らませて除外領域にする。
壁と床の境界は遠近法で斜めに写るので、水平線だけで決めず垂直線も使い、
両者が一致したときだけ信用する。
信頼できる基準線が無ければ 0 度とし、理由を残す。
初期実装は遠近補正ではなく単純な回転補正のみ。
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .. import overlay
from ..config import StraightenConfig
from ..models import ArtworkDetection, RawImage, ReferenceLine, Status, Straightening

#: 線分をこの長さ未満なら捨てる（画像の長辺に対する比率）
MIN_LINE_RATIO = 0.08


class Straightener:
    name = "T4_straightener"

    def __init__(self, config: StraightenConfig) -> None:
        self.config = config

    def prepare(self) -> None:
        return None

    def release(self) -> None:
        return None

    def estimate(
        self,
        raw: RawImage,
        detection: ArtworkDetection,
        overlay_path: Path | None = None,
    ) -> Straightening:
        image = cv2.imread(str(raw.preview_path))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        background = _background_area(gray.shape, detection, self.config.mask_dilate_ratio)

        lines = _detect_reference_lines(gray, background, self.config.max_abs_angle)
        horizontals = [l for l in lines if l.kind == "horizontal"]
        verticals = [l for l in lines if l.kind == "vertical"]

        if overlay_path is not None:
            overlay.save(overlay.draw_lines(image, lines), overlay_path)

        if len(lines) < self.config.min_lines:
            return Straightening(
                status=Status.FALLBACK,
                note=f"基準線が {len(lines)} 本しか取れないため回転なし",
                lines=lines,
                overlay_path=overlay_path,
            )

        angle, confidence = _aggregate(horizontals, verticals)

        if abs(angle) > self.config.max_abs_angle:
            return Straightening(
                status=Status.FALLBACK,
                note=f"推定角 {angle:.2f} 度が上限を超えるため回転なし",
                lines=lines,
                overlay_path=overlay_path,
            )

        return Straightening(
            status=Status.SUCCESS,
            angle_degrees=angle,
            confidence=confidence,
            lines=lines,
            overlay_path=overlay_path,
        )


def _background_area(
    shape: tuple[int, int], detection: ArtworkDetection, dilate_ratio: float
) -> np.ndarray:
    """作品を除いた背景だけを 255 にしたマスク。"""
    height, width = shape
    area = np.full((height, width), 255, dtype=np.uint8)
    if detection.bbox is None:
        return area

    margin_x = width * dilate_ratio
    margin_y = height * dilate_ratio
    box = detection.bbox
    cv2.rectangle(
        area,
        (int(box.left - margin_x), int(box.top - margin_y)),
        (int(box.right + margin_x), int(box.bottom + margin_y)),
        0,
        thickness=-1,
    )
    return area


def _detect_reference_lines(
    gray: np.ndarray, background: np.ndarray, max_abs_angle: float
) -> list[ReferenceLine]:
    """背景の中から、水平・垂直に近い線分だけを拾う。"""
    masked = cv2.bitwise_and(gray, gray, mask=background)
    detector = cv2.createLineSegmentDetector()
    segments = detector.detect(masked)[0]
    if segments is None:
        return []

    min_length = max(gray.shape) * MIN_LINE_RATIO
    lines: list[ReferenceLine] = []

    for x1, y1, x2, y2 in segments.reshape(-1, 4):
        if math.hypot(x2 - x1, y2 - y1) < min_length:
            continue
        line = _classify(float(x1), float(y1), float(x2), float(y2))
        if line is not None and abs(line.angle_degrees) <= max_abs_angle:
            lines.append(line)

    return lines


def _classify(x1: float, y1: float, x2: float, y2: float) -> ReferenceLine | None:
    """線分を水平寄り／垂直寄りに分け、水平にするための回転角（CCW 正）を求める。"""
    dx, dy = x2 - x1, y2 - y1

    if abs(dx) >= abs(dy):
        if dx < 0:
            dx, dy = -dx, -dy
        angle = math.degrees(math.atan2(dy, dx))
        kind = "horizontal"
    else:
        if dy < 0:
            dx, dy = -dx, -dy
        # 垂直線は 90 度回して水平に読み替える
        angle = math.degrees(math.atan2(-dx, dy))
        kind = "vertical"

    return ReferenceLine(x1=x1, y1=y1, x2=x2, y2=y2, kind=kind, angle_degrees=angle)


def _aggregate(
    horizontals: list[ReferenceLine], verticals: list[ReferenceLine]
) -> tuple[float, float]:
    """水平系と垂直系それぞれの代表角を出し、一致度を確信度にする。"""
    h_angle = _length_weighted_median(horizontals)
    v_angle = _length_weighted_median(verticals)

    if h_angle is None:
        return (v_angle or 0.0, 0.4)
    if v_angle is None:
        return (h_angle, 0.4)

    # 両者が近いほど信用できる。1 度ずれたら確信度は 0 に近づく。
    disagreement = abs(h_angle - v_angle)
    confidence = max(0.0, 1.0 - disagreement)
    return ((h_angle + v_angle) / 2.0, confidence)


def _length_weighted_median(lines: list[ReferenceLine]) -> float | None:
    """長い線分ほど重く見た中央値。外れ値に引きずられにくい。"""
    if not lines:
        return None

    weighted = sorted(
        (l.angle_degrees, math.hypot(l.x2 - l.x1, l.y2 - l.y1)) for l in lines
    )
    total = sum(w for _, w in weighted)
    accumulated = 0.0
    for angle, weight in weighted:
        accumulated += weight
        if accumulated >= total / 2.0:
            return angle
    return weighted[-1][0]
