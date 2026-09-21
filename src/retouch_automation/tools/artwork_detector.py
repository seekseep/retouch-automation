"""T3 作品検出。

花・葉・枝・花器を含む作品全体の領域を推定する。

検出対象は作品全体であって、花だけ・花器だけではない。
細い枝は面積が小さく通常のセグメンテーションから漏れやすいので、
主領域を取ったあとにエッジから枝を拾い直す。

検出マスクをそのまま厳密なトリミング境界にしない。安全余白は T5 が足す。
検出できなければ画像全体を返し、後続処理を止めない。

バックエンドは差し替え可能にしてある。
  model     … GroundingDINO で位置を出し、SAM 2.1 で領域を取る
  classical … 背景幕からの色差＋質感。モデル無しでも全工程が通るための退避路
"""

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np

from .. import modelzoo, overlay
from ..config import DetectionConfig, ModelConfig
from ..models import ArtworkDetection, Box, RawImage, Space, Status

#: 探索する中央領域（画像に対する比率）。作品は概ね中央に置かれる前提。
INNER_REGION_RATIO = 0.70

#: 作品とみなす上位何パーセントか。展示写真では作品が画面に占める割合は
#: 実測で 7〜12%。取りこぼすより広めに取る側へ倒す。
ARTWORK_PERCENTILE = 88.0

#: 背景幕の色を測る前にかけるぼかし（画素）。布目や粒状ノイズを均す。
BACKDROP_BLUR_SIGMA = 2.0

#: これより細かい濃淡を「質感」とみなす（画素）。幕は滑らかで、葉や花は細かい。
TEXTURE_BLUR_SIGMA = 3.0

#: 作品とみなすスコアの下限。スコアは画面内の最大値で 0.0–2.0 に正規化してあるので、
#: 「最も幕から離れた画素の 5% 以上は離れていること」を要求する意味になる。
#: 幕が完全に無地だと上位何パーセントを取っても閾値が 0 に落ち、全画素が
#: 作品に化けるため、下限が要る。実写 9 枚の閾値は 0.23〜0.66 でここには掛からない。
ARTWORK_MIN_SCORE = 0.10

#: 形態素処理のカーネル幅（画像長辺に対する比率）。
#: 葉の隙間は埋めたいが枝は残したいので、画像の大きさに合わせて決める。
MORPH_KERNEL_RATIO = 0.0045

#: プレビューのグレースケール標準偏差がこれを下回る画像は作品が写っていないと
#: みなす。一様な画像では「幕から離れた色」が定義できず、上位何パーセントを
#: 取っても意味のある領域にならない。
MIN_PREVIEW_STDDEV = 1.0

#: マスクがこの比率より小さい／大きい場合は推定を信用しない
MIN_AREA_RATIO = 0.02
MAX_AREA_RATIO = 0.85

#: 枝を拾い直すとき、主領域の周囲どこまでを探索するか（画像長辺に対する比率）
BRANCH_SEARCH_RATIO = 0.12

#: 主領域からこの比率（画像長辺に対する）以内の隙間しかない連結成分は、
#: 同じ作品の一部とみなして拾う。花と花器の間で茎が切れた場合の救済。
MERGE_GAP_RATIO = 0.06

#: 拾い直す連結成分の最小面積比。粒ノイズを巻き込まないための下限。
MIN_COMPONENT_AREA_RATIO = 0.0005

#: 画面を端から端まで貫く連結成分は、壁・柱・什器とみなして候補から外す。
#: 作品は台に載った自立した物体なので、画面の上下端（または左右端）の
#: 両方に届くことはない。会場の明るい壁が画面を縦に貫いて主領域に化け、
#: 枠が画面いっぱいに広がるのを実写で踏んだ。
FRAME_SPAN_RATIO = 0.98


class _ModelMiss(Exception):
    """モデルでは作品が取れなかった。理由を添えて classical に譲る。"""


class ArtworkDetector:
    name = "T3_artwork_detector"

    def __init__(self, config: DetectionConfig, models: ModelConfig | None = None) -> None:
        self.config = config
        self.models = models or ModelConfig()
        self._backend = "classical"
        self._device = "cpu"

    def prepare(self) -> None:
        """モデルを温める。全写真で使い回すので 1 回だけ。"""
        self._backend = "classical"
        if self.config.backend == "classical":
            return
        if not modelzoo.is_available():
            if self.config.backend == "model":
                raise RuntimeError(
                    "detection.backend が model だが torch / transformers が入っていない。"
                    " `uv sync --extra models` を実行するか backend を auto にすること。"
                )
            return

        self._device = modelzoo.resolve_device(self.models.device)
        modelzoo.load_detector(self.models.detector_model, self._device)
        modelzoo.load_segmenter(self.models.segmenter_model, self._device)
        self._backend = "model"

    def release(self) -> None:
        if self._backend == "model":
            modelzoo.release_all()
        self._backend = "classical"

    @property
    def backend(self) -> str:
        """実際に使っているバックエンド。prepare() の後に確定する。"""
        return self._backend

    def detect(
        self,
        raw: RawImage,
        mask_path: Path | None = None,
        overlay_path: Path | None = None,
    ) -> ArtworkDetection:
        image = cv2.imread(str(raw.preview_path))

        stddev = float(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).std())
        if stddev < MIN_PREVIEW_STDDEV:
            return _whole_image(
                raw, f"プレビューがほぼ一様（標準偏差 {stddev:.2f}）のため画像全体を採用"
            )

        detail: dict = {"backend": "classical"}
        if self._backend == "model":
            try:
                mask, confidence, model_detail = self._detect_with_model(image)
            except _ModelMiss as miss:
                # モデルが見つけられなかった場合は classical に落として続ける。
                # 理由は結果に残す（ログと result.json で追えるように）
                detail["model_miss"] = str(miss)
            else:
                return self._finish(
                    raw, image, mask, confidence, model_detail, mask_path, overlay_path
                )

        mask = _recover_branches(image, _segment(image))
        return self._finish(raw, image, mask, None, detail, mask_path, overlay_path)

    def _finish(
        self,
        raw: RawImage,
        image: np.ndarray,
        mask: np.ndarray,
        confidence: float | None,
        detail: dict,
        mask_path: Path | None,
        overlay_path: Path | None,
    ) -> ArtworkDetection:
        area_ratio = float(np.count_nonzero(mask)) / mask.size
        if not (MIN_AREA_RATIO <= area_ratio <= MAX_AREA_RATIO):
            return _whole_image(
                raw,
                f"作品領域の面積比 {area_ratio:.2f} が想定外のため画像全体を採用",
                detail=detail,
            )

        bbox = _bounding_box(mask)
        if bbox is None:
            return _whole_image(raw, "マスクが空のため画像全体を採用", detail=detail)

        if mask_path is not None:
            cv2.imwrite(str(mask_path), mask)
        if overlay_path is not None:
            overlay.save(overlay.draw_box(overlay.draw_mask(image, mask), bbox), overlay_path)

        return ArtworkDetection(
            status=Status.SUCCESS,
            mask_path=mask_path,
            bbox=bbox,
            confidence=confidence if confidence is not None else _confidence(mask, area_ratio),
            detail={**detail, "area_ratio": round(area_ratio, 4)},
            overlay_path=overlay_path,
        )

    def _detect_with_model(self, image: np.ndarray) -> tuple[np.ndarray, float, dict]:
        """テキスト指示で位置を出し、その枠を種にして領域を切る。

        取れなければ _ModelMiss を投げる。

        枠は「作品全体」を指す 1 本を主にして、そこに重なる部品の枠だけを足す。
        重ならない枠を足すと、隣に並んだ別の作品まで枠に入る（実写で踏んだ）。
        """
        boxes = self._locate(image)
        if not boxes:
            raise _ModelMiss("位置推定が枠を 1 つも返さない")

        primary = max(boxes, key=lambda b: b[4])
        merged = list(primary[:4])
        for box in boxes:
            if box is primary or not _intersects(merged, box[:4]):
                continue
            merged = [
                min(merged[0], box[0]),
                min(merged[1], box[1]),
                max(merged[2], box[2]),
                max(merged[3], box[3]),
            ]

        mask = self._segment_with_model(image, merged)
        if mask is None or not mask.any():
            raise _ModelMiss(f"領域抽出が空（位置推定の確信度 {primary[4]:.2f}）")

        # 位置推定の確信度が低いと、SAM 2.1 が種にした枠の中の細部だけを拾って
        # マスクが極端に痩せる（実写で面積比 0.005 まで落ちた）。
        # そのまま返すと classical より悪い結果で上書きしてしまうので、
        # 想定外の面積なら「モデルでは取れなかった」として classical に譲る。
        cleaned = _clean(mask)
        area_ratio = float(np.count_nonzero(cleaned)) / cleaned.size
        if not (MIN_AREA_RATIO <= area_ratio <= MAX_AREA_RATIO):
            raise _ModelMiss(
                f"面積比 {area_ratio:.3f} が想定外（位置推定の確信度 {primary[4]:.2f}）"
            )

        detail = {
            "backend": "model",
            "detector": self.models.detector_model,
            "segmenter": self.models.segmenter_model,
            "device": self._device,
            "detector_score": round(float(primary[4]), 4),
            "detector_label": primary[5],
            "box_count": len(boxes),
        }
        return (cleaned, float(primary[4]), detail)

    def _locate(self, image: np.ndarray) -> list[tuple[float, float, float, float, float, str]]:
        """テキスト指示に合う枠を (left, top, right, bottom, score, label) で返す。"""
        import torch
        from PIL import Image

        processor, model = modelzoo.load_detector(self.models.detector_model, self._device)
        pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        inputs = processor(
            images=pil, text=self.models.detector_prompt, return_tensors="pt"
        ).to(self._device)
        with torch.no_grad():
            outputs = model(**inputs)

        found = processor.post_process_grounded_object_detection(
            outputs,
            inputs["input_ids"],
            threshold=self.models.detector_threshold,
            text_threshold=self.models.detector_threshold,
            target_sizes=[image.shape[:2]],
        )[0]

        return [
            (float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(s), str(label))
            for b, s, label in zip(found["boxes"], found["scores"], found["text_labels"])
        ]

    def _segment_with_model(self, image: np.ndarray, box: list[float]) -> np.ndarray | None:
        """枠を種にして領域を切り出す。"""
        import torch
        from PIL import Image

        processor, model = modelzoo.load_segmenter(self.models.segmenter_model, self._device)
        pil = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
        inputs = processor(images=pil, input_boxes=[[box]], return_tensors="pt").to(self._device)
        with torch.no_grad():
            outputs = model(**inputs, multimask_output=False)

        masks = processor.post_process_masks(outputs.pred_masks.cpu(), inputs["original_sizes"])
        if not len(masks) or not len(masks[0]):
            return None
        raw_mask = masks[0][0].numpy()
        if raw_mask.ndim == 3:
            raw_mask = raw_mask[0]
        return (raw_mask > 0).astype(np.uint8) * 255


def _intersects(a: list[float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _segment(image: np.ndarray) -> np.ndarray:
    """背景幕から浮いている部分を作品として切り出す。

    展示写真は作品の背後に無地の幕が張ってあり、画面の大半をその幕が占める。
    GrabCut は「中央にある高コントラストの塊」を取るため、作品ではなく幕そのものを
    拾ってしまう（実写 9 枚すべてで確認。隣の展示まで枠に入った）。
    そこで幕の色を推定し、そこから離れた色と、幕には無い細かい濃淡を手掛かりにする。

    花器は彩度が低く滑らかなことが多いので、彩度ではなく幕からの色差で測る。
    灰色の陶器でも幕との明度差で拾える。
    """
    height, width = image.shape[:2]
    margin_x = int(width * (1.0 - INNER_REGION_RATIO) / 2.0)
    margin_y = int(height * (1.0 - INNER_REGION_RATIO) / 2.0)
    inner = np.zeros((height, width), bool)
    inner[margin_y : height - margin_y, margin_x : width - margin_x] = True

    # 幕の色。作品より幕のほうが広いので、中央領域の中央値がほぼ幕の色になる。
    lab = cv2.cvtColor(
        cv2.GaussianBlur(image, (0, 0), BACKDROP_BLUR_SIGMA), cv2.COLOR_BGR2LAB
    ).astype(np.float32)
    backdrop = np.array([np.median(lab[:, :, c][inner]) for c in range(3)], np.float32)
    color_distance = np.linalg.norm(lab - backdrop, axis=2)

    # 幕は滑らか、葉や花は細かい。ぼかす前後の差をその細かさの目安にする。
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    texture = cv2.absdiff(gray, cv2.GaussianBlur(gray, (0, 0), TEXTURE_BLUR_SIGMA))

    score = _normalized(color_distance) + _normalized(texture.astype(np.float32))
    threshold = max(
        float(np.percentile(score[inner], ARTWORK_PERCENTILE)), ARTWORK_MIN_SCORE
    )

    above = ((score >= threshold).astype(np.uint8)) * 255
    seed = np.where(inner, above, 0).astype(np.uint8)
    return _keep_artwork_components(_clean(_grow_outward(seed, above)))


def _normalized(values: np.ndarray) -> np.ndarray:
    """0.0–1.0 に伸ばす。色差と質感を足し合わせる前に尺度を揃える。"""
    return cv2.normalize(values, None, 0.0, 1.0, cv2.NORM_MINMAX)


def _grow_outward(seed: np.ndarray, region: np.ndarray) -> np.ndarray:
    """seed に触れている region の連結成分を丸ごと残す。

    中央領域は作品を見つけるためのもので、そこで切り落とすためではない。
    台に置かれた花器のように作品が中央領域からはみ出しても、
    中で見つかった部分と繋がっていれば外側まで残す。
    """
    count, labels = cv2.connectedComponents(region, connectivity=8)
    if count <= 1:
        return seed
    touched = np.unique(labels[seed > 0])
    touched = touched[touched != 0]
    if touched.size == 0:
        return seed
    return np.where(np.isin(labels, touched), 255, 0).astype(np.uint8)


def _clean(mask: np.ndarray) -> np.ndarray:
    """細かい穴と粒ノイズを均す。枝を消さないようカーネルは小さく保つ。

    2048 px のプレビューと 640 px の合成画像で同じ効き方をするよう、
    カーネルは画像の長辺に比例させる。
    """
    unit = max(3, int(max(mask.shape) * MORPH_KERNEL_RATIO) | 1)
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((unit, unit), np.uint8), iterations=3)
    opened = max(3, (unit // 2) | 1)
    return cv2.morphologyEx(closed, cv2.MORPH_OPEN, np.ones((opened, opened), np.uint8), iterations=2)


def _keep_artwork_components(mask: np.ndarray) -> np.ndarray:
    """作品を構成する連結成分をまとめて残す。壁の模様などは落とす。

    中央寄りの主領域を核にして、その近傍にある成分を同じ作品として吸収する。
    花と花器をつなぐ茎は細く、GrabCut や形態素処理で切れることがある。
    主領域だけを残すと花器が丸ごと落ちるので、近さを基準に拾い直す。
    """
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count <= 1:
        return mask

    height, width = mask.shape
    center = np.array([width / 2.0, height / 2.0])

    candidates = [
        label for label in range(1, count) if not _spans_frame(stats, label, height, width)
    ]
    if not candidates:
        return mask

    core = None
    best_score = -1.0
    for label in candidates:
        area = stats[label, cv2.CC_STAT_AREA]
        distance = np.linalg.norm(centroids[label] - center)
        # 大きくて中央に近いものを選ぶ
        score = area / (1.0 + distance)
        if score > best_score:
            best_score, core = score, label

    gap_limit = max(height, width) * MERGE_GAP_RATIO
    min_area = mask.size * MIN_COMPONENT_AREA_RATIO
    core_bounds = _component_bounds(stats, core)

    # 主領域からの距離だけで決める。併合しながら範囲を広げていくと、
    # 名札のような小さな成分が飛び石になって壁まで繋がってしまう（実写で踏んだ）。
    kept = {core}
    for label in candidates:
        if label == core or stats[label, cv2.CC_STAT_AREA] < min_area:
            continue
        if _bounds_gap(core_bounds, _component_bounds(stats, label)) <= gap_limit:
            kept.add(label)

    return np.where(np.isin(labels, list(kept)), 255, 0).astype(np.uint8)


def _spans_frame(stats: np.ndarray, label: int, height: int, width: int) -> bool:
    """画面を縦または横に貫いているか。貫いていれば壁や什器とみなす。"""
    return (
        stats[label, cv2.CC_STAT_HEIGHT] >= height * FRAME_SPAN_RATIO
        or stats[label, cv2.CC_STAT_WIDTH] >= width * FRAME_SPAN_RATIO
    )


def _component_bounds(stats: np.ndarray, label: int) -> tuple[int, int, int, int]:
    """連結成分の外接矩形を (left, top, right, bottom) で返す。"""
    left = int(stats[label, cv2.CC_STAT_LEFT])
    top = int(stats[label, cv2.CC_STAT_TOP])
    return (
        left,
        top,
        left + int(stats[label, cv2.CC_STAT_WIDTH]),
        top + int(stats[label, cv2.CC_STAT_HEIGHT]),
    )


def _bounds_gap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """2 つの外接矩形の隙間。重なっていれば 0。"""
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return math.hypot(dx, dy)


def _recover_branches(image: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """主領域の周囲にあるエッジを枝とみなして拾い直す。

    細い枝は面積が小さく前景から漏れるため、主領域の近傍に限ってエッジを足す。
    背景の壁の直線まで拾わないよう、探索範囲を主領域の近くに絞る。
    """
    if not mask.any():
        return mask

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(cv2.bilateralFilter(gray, 5, 50, 50), 60, 160)

    radius = int(max(image.shape[:2]) * BRANCH_SEARCH_RATIO)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius | 1, radius | 1))
    search_area = cv2.dilate(mask, kernel, iterations=1)

    nearby_edges = cv2.bitwise_and(edges, edges, mask=search_area)
    combined = cv2.bitwise_or(mask, nearby_edges)
    return _keep_artwork_components(_clean(combined))


def _bounding_box(mask: np.ndarray) -> Box | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return Box(
        space=Space.PREVIEW,
        left=float(xs.min()),
        top=float(ys.min()),
        right=float(xs.max()),
        bottom=float(ys.max()),
    )


def _confidence(mask: np.ndarray, area_ratio: float) -> float:
    """画像端に触れているほど、作品がはみ出している疑いがあるので下げる。"""
    touches = (
        mask[0, :].any() or mask[-1, :].any() or mask[:, 0].any() or mask[:, -1].any()
    )
    base = 0.85 if MIN_AREA_RATIO * 2 <= area_ratio <= MAX_AREA_RATIO / 2 else 0.6
    return base * (0.5 if touches else 1.0)


def _whole_image(
    raw: RawImage, note: str, detail: dict | None = None
) -> ArtworkDetection:
    """検出できないときのフォールバック。作品を切り落とさない側に倒す。"""
    return ArtworkDetection(
        status=Status.FALLBACK,
        bbox=Box(
            space=Space.PREVIEW,
            left=0.0,
            top=0.0,
            right=float(raw.preview_size.width),
            bottom=float(raw.preview_size.height),
        ),
        confidence=0.0,
        detail=detail or {"backend": "classical"},
        note=note,
    )
