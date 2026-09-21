"""座標系の変換を一元管理する。

このシステムで事故が起きるのはほぼここなので、変換はこのモジュールの外に書かない。

扱う空間は models.Space の 3 つに加え、XMP に書く正規化座標 CROP_NORM の 4 つ。

    SENSOR  … RAW の有効画素領域（EXIF Orientation 適用前）
    IMAGE   … Orientation 適用後の見た目どおりの画像
    PREVIEW … 解析用の縮小画像。画像解析は全部ここ
    CROP_NORM … crs:CropTop/Left/Bottom/Right に書く 0.0–1.0

回転は座標空間を増やさず「角度＋回転中心」として持ち回り、必要な時だけ適用する。
"""

from __future__ import annotations

import math

from .models import Box, Size, Space

# ---------------------------------------------------------------------
# Phase 0 で確定させる定数。実物の Lightroom XMP と照合するまで暫定。
# 反転が必要になったらここだけを直す。
# ---------------------------------------------------------------------

#: crs:CropAngle の符号。+1 なら内部表現（CCW 正）をそのまま書く。
#:
#: Lightroom の実物と照合して確定済み。
#: 角度補正スライダーを +5.00 にすると画像は時計回りに 5 度回り
#: （画像枠の上辺が右下がりになる）、その状態の XMP は CropAngle="-5" だった。
#: 時計回り 5 度は CCW 正の内部表現では -5 度なので、両者は同符号。
#: 検証に使った XMP は DESIGN.md の 6 章に記録。
CROP_ANGLE_SIGN: float = 1.0

# crs:Crop* の座標定義は docs/xmp.md に実測で確定済み。
# 軸に平行な外接矩形ではなく、傾いたトリミング枠の「向かい合う 2 頂点」を
# 回転前の画像座標で表したもの。image_to_crop_norm() がその変換を担う。


def to_crop_angle(angle_degrees: float) -> float:
    """内部表現（CCW 正）を crs:CropAngle の値に変換する。

    いまは恒等変換だが、OpenCV が返した角度を符号の確認なしに XMP へ書かない
    ための関門として残す。反転が必要になったら CROP_ANGLE_SIGN だけを直す。
    """
    return CROP_ANGLE_SIGN * angle_degrees


# ---------------------------------------------------------------------
# PREVIEW ⇄ IMAGE
# ---------------------------------------------------------------------


def preview_to_image(box: Box, scale: float) -> Box:
    if box.space is not Space.PREVIEW:
        raise ValueError(f"PREVIEW の矩形を渡すこと: {box.space}")
    return Box(
        space=Space.IMAGE,
        left=box.left * scale,
        top=box.top * scale,
        right=box.right * scale,
        bottom=box.bottom * scale,
    )


def image_to_preview(box: Box, scale: float) -> Box:
    if box.space is not Space.IMAGE:
        raise ValueError(f"IMAGE の矩形を渡すこと: {box.space}")
    return Box(
        space=Space.PREVIEW,
        left=box.left / scale,
        top=box.top / scale,
        right=box.right / scale,
        bottom=box.bottom / scale,
    )


# ---------------------------------------------------------------------
# 回転
# ---------------------------------------------------------------------


def rotate_point(
    x: float, y: float, angle_degrees: float, cx: float, cy: float
) -> tuple[float, float]:
    """点を (cx, cy) 中心に CCW 正で回す。"""
    rad = math.radians(angle_degrees)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    dx, dy = x - cx, y - cy
    # 画像座標は y 軸が下向きなので、CCW 正にするため符号を合わせる
    return (cx + dx * cos_a + dy * sin_a, cy - dx * sin_a + dy * cos_a)


def rotated_bounds(box: Box, angle_degrees: float, cx: float, cy: float) -> Box:
    """矩形を回した後の外接矩形。"""
    corners = [
        (box.left, box.top),
        (box.right, box.top),
        (box.right, box.bottom),
        (box.left, box.bottom),
    ]
    pts = [rotate_point(x, y, angle_degrees, cx, cy) for x, y in corners]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return Box(space=box.space, left=min(xs), top=min(ys), right=max(xs), bottom=max(ys))


def largest_inscribed_rect(size: Size, angle_degrees: float) -> Box:
    """画像を angle だけ回したとき、黒い余白を含まない最大の内接矩形（IMAGE 空間）。

    T5 が「回転で生じる画像外領域」を避けるために使う。
    作品がこれに収まらない場合は、余白を残す側を選ぶ（T5 の責務）。
    """
    w, h = float(size.width), float(size.height)
    angle = abs(math.radians(angle_degrees)) % math.pi
    if angle > math.pi / 2:
        angle = math.pi - angle
    if angle < 1e-9:
        return Box(space=Space.IMAGE, left=0.0, top=0.0, right=w, bottom=h)

    side_long, side_short = (w, h) if w >= h else (h, w)
    sin_a, cos_a = math.sin(angle), math.cos(angle)

    if side_short <= 2.0 * sin_a * cos_a * side_long or abs(sin_a - cos_a) < 1e-10:
        x = 0.5 * side_short
        wr, hr = (x / sin_a, x / cos_a) if w >= h else (x / cos_a, x / sin_a)
    else:
        cos_2a = cos_a * cos_a - sin_a * sin_a
        wr = (w * cos_a - h * sin_a) / cos_2a
        hr = (h * cos_a - w * sin_a) / cos_2a

    cx, cy = w / 2.0, h / 2.0
    return Box(
        space=Space.IMAGE,
        left=cx - wr / 2.0,
        top=cy - hr / 2.0,
        right=cx + wr / 2.0,
        bottom=cy + hr / 2.0,
    )


# ---------------------------------------------------------------------
# IMAGE ⇄ CROP_NORM
# ---------------------------------------------------------------------


def image_to_crop_norm(
    box: Box, size: Size, angle_degrees: float = 0.0
) -> tuple[float, float, float, float]:
    """回転後の画像で見た矩形を crs:Crop* の値に変換する。

    **crs:Crop* は軸に平行な外接矩形ではない。** 傾いたトリミング枠の
    向かい合う 2 頂点を、回転前の画像座標で表して正規化したもの。
    角度が 0 のときだけ軸平行の矩形に退化する。

    box は「回転後の画像で見た軸平行の矩形」として受け取る。T5 が
    rotated_bounds() で作るのがまさにそれ。回転を戻して頂点の位置に直す。

    Lightroom の実物 XMP と小数以下まで一致することを確認済み。
    根拠と検証手順は docs/xmp.md。

    戻り値は (left, top, right, bottom)。
    """
    if box.space is not Space.IMAGE:
        raise ValueError(f"IMAGE の矩形を渡すこと: {box.space}")

    cx, cy = size.width / 2.0, size.height / 2.0
    left, top = rotate_point(box.left, box.top, -angle_degrees, cx, cy)
    right, bottom = rotate_point(box.right, box.bottom, -angle_degrees, cx, cy)

    clamp = lambda v: min(1.0, max(0.0, v))  # noqa: E731
    return (
        clamp(left / size.width),
        clamp(top / size.height),
        clamp(right / size.width),
        clamp(bottom / size.height),
    )


# ---------------------------------------------------------------------
# EXIF Orientation
# ---------------------------------------------------------------------

#: Orientation が縦横を入れ替えるか
_SWAPS_AXES = {5, 6, 7, 8}


def orientation_swaps_axes(orientation: int) -> bool:
    return orientation in _SWAPS_AXES


def sensor_to_image_size(sensor: Size, orientation: int) -> Size:
    if orientation_swaps_axes(orientation):
        return Size(width=sensor.height, height=sensor.width)
    return Size(width=sensor.width, height=sensor.height)
