"""T6 レタッチ。

作品領域と背景を区別して解析し、花材本来の色と質感を保つ補正値を出す。

避けること:
  - 白い花の白飛び
  - 暗い枝や葉の不自然な持ち上げ
  - 彩度の過剰な補正

解析用 JPEG の明るさから Exposure2012 を単純換算しない。
ここで出すのは「どれだけ輝度の端に張り付いているか」という統計に基づく
保守的な値だけで、露出そのものは既定で動かさない。

判断根拠が不十分な項目はニュートラルのままにする。
実際に XMP へ出すのは Highlights2012 と Shadows2012 の 2 項目だけ。
どちらも「作品領域の 0.5% 超が輝度の端に張り付いている」ときしか動かず、
補正量にも上限を置いてある。

config.develop.enabled を False にすると、算出結果を detail に記録するだけで
XMP には反映しない。露出やホワイトバランスまで踏み込むときは、
RAW 現像結果と突き合わせて妥当性を確認してから項目を増やすこと。
"""

from __future__ import annotations

import cv2
import numpy as np

from ..config import DevelopConfig
from ..models import ArtworkDetection, Classification, Develop, RawImage, Status

#: この輝度以上を「白飛びしかけ」とみなす（0–255）
HIGHLIGHT_LEVEL = 245
#: この輝度以下を「黒つぶれしかけ」とみなす
SHADOW_LEVEL = 10

#: 作品領域のうち、この比率を超えて張り付いていたら補正する
CLIP_RATIO_THRESHOLD = 0.005

#: 補正量の上限。不自然な持ち上げを避けるため小さく抑える
MAX_HIGHLIGHT_RECOVERY = 40
MAX_SHADOW_LIFT = 20


class Developer:
    name = "T6_developer"

    def __init__(self, config: DevelopConfig) -> None:
        self.config = config

    def prepare(self) -> None:
        return None

    def release(self) -> None:
        return None

    def develop(
        self,
        raw: RawImage,
        detection: ArtworkDetection,
        classification: Classification | None,
    ) -> Develop:
        stats = _measure(raw, detection)
        proposal = _propose(stats)

        if not self.config.enabled:
            return Develop(
                status=Status.FALLBACK,
                detail={"measured": stats, "proposal": proposal},
                note="develop.enabled が False のため XMP には反映しない",
            )

        return Develop(
            status=Status.SUCCESS,
            highlights2012=proposal["highlights2012"],
            shadows2012=proposal["shadows2012"],
            detail={"measured": stats},
        )


def _measure(raw: RawImage, detection: ArtworkDetection) -> dict[str, float]:
    """作品領域と背景を分けて輝度分布を測る。"""
    image = cv2.imread(str(raw.preview_path))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    artwork = _artwork_pixels(gray, detection)

    return {
        "artwork_median": float(np.median(artwork)),
        "artwork_p99": float(np.percentile(artwork, 99)),
        "artwork_p01": float(np.percentile(artwork, 1)),
        "highlight_clip_ratio": float(np.mean(artwork >= HIGHLIGHT_LEVEL)),
        "shadow_clip_ratio": float(np.mean(artwork <= SHADOW_LEVEL)),
        "background_median": float(np.median(gray)),
    }


def _artwork_pixels(gray: np.ndarray, detection: ArtworkDetection) -> np.ndarray:
    if detection.bbox is None:
        return gray.flatten()
    box = detection.bbox
    region = gray[
        max(0, int(box.top)) : int(box.bottom), max(0, int(box.left)) : int(box.right)
    ]
    return region.flatten() if region.size else gray.flatten()


def _propose(stats: dict[str, float]) -> dict[str, int]:
    """張り付き具合から保守的な補正量を出す。上限で必ず頭打ちにする。"""
    highlights = 0
    if stats["highlight_clip_ratio"] > CLIP_RATIO_THRESHOLD:
        strength = min(1.0, stats["highlight_clip_ratio"] / 0.05)
        highlights = -round(MAX_HIGHLIGHT_RECOVERY * strength)

    shadows = 0
    if stats["shadow_clip_ratio"] > CLIP_RATIO_THRESHOLD:
        strength = min(1.0, stats["shadow_clip_ratio"] / 0.05)
        shadows = round(MAX_SHADOW_LIFT * strength)

    return {"highlights2012": highlights, "shadows2012": shadows}
