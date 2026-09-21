"""ツール間で受け渡す共通データモデル。

ここには具体的な画像認識モデル名（Florence-2 / SAM 2.1 / DINOv2）を出さない。
モデルを差し替えても、この契約は変わらないようにする。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class Status(StrEnum):
    """部分結果の状態。

    SUCCESS  … 正常に推定できた
    FALLBACK … 推定できず、保守的な既定値を採用した（処理は継続、要確認としてレポートに残す）
    FAILED   … 当該ツールが失敗した（他のツールは継続する）
    """

    SUCCESS = "success"
    FALLBACK = "fallback"
    FAILED = "failed"


class Space(StrEnum):
    """座標空間。矩形や点は必ずどの空間の値かを持つ。

    SENSOR  … RAW の有効画素領域。EXIF Orientation 適用前
    IMAGE   … Orientation 適用後の「見た目どおり」の画像
    PREVIEW … 解析用に縮小した画像。画像解析は全てここで行う
    """

    SENSOR = "sensor"
    IMAGE = "image"
    PREVIEW = "preview"


class Size(BaseModel):
    width: int
    height: int


class Box(BaseModel):
    """矩形。どの座標空間の値かを必ず持つ。"""

    space: Space
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[float, float]:
        return ((self.left + self.right) / 2.0, (self.top + self.bottom) / 2.0)

    def clamped(self, size: Size) -> Box:
        return Box(
            space=self.space,
            left=max(0.0, min(self.left, size.width)),
            top=max(0.0, min(self.top, size.height)),
            right=max(0.0, min(self.right, size.width)),
            bottom=max(0.0, min(self.bottom, size.height)),
        )


# --- T1 ---------------------------------------------------------------


class RawImage(BaseModel):
    """T1 の出力。以降の全ツールの起点になる。"""

    photo_id: str
    raw_path: Path
    preview_path: Path

    sensor_size: Size
    image_size: Size
    preview_size: Size

    #: EXIF Orientation（1–8）。IMAGE ⇄ SENSOR の変換に使う
    orientation: int = 1
    #: PREVIEW → IMAGE のスケール（preview の 1px が image の何 px か）
    preview_scale: float

    captured_at: datetime | None = None
    camera_model: str | None = None
    lens_model: str | None = None
    iso: int | None = None
    shutter_speed: float | None = None
    aperture: float | None = None
    #: RAW のカメラ設定ホワイトバランス（あれば）
    as_shot_neutral: list[float] | None = None

    status: Status = Status.SUCCESS
    note: str | None = None


# --- T3 ---------------------------------------------------------------


class ArtworkDetection(BaseModel):
    """作品（花・葉・枝・花器を含む全体）の領域。"""

    status: Status
    #: PREVIEW 空間のマスク画像（PNG, 0/255）
    mask_path: Path | None = None
    #: 作品全体を囲む矩形（PREVIEW 空間）
    bbox: Box | None = None
    #: 0.0–1.0。低いほど保守的に広く残す
    confidence: float = 0.0
    #: 検出に使った手法・プロンプト等の記録
    detail: dict[str, object] = Field(default_factory=dict)
    overlay_path: Path | None = None
    note: str | None = None


# --- T2 ---------------------------------------------------------------


class Classification(BaseModel):
    group_id: str
    tags: list[str]
    status: Status = Status.SUCCESS
    #: 同一作品と判定した根拠（類似度・時刻差など）
    detail: dict[str, object] = Field(default_factory=dict)
    note: str | None = None


# --- T4 ---------------------------------------------------------------


class ReferenceLine(BaseModel):
    """水平推定に使った背景の基準線（PREVIEW 空間）。"""

    x1: float
    y1: float
    x2: float
    y2: float
    #: "horizontal" / "vertical"
    kind: str
    angle_degrees: float


class Straightening(BaseModel):
    status: Status
    #: 画像を水平にするために必要な回転角（度）。CCW 正。
    #: XMP へ書く際の符号は geometry.to_crop_angle() に閉じ込める。
    angle_degrees: float = 0.0
    confidence: float = 0.0
    #: 水平線・垂直線それぞれから求めた角度（度、CCW 正）。その系の線が無ければ None。
    #: 両者の食い違いが確信度になる。傾きの取り違えを調べるときはここを見る。
    horizontal_degrees: float | None = None
    vertical_degrees: float | None = None
    lines: list[ReferenceLine] = Field(default_factory=list)
    overlay_path: Path | None = None
    note: str | None = None


# --- T5 ---------------------------------------------------------------


class Crop(BaseModel):
    status: Status
    #: 適用する回転角（度、CCW 正）。XMP へ書く際の符号は geometry が決める。
    angle_degrees: float = 0.0
    #: XMP に書く正規化座標（0.0–1.0）。定義は Phase 0 で確定する。
    left: float = 0.0
    top: float = 0.0
    right: float = 1.0
    bottom: float = 1.0
    #: 合わせた比率プリセット（"4x5 縦" など）。全画面や自由比率のときは None
    aspect_ratio: str | None = None
    #: 回転後の有効画像領域と作品全体を同時に保持できたか
    keeps_artwork: bool = True
    contains_void: bool = False
    overlay_path: Path | None = None
    note: str | None = None

    @property
    def is_full_frame(self) -> bool:
        """全画面かつ無回転。この場合は XMP に HasCrop=False を書く。"""
        return (self.left, self.top, self.right, self.bottom) == (0.0, 0.0, 1.0, 1.0) and (
            self.angle_degrees == 0.0
        )


# --- T6 ---------------------------------------------------------------


class Develop(BaseModel):
    """RAW 現像用の補正値。全て Adobe Camera Raw のスケールに合わせる。

    判断根拠が不十分な項目はニュートラル（0 / None）のままにする。
    """

    status: Status = Status.FALLBACK
    exposure2012: float = 0.0       # EV, -5.0..+5.0
    contrast2012: int = 0           # -100..+100
    highlights2012: int = 0
    shadows2012: int = 0
    whites2012: int = 0
    blacks2012: int = 0
    vibrance: int = 0
    saturation: int = 0
    #: None ならカメラ設定のまま（XMP に WB を書かない）
    temperature: int | None = None
    tint: int | None = None
    detail: dict[str, object] = Field(default_factory=dict)
    note: str | None = None


# --- 写真ごとの集約 -----------------------------------------------------


class PhotoResult(BaseModel):
    """1 枚の写真に対する全ツールの結果。JSON で永続化し、再実行判定に使う。"""

    photo_id: str
    raw_path: Path
    xmp_path: Path

    raw: RawImage | None = None
    classification: Classification | None = None
    detection: ArtworkDetection | None = None
    straightening: Straightening | None = None
    crop: Crop | None = None
    develop: Develop | None = None

    #: ツール名 -> エラー内容
    errors: dict[str, str] = Field(default_factory=dict)
    xmp_written: bool = False

    @property
    def needs_review(self) -> bool:
        """人間の確認が要る写真か。"""
        if self.errors:
            return True
        parts = (self.classification, self.detection, self.straightening, self.crop, self.develop)
        return any(p is not None and p.status is not Status.SUCCESS for p in parts)
