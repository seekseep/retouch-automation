"""合成画像で T2〜T6 を確かめる。RAW が無くてもここまでは押さえられる。"""

import math
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
import pytest

from retouch_automation import modelzoo
from retouch_automation.config import (
    ClassifyConfig,
    DetectionConfig,
    DevelopConfig,
    ModelConfig,
    StraightenConfig,
)
from retouch_automation.models import ArtworkDetection, Box, RawImage, Size, Space, Status
from retouch_automation.tools.artwork_detector import ArtworkDetector
from retouch_automation.tools.classifier import Classifier
from retouch_automation.tools.developer import Developer
from retouch_automation.tools.straightener import Straightener

WIDTH, HEIGHT = 640, 480
TILT = 3.0


def write_preview(image: np.ndarray, path: Path) -> RawImage:
    """合成画像をプレビューとして保存し、対応する RawImage を返す。"""
    cv2.imwrite(str(path), image)
    height, width = image.shape[:2]
    return RawImage(
        photo_id=path.stem,
        raw_path=path.with_suffix(".ARW"),
        preview_path=path,
        sensor_size=Size(width=width * 4, height=height * 4),
        image_size=Size(width=width * 4, height=height * 4),
        preview_size=Size(width=width, height=height),
        preview_scale=4.0,
        captured_at=datetime(2026, 8, 1, 12, 0, 0),
    )


def no_detection() -> ArtworkDetection:
    return ArtworkDetection(status=Status.FALLBACK)


def full_frame_detection() -> ArtworkDetection:
    return ArtworkDetection(
        status=Status.SUCCESS,
        bbox=Box(space=Space.PREVIEW, left=0, top=0, right=WIDTH, bottom=HEIGHT),
    )


# --- T4 水平推定 -------------------------------------------------------


def tilted_room(tilt_degrees: float) -> np.ndarray:
    """壁の水平線と建具の垂直線が tilt だけ時計回りに傾いた部屋を描く。

    時計回りに傾いた内容は、反時計回りに tilt だけ回せば水平になる。
    つまり推定値は +tilt になるはず。
    """
    image = np.full((HEIGHT, WIDTH, 3), 200, np.uint8)
    slope = math.tan(math.radians(tilt_degrees))

    for y in (120, 240, 360):
        cv2.line(image, (0, int(y)), (WIDTH, int(y + WIDTH * slope)), (60, 60, 60), 3)
    for x in (160, 480):
        cv2.line(image, (int(x), 0), (int(x - HEIGHT * slope), HEIGHT), (60, 60, 60), 3)
    return image


def test_straightener_recovers_tilt_with_correct_sign(tmp_path):
    raw = write_preview(tilted_room(TILT), tmp_path / "tilted.jpg")
    result = Straightener(StraightenConfig()).estimate(raw, no_detection())

    assert result.status is Status.SUCCESS
    assert result.angle_degrees == pytest.approx(TILT, abs=0.5)


def test_straightener_returns_zero_on_level_image(tmp_path):
    raw = write_preview(tilted_room(0.0), tmp_path / "level.jpg")
    result = Straightener(StraightenConfig()).estimate(raw, no_detection())

    assert result.angle_degrees == pytest.approx(0.0, abs=0.3)


def test_straightener_falls_back_without_lines(tmp_path):
    blank = np.full((HEIGHT, WIDTH, 3), 180, np.uint8)
    raw = write_preview(blank, tmp_path / "blank.jpg")
    result = Straightener(StraightenConfig()).estimate(raw, no_detection())

    assert result.status is Status.FALLBACK
    assert result.angle_degrees == 0.0
    assert result.note


def test_straightener_ignores_lines_inside_artwork(tmp_path):
    """作品の内側にある線は基準にしない。"""
    image = tilted_room(0.0)
    # 作品の枝に見立てた急な斜線を中央に足す
    for offset in (-40, 0, 40):
        cv2.line(image, (260 + offset, 140), (380 + offset, 340), (30, 30, 30), 4)

    raw = write_preview(image, tmp_path / "with_artwork.jpg")
    detection = ArtworkDetection(
        status=Status.SUCCESS,
        bbox=Box(space=Space.PREVIEW, left=220, top=120, right=420, bottom=360),
    )
    result = Straightener(StraightenConfig()).estimate(raw, detection)

    assert result.angle_degrees == pytest.approx(0.0, abs=0.5)


def test_straightener_ignores_edges_of_excluded_area(tmp_path):
    """作品を除外した領域の縁を基準線と取り違えない。

    除外領域を黒く塗ってから線分を探すと、塗った矩形の縁が完全な水平・垂直の
    長い線分として拾われ、傾いた写真でも 0 度に張り付く（実写 13 枚中 5 枚で発生）。
    """
    raw = write_preview(tilted_room(TILT), tmp_path / "tilted_with_artwork.jpg")
    detection = ArtworkDetection(
        status=Status.SUCCESS,
        bbox=Box(space=Space.PREVIEW, left=220, top=150, right=420, bottom=330),
    )
    result = Straightener(StraightenConfig()).estimate(raw, detection)

    assert result.angle_degrees == pytest.approx(TILT, abs=0.5)
    assert all(abs(line.angle_degrees) > 1.0 for line in result.lines)


# --- T3 作品検出 -------------------------------------------------------


def arrangement_on_wall() -> np.ndarray:
    """壁を背にした作品。花器と花、細い枝を描く。"""
    image = np.full((HEIGHT, WIDTH, 3), 210, np.uint8)
    cv2.rectangle(image, (280, 330), (360, 420), (70, 60, 110), -1)      # 花器
    cv2.circle(image, (320, 250), 55, (60, 90, 200), -1)                 # 花
    cv2.line(image, (320, 300), (200, 130), (50, 110, 60), 3)            # 枝
    cv2.line(image, (320, 300), (450, 160), (50, 110, 60), 3)            # 枝
    return image


def arrangement_without_vessel() -> np.ndarray:
    """花器の無い作品。壁に掛けた枝ものを想定する。"""
    image = np.full((HEIGHT, WIDTH, 3), 210, np.uint8)
    cv2.circle(image, (320, 250), 55, (60, 90, 200), -1)
    cv2.line(image, (320, 300), (200, 130), (50, 110, 60), 3)
    cv2.line(image, (320, 300), (450, 160), (50, 110, 60), 3)
    return image


def test_detector_covers_vessel_and_flower(tmp_path):
    """初期矩形の外が確定背景にならないことを見る。

    花器は y 330..420 にあり、前景の種にする中央領域（70%）から下にはみ出す。
    はみ出した分を確定背景として学習すると花器がまるごと背景に倒れるので、
    ここが通らなくなったら _segment の初期化を疑う。
    花器そのものは必ず取れなくてよい（取れない作品もある）が、
    「作品の一部が系統的に切れる」状態は避けたい。
    """
    raw = write_preview(arrangement_on_wall(), tmp_path / "work.jpg")
    result = ArtworkDetector(DetectionConfig()).detect(
        raw, mask_path=tmp_path / "mask.png"
    )

    assert result.status is Status.SUCCESS
    assert result.bbox is not None
    # 花（上）から花器（下）までを含む
    assert result.bbox.top <= 210
    assert result.bbox.bottom >= 410


def test_detector_covers_arrangement_without_vessel(tmp_path):
    """花器が無くても作品全体を拾う。"""
    raw = write_preview(arrangement_without_vessel(), tmp_path / "no_vessel.jpg")
    result = ArtworkDetector(DetectionConfig()).detect(raw)

    assert result.status is Status.SUCCESS
    assert result.bbox is not None
    # 枝の左端（x=200）から右端（x=450）、花の上端（y=195）までを含む
    assert result.bbox.left <= 210
    assert result.bbox.right >= 440
    assert result.bbox.top <= 210


def test_detector_is_reproducible(tmp_path):
    """同じ写真からは毎回同じ領域を返す。

    GrabCut の GMM 初期化は OpenCV のグローバル RNG を引くので、種を固定しないと
    呼び出し順で結果が変わる。同じ写真を 2 回処理して違うトリミングになると、
    やり直しのたびに結果が動いて人の確認が無駄になる。
    """
    raw = write_preview(arrangement_on_wall(), tmp_path / "work.jpg")
    detector = ArtworkDetector(DetectionConfig())
    boxes = [detector.detect(raw).bbox for _ in range(3)]

    assert all(b is not None for b in boxes)
    assert len({(b.left, b.top, b.right, b.bottom) for b in boxes}) == 1


def test_detector_falls_back_on_blank_image(tmp_path):
    blank = np.full((HEIGHT, WIDTH, 3), 210, np.uint8)
    raw = write_preview(blank, tmp_path / "blank.jpg")
    result = ArtworkDetector(DetectionConfig()).detect(raw)

    assert result.status is Status.FALLBACK
    assert result.bbox.width == WIDTH
    assert result.bbox.height == HEIGHT


# --- T2 分類 -----------------------------------------------------------


def make_raw_for_classification(tmp_path, name, image, seconds):
    raw = write_preview(image, tmp_path / f"{name}.jpg")
    raw.captured_at = datetime(2026, 8, 1, 12, 0, 0) + timedelta(seconds=seconds)
    return raw


def test_same_artwork_shares_group(tmp_path):
    image = arrangement_on_wall()
    raws = [
        make_raw_for_classification(tmp_path, "a", image, 0),
        make_raw_for_classification(tmp_path, "b", image.copy(), 10),
    ]
    detections = {r.photo_id: full_frame_detection() for r in raws}

    groups = Classifier(["ikebana"], "artwork_").group(raws, detections)
    assert groups["a"].group_id == groups["b"].group_id


def test_time_gap_starts_new_group(tmp_path):
    image = arrangement_on_wall()
    raws = [
        make_raw_for_classification(tmp_path, "a", image, 0),
        make_raw_for_classification(tmp_path, "b", image.copy(), 600),
    ]
    detections = {r.photo_id: full_frame_detection() for r in raws}

    groups = Classifier(["ikebana"], "artwork_").group(raws, detections)
    assert groups["a"].group_id != groups["b"].group_id


def test_every_photo_gets_a_group(tmp_path):
    raws = [
        make_raw_for_classification(tmp_path, f"p{i}", arrangement_on_wall(), i * 300)
        for i in range(4)
    ]
    groups = Classifier(["ikebana"], "artwork_").group(raws, {})

    assert len(groups) == 4
    assert all("ikebana" in c.tags for c in groups.values())


# --- T6 レタッチ -------------------------------------------------------


def test_develop_stays_neutral_while_disabled(tmp_path):
    """無効にすると算出はするが XMP には出さない。"""
    image = arrangement_on_wall()
    image[0:100, 0:100] = 255  # 白飛び領域
    raw = write_preview(image, tmp_path / "bright.jpg")

    result = Developer(DevelopConfig(enabled=False)).develop(raw, full_frame_detection(), None)

    assert result.status is Status.FALLBACK
    assert result.highlights2012 == 0
    assert result.detail["proposal"]["highlights2012"] < 0


def test_develop_recovers_highlights_when_enabled(tmp_path):
    image = arrangement_on_wall()
    image[0:120, 0:120] = 255
    raw = write_preview(image, tmp_path / "bright.jpg")

    result = Developer(DevelopConfig(enabled=True)).develop(raw, full_frame_detection(), None)

    assert result.status is Status.SUCCESS
    assert result.highlights2012 < 0


def test_develop_leaves_white_balance_to_camera(tmp_path):
    raw = write_preview(arrangement_on_wall(), tmp_path / "plain.jpg")
    result = Developer(DevelopConfig(enabled=True)).develop(raw, full_frame_detection(), None)

    assert result.temperature is None
    assert result.tint is None


def test_develop_is_enabled_by_default():
    """既定で有効。動くのは張り付きを検出した項目だけ。"""
    assert DevelopConfig().enabled is True


# --- バックエンドの選び方 -----------------------------------------------


def test_detector_falls_back_to_classical_without_models(monkeypatch):
    """auto ならモデルが無くても止まらない。"""
    monkeypatch.setattr(modelzoo, "is_available", lambda: False)
    detector = ArtworkDetector(DetectionConfig(backend="auto"), ModelConfig())
    detector.prepare()

    assert detector.backend == "classical"


def test_detector_refuses_model_backend_without_models(monkeypatch):
    """model を明示したのに依存が無い場合は、黙って劣化させず落とす。

    写真を 1 枚も処理しないうちに prepare() で止まるので、
    全部終わってから「実はモデルを使っていませんでした」にならない。
    """
    monkeypatch.setattr(modelzoo, "is_available", lambda: False)
    detector = ArtworkDetector(DetectionConfig(backend="model"), ModelConfig())

    with pytest.raises(RuntimeError, match="extra models"):
        detector.prepare()


def test_classifier_falls_back_to_classical_without_models(monkeypatch):
    monkeypatch.setattr(modelzoo, "is_available", lambda: False)
    classifier = Classifier(["ikebana"], "artwork_", ClassifyConfig(backend="auto"))
    classifier.prepare()

    assert classifier.backend == "classical"


def test_classifier_refuses_model_backend_without_models(monkeypatch):
    monkeypatch.setattr(modelzoo, "is_available", lambda: False)
    classifier = Classifier(["ikebana"], "artwork_", ClassifyConfig(backend="model"))

    with pytest.raises(RuntimeError, match="extra models"):
        classifier.prepare()


def test_model_similarity_threshold_separates_measured_range():
    """校正に使った実測値が、しきい値の両側にきちんと分かれているか。

    展示写真 13 枚・6 作品で測った連続区間の類似度。

        同一作品内 … 0.9730 〜 0.9900
        別作品の境 … 0.0207 〜 0.4446

    しきい値をこの隙間の外へ動かすと分類が崩れる。動かすなら測り直すこと。
    """
    from retouch_automation.tools import classifier as module

    same_artwork = (0.9730, 0.9803, 0.9855, 0.9869, 0.9891, 0.9899, 0.9900)
    different_artwork = (0.0207, 0.1171, 0.1564, 0.2046, 0.4446)

    assert all(s >= module.MODEL_SIMILARITY_THRESHOLD for s in same_artwork)
    assert all(d < module.MODEL_SIMILARITY_THRESHOLD for d in different_artwork)


def test_time_gap_does_not_split_within_one_artwork():
    """同一作品内の最大間隔より長いこと。ここを下回ると作品が割れる。

    実測は同一作品内が 1〜5 秒、作品の切れ目が 6・6・9・18・22・58・594 秒。
    切れ目の最小が 6 秒しかないので、時刻だけでは切り分けられない。
    切り分けは類似度が受け持ち、ここは長い中断を拾う歯止めとして働く。
    """
    from retouch_automation.tools import classifier as module

    assert module.TIME_GAP_SECONDS > 5.0
