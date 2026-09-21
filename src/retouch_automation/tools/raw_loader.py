"""T1 RAW読み込み。

RAW から解析用のプレビュー JPEG を作り、以降の全ツールが使う RawImage を返す。
座標変換に必要な情報（サイズ・Orientation・スケール）をここで確定させる。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import numpy as np
import rawpy
from PIL import Image

from ..config import PreviewConfig
from ..models import RawImage, Size, Status

RAW_SUFFIXES = (".arw",)


def find_raw_files(raw_dir: Path) -> list[Path]:
    """対象 RAW を名前順に列挙する。"""
    return sorted(p for p in raw_dir.iterdir() if p.suffix.lower() in RAW_SUFFIXES)


class RawLoader:
    name = "T1_raw_loader"

    def __init__(self, config: PreviewConfig) -> None:
        self.config = config

    def prepare(self) -> None:
        return None

    def release(self) -> None:
        return None

    def load(self, raw_path: Path, preview_path: Path) -> RawImage:
        with rawpy.imread(str(raw_path)) as raw:
            sensor_size = Size(width=raw.sizes.raw_width, height=raw.sizes.raw_height)
            as_shot = [float(v) for v in raw.camera_whitebalance[:3]]
            captured_at = _captured_at(raw, raw_path)
            iso, shutter, aperture = _exposure(raw)
            rgb = self._develop_for_analysis(raw)

        # IMAGE 空間の寸法は postprocess の結果そのものから取る。
        # raw.sizes.width/height は回転前の値なので、縦位置の写真では縦横が逆になる。
        # ここを raw.sizes から取ると preview_scale と CROP_NORM が両方狂う。
        image_size = Size(width=rgb.shape[1], height=rgb.shape[0])

        preview = self._shrink(rgb)
        preview.save(preview_path, quality=self.config.jpeg_quality)

        return RawImage(
            photo_id=raw_path.stem,
            raw_path=raw_path,
            preview_path=preview_path,
            sensor_size=sensor_size,
            image_size=image_size,
            preview_size=Size(width=preview.width, height=preview.height),
            # rawpy の postprocess は Orientation を適用済みの向きで返すため、
            # IMAGE 空間はこの時点で「見た目どおり」になっている。
            orientation=1,
            preview_scale=image_size.width / preview.width,
            captured_at=captured_at,
            iso=iso,
            shutter_speed=shutter,
            aperture=aperture,
            as_shot_neutral=as_shot,
            status=Status.SUCCESS,
        )

    def _develop_for_analysis(self, raw: rawpy.RawPy) -> np.ndarray:
        """解析用のニュートラル現像。見栄えではなく再現性を優先する。"""
        return raw.postprocess(
            use_camera_wb=True,
            no_auto_bright=True,
            output_bps=8,
        )

    def _shrink(self, rgb: np.ndarray) -> Image.Image:
        image = Image.fromarray(rgb)
        long_edge = max(image.width, image.height)
        if long_edge <= self.config.long_edge:
            return image
        scale = self.config.long_edge / long_edge
        size = (round(image.width * scale), round(image.height * scale))
        return image.resize(size, Image.LANCZOS)


def _captured_at(raw: rawpy.RawPy, raw_path: Path) -> datetime:
    """撮影日時。T2 はこの間隔で作品の切れ目を見るので、実際の撮影時刻が要る。

    ファイルの更新時刻で代用すると、コピーや書き出しで全ファイルが同じ時刻になり、
    T2 が全写真を 1 つの作品として束ねてしまう（実際に踏んだ）。
    """
    stamp = getattr(raw.other, "timestamp", None)
    if isinstance(stamp, datetime):
        return stamp
    if isinstance(stamp, (int, float)) and stamp > 0:
        return datetime.fromtimestamp(stamp)
    return datetime.fromtimestamp(raw_path.stat().st_mtime)


def _exposure(raw: rawpy.RawPy) -> tuple[int | None, float | None, float | None]:
    """ISO・シャッター速度・絞り。取れなければ None。"""
    other = raw.other

    def value(name: str) -> float | None:
        raw_value = getattr(other, name, None)
        if raw_value is None:
            return None
        number = float(raw_value)
        return number if number > 0 else None

    iso = value("iso_speed")
    return (int(iso) if iso is not None else None, value("shutter_speed"), value("aperture"))
