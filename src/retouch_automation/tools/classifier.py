"""T2 写真分類。

同じ作品を撮った写真をグループ化する。

会場の背景が似ているため、画像全体の類似度で同一作品と判定してはいけない。
T3 の作品領域を切り出してから特徴量を比べ、撮影時刻の連続性と併せて判断する。

すべての写真にグループ ID を割り当てる。分類が不確実でも除外しない。

特徴量のバックエンドは 2 つある。
  model     … DINOv2 の画像特徴量（既定）
  classical … 色ヒストグラム＋縮小画像。モデル無しでも動かすための退避路。
              背景が変わらない会場では実写で役に立たなかった（下の定数のコメント参照）
"""

from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np

from .. import modelzoo
from ..config import ClassifyConfig, ModelConfig
from ..models import ArtworkDetection, Classification, RawImage, Status

#: これより長く間が空いたら、似ていても別の作品とみなす（秒）
#:
#: **時刻は主役ではなく歯止め。** 実写 2 組で測ったところ、作品の切れ目は
#: 6 秒しか空かないことがあり（同一作品内の最大 5 秒と 1 秒差）、
#: 時刻だけでは切り分けられない。主に効かせるのは特徴量の類似度のほう。
#:
#: ここで受け持つのは「見た目が似ていても、間がはっきり空いていれば別の作品」の側。
#: 実測した切れ目は 6, 6, 9, 18, 22, 58, 594 秒。同一作品内は 1〜5 秒。
#: 15 秒なら同一作品を誤って割ることはなく、長い中断は確実に拾える。
#:
#: なお classical バックエンド（モデル無し）では類似度が当てにならないので、
#: 時刻だけが頼りになり、6 秒の切れ目を取りこぼす。モデルを入れること。
TIME_GAP_SECONDS = 15.0

#: 作品領域の特徴量がこれ以上似ていれば同じ作品の候補とする（classical 用）
#:
#: 実写では当てにならない。同一作品が 0.9026〜0.9999、別作品が 0.8980〜0.9005 と
#: 境界のマージンが 0.2% しかなく、ここに閾値を引いても次の撮影会では外れる。
#: 色ヒストグラムでは会場の背景と緑の植物という共通点が勝ってしまうため。
#: classical のときは実質的に時刻だけで切れている。
SIMILARITY_THRESHOLD = 0.82

#: DINOv2 の特徴量を使うときのしきい値
#:
#: 展示写真 13 枚・6 作品で実測して決めた。連続する 12 区間の類似度は
#:
#:     同一作品内 … 0.9730 〜 0.9900（7 区間）
#:     別作品の境 … 0.0207 〜 0.4446（5 区間）
#:
#: と、あいだが 0.53 空く。両クラスタから最も離れる中点付近を採る。
#: この値と TIME_GAP_SECONDS = 15 の組み合わせで、13 枚が正解どおり 6 作品に分かれる。
#:
#: 色ヒストグラム（classical）では同じ写真で境界のマージンが 0.2% しか無かった。
#: 特徴量をモデルに替えたことで初めて分類が成立している。
MODEL_SIMILARITY_THRESHOLD = 0.70

#: 特徴量に使う縮小画像の辺
THUMBNAIL_SIZE = 32

_EPOCH = datetime.fromtimestamp(0)


class Classifier:
    name = "T2_classifier"

    def __init__(
        self,
        base_tags: list[str],
        group_tag_prefix: str,
        config: ClassifyConfig | None = None,
        models: ModelConfig | None = None,
    ) -> None:
        self.base_tags = base_tags
        self.group_tag_prefix = group_tag_prefix
        self.config = config or ClassifyConfig()
        self.models = models or ModelConfig()
        self._backend = "classical"
        self._device = "cpu"

    def prepare(self) -> None:
        self._backend = "classical"
        if self.config.backend == "classical":
            return
        if not modelzoo.is_available():
            if self.config.backend == "model":
                raise RuntimeError(
                    "classify.backend が model だが torch / transformers が入っていない。"
                    " `uv sync --extra models` を実行するか backend を auto にすること。"
                )
            return

        self._device = modelzoo.resolve_device(self.models.device)
        modelzoo.load_embedder(self.models.embedding_model, self._device)
        self._backend = "model"

    def release(self) -> None:
        self._backend = "classical"

    @property
    def backend(self) -> str:
        """実際に使っているバックエンド。prepare() の後に確定する。"""
        return self._backend

    @property
    def similarity_threshold(self) -> float:
        return (
            MODEL_SIMILARITY_THRESHOLD if self._backend == "model" else SIMILARITY_THRESHOLD
        )

    def _feature(self, raw: RawImage, detection: ArtworkDetection | None) -> np.ndarray:
        if self._backend != "model":
            return _artwork_feature(raw, detection)

        image = cv2.imread(str(raw.preview_path))
        if image is None:
            return np.zeros(1, dtype=np.float32)
        return self._embed(_crop_to_artwork(image, detection))

    def _embed(self, region: np.ndarray) -> np.ndarray:
        """作品領域の見た目を 1 本のベクトルにする。"""
        import torch
        from PIL import Image

        processor, model = modelzoo.load_embedder(self.models.embedding_model, self._device)
        pil = Image.fromarray(cv2.cvtColor(region, cv2.COLOR_BGR2RGB))
        inputs = processor(images=pil, return_tensors="pt").to(self._device)
        with torch.no_grad():
            outputs = model(**inputs)

        # CLS トークンが画像全体の要約になる
        vector = outputs.last_hidden_state[:, 0].squeeze(0).float().cpu().numpy()
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm > 0 else vector

    def group(
        self,
        raws: list[RawImage],
        detections: dict[str, ArtworkDetection],
    ) -> dict[str, Classification]:
        ordered = sorted(raws, key=lambda r: (r.captured_at or _EPOCH, r.photo_id))
        features = {r.photo_id: self._feature(r, detections.get(r.photo_id)) for r in ordered}
        threshold = self.similarity_threshold

        result: dict[str, Classification] = {}
        group_index = 0
        previous: RawImage | None = None
        similarity = 0.0

        for raw in ordered:
            if previous is None:
                group_index = 1
            else:
                similarity = _cosine(features[previous.photo_id], features[raw.photo_id])
                if _is_new_group(previous, raw, similarity, threshold):
                    group_index += 1

            group_id = f"{self.group_tag_prefix}{group_index:04d}"
            uncertain = previous is not None and abs(similarity - threshold) < 0.05

            result[raw.photo_id] = Classification(
                group_id=group_id,
                tags=[*self.base_tags, group_id],
                status=Status.FALLBACK if uncertain else Status.SUCCESS,
                detail={
                    "similarity_to_previous": round(similarity, 4),
                    "backend": self._backend,
                },
                note="類似度がしきい値付近のため要確認" if uncertain else None,
            )
            previous = raw

        return result


def _is_new_group(
    previous: RawImage, current: RawImage, similarity: float, threshold: float
) -> bool:
    if previous.captured_at is None or current.captured_at is None:
        return True
    gap = (current.captured_at - previous.captured_at).total_seconds()
    if gap > TIME_GAP_SECONDS:
        return True
    return similarity < threshold


def _artwork_feature(raw: RawImage, detection: ArtworkDetection | None) -> np.ndarray:
    """作品領域だけを切り出した特徴量。背景の影響を落とすのが狙い。"""
    image = cv2.imread(str(raw.preview_path))
    if image is None:
        return np.zeros(1, dtype=np.float32)

    region = _crop_to_artwork(image, detection)
    thumbnail = cv2.resize(region, (THUMBNAIL_SIZE, THUMBNAIL_SIZE), interpolation=cv2.INTER_AREA)

    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [24, 8], [0, 180, 0, 256])
    histogram = cv2.normalize(histogram, histogram).flatten()

    shape = cv2.cvtColor(thumbnail, cv2.COLOR_BGR2GRAY).flatten().astype(np.float32) / 255.0
    return np.concatenate([histogram, shape])


def _crop_to_artwork(image: np.ndarray, detection: ArtworkDetection | None) -> np.ndarray:
    if detection is None or detection.bbox is None:
        return image
    box = detection.bbox
    left, top = max(0, int(box.left)), max(0, int(box.top))
    right, bottom = int(box.right), int(box.bottom)
    if right - left < 8 or bottom - top < 8:
        return image
    return image[top:bottom, left:right]


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape:
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)
