"""確認用画像の描画。解析結果を目視できる形にする。"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .models import Box

MASK_COLOR = (0, 255, 0)
LINE_COLOR = (0, 160, 255)
CROP_COLOR = (0, 0, 255)


def draw_mask(image: np.ndarray, mask: np.ndarray, alpha: float = 0.35) -> np.ndarray:
    """マスク領域を半透明で塗る。"""
    tinted = image.copy()
    tinted[mask > 0] = MASK_COLOR
    return cv2.addWeighted(tinted, alpha, image, 1.0 - alpha, 0.0)


def draw_box(image: np.ndarray, box: Box, color=CROP_COLOR, thickness: int = 2) -> np.ndarray:
    out = image.copy()
    cv2.rectangle(
        out,
        (int(box.left), int(box.top)),
        (int(box.right), int(box.bottom)),
        color,
        thickness,
    )
    return out


def draw_lines(image: np.ndarray, lines, color=LINE_COLOR, thickness: int = 2) -> np.ndarray:
    out = image.copy()
    for line in lines:
        cv2.line(
            out,
            (int(line.x1), int(line.y1)),
            (int(line.x2), int(line.y2)),
            color,
            thickness,
        )
    return out


def save(image: np.ndarray, path: Path) -> Path:
    cv2.imwrite(str(path), image)
    return path
