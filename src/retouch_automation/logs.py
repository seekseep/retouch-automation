"""ログ出力。

画面（stderr）には工程の進み具合と、写真ごとの結果を 1 行ずつ出す。
作業ディレクトリの process.log には、基準線 1 本ごとの角度のような細部まで残す。

写真ごとの 1 行は、ツールの結果オブジェクトから組み立てる。
ツールの内側には写真を跨いだ文脈が無いので、要約はオーケストレーターが出す。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from .models import (
    ArtworkDetection,
    Classification,
    Crop,
    Develop,
    RawImage,
    Status,
    Straightening,
)

#: このパッケージのロガーの親。サードパーティのログには手を出さない
ROOT_LOGGER = "retouch_automation"

CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


def setup(verbosity: int = 0) -> None:
    """画面へのログを設定する。verbosity は -1（警告のみ）/ 0（既定）/ 1（詳細）。"""
    level = {-1: logging.WARNING, 0: logging.INFO}.get(verbosity, logging.DEBUG)

    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, TIME_FORMAT))
    logger.addHandler(console)


def attach_file(path: Path) -> None:
    """詳細ログをファイルにも書く。実行のたびに作り直す。"""
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(FILE_FORMAT))
    logging.getLogger(ROOT_LOGGER).addHandler(handler)


def level_for(status: Status | None) -> int:
    """正常なら INFO、フォールバックや失敗は目に付くように WARNING。"""
    return logging.INFO if status is Status.SUCCESS else logging.WARNING


# --- 写真ごとの 1 行 -----------------------------------------------------

Part = RawImage | ArtworkDetection | Classification | Straightening | Crop | Develop


def describe(part: Part) -> str:
    """ツールの結果を 1 行にする。note があれば末尾に添える。"""
    return _with_note(_DESCRIBERS[type(part)](part), part.note)


def _raw(raw: RawImage) -> str:
    parts = [f"{raw.preview_size.width}x{raw.preview_size.height}"]
    if raw.captured_at:
        parts.append(f"撮影 {raw.captured_at:%Y-%m-%d %H:%M:%S}")
    if raw.iso:
        parts.append(f"ISO {raw.iso}")
    if raw.shutter_speed:
        parts.append(_shutter(raw.shutter_speed))
    if raw.aperture:
        parts.append(f"f/{raw.aperture:g}")
    return "  ".join(parts)


def _detection(detection: ArtworkDetection) -> str:
    detail = detection.detail
    parts = [str(detail.get("backend", "?"))]
    if detection.bbox is not None:
        box = detection.bbox
        parts.append(f"枠 {box.left:.0f},{box.top:.0f}-{box.right:.0f},{box.bottom:.0f}")
    if "area_ratio" in detail:
        parts.append(f"面積比 {detail['area_ratio']:.3f}")
    if "detector_score" in detail:
        parts.append(f"確信度 {detail['detector_score']:.2f}（{detail.get('detector_label')}）")
    if "model_miss" in detail:
        parts.append(f"モデルで取れず classical に切替: {detail['model_miss']}")
    return "  ".join(parts)


def _classification(classification: Classification) -> str:
    text = classification.group_id
    similarity = classification.detail.get("similarity_to_previous")
    if similarity is not None:
        text += f"  前の写真との類似度 {similarity:.3f}"
    return text


def _straightening(straightening: Straightening) -> str:
    horizontals = sum(1 for l in straightening.lines if l.kind == "horizontal")
    verticals = len(straightening.lines) - horizontals
    return (
        f"回転 {straightening.angle_degrees:+.2f}°  確信度 {straightening.confidence:.2f}"
        f"  水平 {horizontals} 本 {_degrees(straightening.horizontal_degrees)}"
        f" / 垂直 {verticals} 本 {_degrees(straightening.vertical_degrees)}"
    )


def _crop(crop: Crop) -> str:
    text = (
        f"L {crop.left:.3f} T {crop.top:.3f} R {crop.right:.3f} B {crop.bottom:.3f}"
        f"  回転 {crop.angle_degrees:+.2f}°"
    )
    if crop.is_full_frame:
        text += "  全画面"
    if crop.contains_void:
        text += "  回転で生じる画像外にかかる"
    return text


def _develop(develop: Develop) -> str:
    return f"Highlights {develop.highlights2012:+d} / Shadows {develop.shadows2012:+d}"


_DESCRIBERS = {
    RawImage: _raw,
    ArtworkDetection: _detection,
    Classification: _classification,
    Straightening: _straightening,
    Crop: _crop,
    Develop: _develop,
}


def _with_note(text: str, note: str | None) -> str:
    return f"{text}  … {note}" if note else text


def _degrees(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}°"


def _shutter(seconds: float) -> str:
    if seconds >= 1.0:
        return f'{seconds:g}"'
    return f"1/{round(1.0 / seconds)}"
