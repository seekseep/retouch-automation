"""モデルの取得・デバイス選択・使い回しを一箇所にまとめる。

torch と transformers は optional extra（`uv sync --extra models`）なので、
この モジュールの外から import するときは必ず `is_available()` を先に見ること。
入っていない環境でも classical バックエンドだけで全工程が通る状態を保つ。

読み込んだモデルはプロセス内で使い回す。写真ごとに読み直すと数十秒が毎回乗る。
各ツールの prepare() で温めて release() で放す。

Apple Silicon では MPS を使う。使えない環境では CPU に落ちる（処理は止めず、ログに残す）。
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: モデル ID とデバイスの組ごとに 1 個だけ持つ
_CACHE: dict[tuple[str, str, str], Any] = {}


def is_available() -> bool:
    """モデル用の依存が入っているか。"""
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_device(preference: str = "auto") -> str:
    """実行デバイスを決める。

    "auto" なら MPS が使えれば MPS、無ければ CPU。
    明示指定した デバイスが使えない場合も CPU に落とす（処理は止めない）。
    """
    import torch

    if preference == "auto":
        device = "mps" if torch.backends.mps.is_available() else "cpu"
    elif preference == "mps" and not torch.backends.mps.is_available():
        device = "cpu"
    elif preference == "cuda" and not torch.cuda.is_available():
        device = "cpu"
    else:
        device = preference

    if preference not in ("auto", device):
        logger.warning("デバイス %s が使えないため CPU で動かす", preference)
    return device


def load_embedder(model_id: str, device: str) -> tuple[Any, Any]:
    """T2 の特徴量抽出器（DINOv2）。"""
    return _load("embedder", model_id, device)


def load_detector(model_id: str, device: str) -> tuple[Any, Any]:
    """T3 のテキスト指示による位置推定器。"""
    return _load("detector", model_id, device)


def load_segmenter(model_id: str, device: str) -> tuple[Any, Any]:
    """T3 の領域抽出器（SAM 2.1）。"""
    return _load("segmenter", model_id, device)


def release_all() -> None:
    """読み込んだモデルを全部捨てる。"""
    _CACHE.clear()
    try:
        import gc

        import torch

        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except ImportError:
        return


def _load(kind: str, model_id: str, device: str) -> tuple[Any, Any]:
    key = (kind, model_id, device)
    if key in _CACHE:
        return _CACHE[key]

    logger.info("モデル読み込み %s: %s（%s）", kind, model_id, device)
    started = time.perf_counter()

    from transformers import (
        AutoImageProcessor,
        AutoModel,
        AutoModelForZeroShotObjectDetection,
        AutoProcessor,
        Sam2Model,
        Sam2Processor,
    )

    if kind == "embedder":
        processor = AutoImageProcessor.from_pretrained(model_id)
        model = AutoModel.from_pretrained(model_id)
    elif kind == "detector":
        processor = AutoProcessor.from_pretrained(model_id)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(model_id)
    elif kind == "segmenter":
        processor = Sam2Processor.from_pretrained(model_id)
        model = Sam2Model.from_pretrained(model_id)
    else:
        raise ValueError(f"不明な種別: {kind}")

    loaded = (processor, model.to(device).eval())
    _CACHE[key] = loaded
    logger.debug("モデル読み込み %s 完了 %.1f 秒", kind, time.perf_counter() - started)
    return loaded
