"""座標変換のテスト。ここが壊れると全部ずれるので単体で押さえる。"""

import math

import pytest

from retouch_automation import geometry
from retouch_automation.models import Box, Size, Space


def preview_box() -> Box:
    return Box(space=Space.PREVIEW, left=10, top=20, right=110, bottom=220)


def test_preview_image_round_trip():
    box = preview_box()
    back = geometry.image_to_preview(geometry.preview_to_image(box, 4.0), 4.0)
    assert (back.left, back.top, back.right, back.bottom) == (10, 20, 110, 220)


def test_crop_norm_matches_lightroom_with_rotation():
    """Lightroom が実際に書いた値を再現できるか。

    DSC02759 を角度 +5.00（画像は時計回りに 5 度回る）・縦横比 1x1 で
    切り抜いたときの XMP が下記だった。

        crs:CropAngle  = -5
        crs:CropLeft   = 0.208353   crs:CropTop    = 0.13292
        crs:CropRight  = 0.791647   crs:CropBottom = 0.86708

    このとき切り抜き枠は「回転後の画像で見て」一辺 2119.2 px の正方形で、
    画像（3936x2624）の中心にある。そこからこの 4 つの値が出れば、
    座標定義の理解が正しい。ここが崩れたら docs/xmp.md を読み直すこと。
    """
    size = Size(width=3936, height=2624)
    side = 2119.2
    cx, cy = size.width / 2.0, size.height / 2.0
    box = Box(
        space=Space.IMAGE,
        left=cx - side / 2.0,
        top=cy - side / 2.0,
        right=cx + side / 2.0,
        bottom=cy + side / 2.0,
    )

    left, top, right, bottom = geometry.image_to_crop_norm(box, size, angle_degrees=-5.0)

    assert left == pytest.approx(0.208353, abs=1e-5)
    assert top == pytest.approx(0.13292, abs=1e-5)
    assert right == pytest.approx(0.791647, abs=1e-5)
    assert bottom == pytest.approx(0.86708, abs=1e-5)


def test_crop_norm_without_rotation_is_plain_ratio():
    """角度 0 のときは素直な比率に退化する。

    同じ写真を角度 0 で切り抜いた XMP が下記だった。
        crs:CropLeft=0.461584  crs:CropTop=0  crs:CropRight=1  crs:CropBottom=0.807622
    """
    size = Size(width=3936, height=2624)
    box = Box(
        space=Space.IMAGE,
        left=0.461584 * 3936,
        top=0.0,
        right=3936.0,
        bottom=0.807622 * 2624,
    )

    left, top, right, bottom = geometry.image_to_crop_norm(box, size, angle_degrees=0.0)

    assert left == pytest.approx(0.461584, abs=1e-6)
    assert top == pytest.approx(0.0, abs=1e-6)
    assert right == pytest.approx(1.0, abs=1e-6)
    assert bottom == pytest.approx(0.807622, abs=1e-6)


def test_image_to_crop_norm():
    size = Size(width=200, height=100)
    box = Box(space=Space.IMAGE, left=50, top=25, right=150, bottom=75)
    assert geometry.image_to_crop_norm(box, size) == (0.25, 0.25, 0.75, 0.75)


def test_crop_norm_is_clamped_to_image():
    size = Size(width=100, height=100)
    box = Box(space=Space.IMAGE, left=-20, top=-20, right=200, bottom=200)
    assert geometry.image_to_crop_norm(box, size) == (0.0, 0.0, 1.0, 1.0)


def test_rotate_point_is_counter_clockwise():
    # 画像座標は y 軸が下向き。(1, 0) を 90 度 CCW で回すと (0, -1) に行く。
    x, y = geometry.rotate_point(1.0, 0.0, 90.0, 0.0, 0.0)
    assert math.isclose(x, 0.0, abs_tol=1e-9)
    assert math.isclose(y, -1.0, abs_tol=1e-9)


def test_inscribed_rect_is_full_frame_without_rotation():
    size = Size(width=300, height=200)
    box = geometry.largest_inscribed_rect(size, 0.0)
    assert (box.left, box.top, box.right, box.bottom) == (0.0, 0.0, 300.0, 200.0)


def test_inscribed_rect_shrinks_with_rotation():
    size = Size(width=300, height=200)
    box = geometry.largest_inscribed_rect(size, 5.0)
    assert box.width < 300.0
    assert box.height < 200.0


def test_crop_angle_sign_matches_lightroom():
    """Lightroom の実物 XMP と照合した符号を固定する。

    角度補正 +5.00（画像は時計回りに 5 度回る）の XMP が CropAngle="-5" だった。
    時計回り 5 度は CCW 正の内部表現で -5 度なので、変換は符号をそのまま通す。
    """
    assert geometry.to_crop_angle(-5.0) == pytest.approx(-5.0)
    assert geometry.to_crop_angle(2.0) == pytest.approx(2.0)
    # 反転させるときは geometry の定数 1 箇所だけを直す
    assert geometry.to_crop_angle(2.0) == geometry.CROP_ANGLE_SIGN * 2.0
