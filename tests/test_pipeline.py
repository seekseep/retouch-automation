"""RAW が無くても回せる範囲のテスト。T5 と T7 は合成データで確認できる。"""

from datetime import datetime
from pathlib import Path

import pytest

from retouch_automation import geometry
from retouch_automation.config import CropConfig, XmpConfig
from retouch_automation.models import (
    ArtworkDetection,
    Box,
    Classification,
    Crop,
    Develop,
    PhotoResult,
    RawImage,
    Size,
    Space,
    Status,
    Straightening,
)
from retouch_automation.tools.cropper import Cropper
from retouch_automation.tools.xmp_writer import XmpWriter


def make_raw() -> RawImage:
    return RawImage(
        photo_id="DSC00001",
        raw_path=Path("/tmp/DSC00001.ARW"),
        preview_path=Path("/tmp/preview.jpg"),
        sensor_size=Size(width=6000, height=4000),
        image_size=Size(width=6000, height=4000),
        preview_size=Size(width=1500, height=1000),
        preview_scale=4.0,
        captured_at=datetime(2026, 8, 1, 10, 0, 0),
    )


def detection_at(left, top, right, bottom, status=Status.SUCCESS) -> ArtworkDetection:
    return ArtworkDetection(
        status=status,
        bbox=Box(space=Space.PREVIEW, left=left, top=top, right=right, bottom=bottom),
        confidence=0.9,
    )


def test_centered_artwork_is_cropped():
    crop = Cropper(CropConfig()).compute(
        make_raw(), detection_at(500, 300, 1000, 700), Straightening(status=Status.SUCCESS)
    )
    assert not crop.is_full_frame
    assert crop.left > 0.0 and crop.right < 1.0


def test_artwork_near_edge_keeps_full_frame():
    # 端に寄った作品は切り落とさない
    crop = Cropper(CropConfig()).compute(
        make_raw(), detection_at(5, 5, 1495, 995), Straightening(status=Status.SUCCESS)
    )
    assert crop.is_full_frame


def test_missing_detection_keeps_full_frame():
    crop = Cropper(CropConfig()).compute(
        make_raw(),
        ArtworkDetection(status=Status.FALLBACK),
        Straightening(status=Status.FALLBACK),
    )
    assert crop.is_full_frame


def crop_in_pixels(crop: Crop, size: Size) -> Box:
    """crs:Crop* を、回転後の画像で見た軸平行の枠（px）に戻す。"""
    cx, cy = size.width / 2.0, size.height / 2.0
    left, top = geometry.rotate_point(
        crop.left * size.width, crop.top * size.height, crop.angle_degrees, cx, cy
    )
    right, bottom = geometry.rotate_point(
        crop.right * size.width, crop.bottom * size.height, crop.angle_degrees, cx, cy
    )
    return Box(space=Space.IMAGE, left=left, top=top, right=right, bottom=bottom)


def test_crop_takes_the_closest_aspect_ratio():
    # 作品＋余白は 1488 x 2480（0.6）。いちばん近いのは 10x16 の縦長（0.625）
    raw = make_raw()
    crop = Cropper(CropConfig()).compute(
        raw, detection_at(600, 250, 900, 750), Straightening(status=Status.SUCCESS)
    )
    assert crop.aspect_ratio == "10x16 縦"

    box = crop_in_pixels(crop, raw.image_size)
    assert box.width / box.height == pytest.approx(10 / 16)
    # 広げる向きにだけ合わせるので、作品＋余白（2256, 760）-（3744, 3240）は残る
    assert box.left <= 2256 + 1e-6 and box.right >= 3744 - 1e-6
    assert box.top <= 760 + 1e-6 and box.bottom >= 3240 - 1e-6


def test_crop_skips_aspect_ratios_that_do_not_fit():
    # 作品は 3250 x 3700（0.878）。4x5 縦と 8.5x11 縦のほうが近いが、
    # 高さが画像の 4000 を超えるので 1x1 に落ちる
    raw = make_raw()
    crop = Cropper(CropConfig(composition_margin_ratio=0.0)).compute(
        raw, detection_at(343.75, 37.5, 1156.25, 962.5), Straightening(status=Status.SUCCESS)
    )
    assert crop.aspect_ratio == "1x1"

    box = crop_in_pixels(crop, raw.image_size)
    assert box.width == pytest.approx(3700)
    assert box.height == pytest.approx(3700)


def test_aspect_ratio_holds_with_rotation():
    raw = make_raw()
    crop = Cropper(CropConfig()).compute(
        raw,
        detection_at(500, 300, 1000, 700),
        Straightening(status=Status.SUCCESS, angle_degrees=3.0),
    )
    assert crop.aspect_ratio == "4x5 横"

    box = crop_in_pixels(crop, raw.image_size)
    assert box.width / box.height == pytest.approx(5 / 4)


def test_empty_aspect_ratios_keep_free_ratio():
    raw = make_raw()
    crop = Cropper(CropConfig(aspect_ratios=[])).compute(
        raw, detection_at(600, 250, 900, 750), Straightening(status=Status.SUCCESS)
    )
    assert crop.aspect_ratio is None
    assert crop.note is None

    box = crop_in_pixels(crop, raw.image_size)
    assert box.width / box.height == pytest.approx(0.6)


def test_new_xmp_contains_tags_and_crop(tmp_path):
    result = PhotoResult(
        photo_id="DSC00001",
        raw_path=tmp_path / "DSC00001.ARW",
        xmp_path=tmp_path / "DSC00001.xmp",
        classification=Classification(group_id="artwork_0001", tags=["ikebana", "artwork_0001"]),
        crop=Crop(status=Status.SUCCESS, left=0.1, top=0.1, right=0.9, bottom=0.9),
        develop=Develop(),
    )
    assert XmpWriter(XmpConfig()).write(result) is True

    xmp = result.xmp_path.read_text(encoding="utf-8")
    assert 'crs:HasCrop="True"' in xmp
    assert "artwork_0001" in xmp
    assert "ikebana" in xmp


FIXTURE = Path(__file__).parent / "fixtures" / "lightroom_sidecar.xmp"


def make_existing_xmp(tmp_path) -> Path:
    """Lightroom が実際に書いたサイドカーを模したもの。"""
    xmp_path = tmp_path / "DSC00001.xmp"
    xmp_path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return xmp_path


def test_merge_keeps_existing_metadata(tmp_path):
    """既存の EXIF・撮影情報・History を壊さずに crs: だけ差し替える。"""
    xmp_path = make_existing_xmp(tmp_path)
    result = PhotoResult(
        photo_id="DSC00001",
        raw_path=tmp_path / "DSC00001.ARW",
        xmp_path=xmp_path,
        classification=Classification(group_id="artwork_0001", tags=["ikebana", "artwork_0001"]),
        crop=Crop(status=Status.SUCCESS, left=0.1, top=0.1, right=0.9, bottom=0.9),
        develop=Develop(),
    )

    assert XmpWriter(XmpConfig(on_existing="merge")).write(result) is True

    merged = xmp_path.read_text(encoding="utf-8")
    assert "ILCE-7C" in merged
    assert "Adobe Lightroom 9.1" in merged
    assert "Camera Standard" in merged
    assert 'crs:HasCrop="True"' in merged
    assert "artwork_0001" in merged


def test_existing_xmp_is_kept_by_default(tmp_path):
    """既定では既存の XMP に触らない。

    人が Lightroom で直した回転・トリミングを再実行で消さないため。
    ここが merge に戻ると、手直しが毎回消える運用事故になる。
    """
    assert XmpConfig().on_existing == "skip"

    xmp_path = make_existing_xmp(tmp_path)
    before = xmp_path.read_text(encoding="utf-8")
    result = PhotoResult(
        photo_id="DSC00001",
        raw_path=tmp_path / "DSC00001.ARW",
        xmp_path=xmp_path,
        classification=Classification(group_id="artwork_0001", tags=["ikebana"]),
        crop=Crop(status=Status.SUCCESS, left=0.1, top=0.1, right=0.9, bottom=0.9),
    )

    assert XmpWriter(XmpConfig()).write(result) is False
    assert xmp_path.read_text(encoding="utf-8") == before


def test_skip_leaves_existing_xmp_untouched(tmp_path):
    xmp_path = make_existing_xmp(tmp_path)
    before = xmp_path.read_text(encoding="utf-8")
    result = PhotoResult(
        photo_id="DSC00001", raw_path=tmp_path / "DSC00001.ARW", xmp_path=xmp_path
    )

    assert XmpWriter(XmpConfig(on_existing="skip")).write(result) is False
    assert xmp_path.read_text(encoding="utf-8") == before


def test_rerun_does_not_duplicate_tags(tmp_path):
    """同じ入力で再実行してもタグが重複しない。"""
    xmp_path = make_existing_xmp(tmp_path)
    result = PhotoResult(
        photo_id="DSC00001",
        raw_path=tmp_path / "DSC00001.ARW",
        xmp_path=xmp_path,
        classification=Classification(group_id="artwork_0001", tags=["ikebana", "artwork_0001"]),
        crop=Crop(status=Status.SUCCESS),
        develop=Develop(),
    )
    writer = XmpWriter(XmpConfig(on_existing="merge"))
    writer.write(result)
    writer.write(result)

    assert xmp_path.read_text(encoding="utf-8").count("artwork_0001</rdf:li>") == 1


def test_xmp_is_valid_xml(tmp_path):
    from xml.etree import ElementTree

    result = PhotoResult(
        photo_id="DSC00001",
        raw_path=tmp_path / "DSC00001.ARW",
        xmp_path=tmp_path / "DSC00001.xmp",
        classification=Classification(group_id="artwork_0001", tags=["ikebana"]),
        crop=Crop(status=Status.SUCCESS),
        develop=Develop(),
    )
    XmpWriter(XmpConfig()).write(result)
    body = result.xmp_path.read_text(encoding="utf-8")
    body = body[body.index("<x:xmpmeta") : body.index("</x:xmpmeta>") + len("</x:xmpmeta>")]
    ElementTree.fromstring(body)
