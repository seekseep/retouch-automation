"""実行設定。余白量やしきい値は全部ここに出す（コードに数値を埋めない）。"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field


class PreviewConfig(BaseModel):
    #: 解析用画像の長辺（px）。細い枝の検出に必要な下限は Phase 0 で確認する。
    long_edge: int = 2048
    jpeg_quality: int = 92
    #: RAW 内の埋め込み JPEG を使うか。向き・画角の一致を確認できるまで False。
    use_embedded_jpeg: bool = False


class ModelConfig(BaseModel):
    """モデルの取得元と実行デバイス。

    依存は optional extra なので、入っていない環境では各ツールが
    自動的に classical バックエンドへ落ちる。
    """

    #: "auto" なら MPS が使えれば MPS、無ければ CPU。"cpu" で固定もできる
    device: str = "auto"
    #: T2 の特徴量（作品の見た目を比べる）
    embedding_model: str = "facebook/dinov2-base"
    #: T3 のテキスト指示による位置推定
    #:
    #: 設計では Florence-2 を想定していたが、Microsoft が公開している 4 つの
    #: リポジトリはいずれも旧 remote-code 形式のままで、transformers 5 系の
    #: native 実装が要求する image_token を持たず読み込めない。
    #: 同じ「テキストで指示した物体の位置を出す」仕事をする GroundingDINO に置き換えた。
    #: 詳細は docs/DESIGN.md の 5.7。
    detector_model: str = "IDEA-Research/grounding-dino-base"
    #: T3 に渡すテキスト指示。小文字、句点区切りが GroundingDINO の作法
    detector_prompt: str = "a flower arrangement. a vase. flowers. branches."
    #: 位置推定の採用しきい値
    detector_threshold: float = 0.25
    #: T3 の領域抽出
    segmenter_model: str = "facebook/sam2.1-hiera-base-plus"


class DetectionConfig(BaseModel):
    #: 検出のやり方
    #:   auto      … モデルが使えれば model、無ければ classical（既定）
    #:   model     … GroundingDINO + SAM 2.1
    #:   classical … 背景幕からの色差＋質感。追加のモデル不要
    backend: str = "auto"
    #: 検出した作品領域に加える安全余白（作品サイズに対する比率）
    safety_margin_ratio: float = 0.06
    #: この値未満の確信度では、より広い領域を採用する
    low_confidence_threshold: float = 0.5


class StraightenConfig(BaseModel):
    #: この角度を超える推定は信用しない（度）
    max_abs_angle: float = 8.0
    #: 作品マスクをこの比率だけ膨らませて、水平推定の対象から除外する
    mask_dilate_ratio: float = 0.04
    #: 基準線がこの本数に満たなければ角度 0 にする
    min_lines: int = 3


class CropConfig(BaseModel):
    #: 作品の周囲に残す構図上の余白（作品サイズに対する比率）
    composition_margin_ratio: float = 0.12
    #: 作品が画像端からこの比率以内にある場合は、ほぼ全画面を採用する
    edge_proximity_ratio: float = 0.03
    #: 作品を切り落とすくらいなら黒い余白を許容する
    allow_void: bool = True


class DevelopConfig(BaseModel):
    #: 補正値を XMP に反映するか。
    #:
    #: 反映するのは Highlights2012 と Shadows2012 の 2 項目だけで、しかも
    #: 作品領域の 0.5% 超が輝度の端に張り付いているときしか動かない。
    #: 露出・ホワイトバランス・コントラストは根拠が足りないのでニュートラルのまま。
    #: False にすると算出はするが XMP には書かず、detail に記録だけ残す。
    enabled: bool = True


class ClassifyConfig(BaseModel):
    #: 特徴量の出し方
    #:   auto      … モデルが使えれば model、無ければ classical（既定）
    #:   model     … DINOv2 の画像特徴量
    #:   classical … 色ヒストグラム＋縮小画像。実写では当てにならない
    backend: str = "auto"


class XmpConfig(BaseModel):
    #: 既存 XMP の扱い
    #:   skip      … 既存があれば触らない（既定）
    #:   merge     … crs: の設定と dc:subject だけ差し替え、他のメタデータは残す
    #:   overwrite … 丸ごと置き換える（Lightroom が書いた EXIF 等は失われる）
    #:
    #: 既定が skip なのは、人が Lightroom で直した回転・トリミングを守るため。
    #: このシステムは「最後は人が Lightroom で確認する」前提なので、
    #: 再実行のたびに人の手直しが消えると運用が壊れる。
    #: ツールを改良して全部作り直したいときは --overwrite-xmp を付ける。
    on_existing: str = "skip"
    #: 全写真に付ける固定タグ
    base_tags: list[str] = Field(default_factory=lambda: ["ikebana"])
    group_tag_prefix: str = "artwork_"
    #: audit --tag が、採否を見直すべき作品の写真に付けるキーワード
    review_tag: str = "確認が必要"


class Config(BaseModel):
    work_dir_name: str = ".retouch-automation"
    models: ModelConfig = Field(default_factory=ModelConfig)
    preview: PreviewConfig = Field(default_factory=PreviewConfig)
    classify: ClassifyConfig = Field(default_factory=ClassifyConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    straighten: StraightenConfig = Field(default_factory=StraightenConfig)
    crop: CropConfig = Field(default_factory=CropConfig)
    develop: DevelopConfig = Field(default_factory=DevelopConfig)
    xmp: XmpConfig = Field(default_factory=XmpConfig)
    #: 確認用画像を出すか
    write_overlays: bool = True

    @classmethod
    def load(cls, path: Path | None) -> Config:
        if path is None:
            return cls()
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
