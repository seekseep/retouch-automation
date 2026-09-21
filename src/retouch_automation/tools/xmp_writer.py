"""T7 XMP生成。

XMP への書き込みはこのツールだけが行う。

既存 XMP を無条件に上書きしない。Lightroom が書いた実物には EXIF・撮影情報・
xmpMM:History・crd:CameraProfile などが入っており、丸ごと置き換えると失われる。
既定は skip で、既存の XMP があれば触らない。人が Lightroom で直した内容を守るため。
merge を指定すると crs: の現像設定と dc:subject のキーワードだけを差し替える。

実物の Lightroom 9.1 が書くサイドカーを確認した結果:
  - <?xpacket?> ラッパーは付かない
  - crs:RawFileName と photoshop:SidecarForExtension を持つ
  - 設定は rdf:Description の属性として並ぶ

未検証（DESIGN.md「未確定事項」）:
  - crs:CropAngle の符号と単位
  - crs:Crop* が回転前基準か回転後基準か
  手元の実物はメタデータのみの保存でトリミングが入っておらず、照合できていない。
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree

from .. import geometry
from ..config import XmpConfig
from ..models import Crop, Develop, PhotoResult

NAMESPACES = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "crs": "http://ns.adobe.com/camera-raw-settings/1.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "aux": "http://ns.adobe.com/exif/1.0/aux/",
    "exifEX": "http://cipa.jp/exif/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "xmpMM": "http://ns.adobe.com/xap/1.0/mm/",
    "stEvt": "http://ns.adobe.com/xap/1.0/sType/ResourceEvent#",
    "crd": "http://ns.adobe.com/camera-raw-defaults/1.0/",
    "xmpDM": "http://ns.adobe.com/xmp/1.0/DynamicMedia/",
}

RDF = NAMESPACES["rdf"]
CRS = NAMESPACES["crs"]
DC = NAMESPACES["dc"]

_EMPTY = """<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="retouch-automation">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""/>
 </rdf:RDF>
</x:xmpmeta>
"""

for prefix, uri in NAMESPACES.items():
    ElementTree.register_namespace(prefix, uri)


class XmpWriter:
    name = "T7_xmp_writer"

    def __init__(self, config: XmpConfig) -> None:
        self.config = config

    def prepare(self) -> None:
        return None

    def release(self) -> None:
        return None

    def write(self, result: PhotoResult) -> bool:
        """XMP を書く。既存を温存してスキップした場合は False を返す。"""
        exists = result.xmp_path.exists()
        if exists and self.config.on_existing == "skip":
            return False

        source = (
            result.xmp_path.read_text(encoding="utf-8")
            if exists and self.config.on_existing == "merge"
            else _EMPTY
        )
        root = ElementTree.fromstring(source)
        description = _description(root)

        _apply(description, _crop_attributes(result.crop))
        _apply(description, _develop_attributes(result.develop))
        description.set(f"{{{CRS}}}RawFileName", result.raw_path.name)

        tags = result.classification.tags if result.classification else []
        _set_subject(description, tags)

        result.xmp_path.write_text(
            ElementTree.tostring(root, encoding="unicode") + "\n", encoding="utf-8"
        )
        return True


def _description(root: ElementTree.Element) -> ElementTree.Element:
    description = root.find(f".//{{{RDF}}}Description")
    if description is None:
        raise ValueError("rdf:Description が見つからない XMP")
    return description


def _apply(description: ElementTree.Element, attributes: dict[str, str]) -> None:
    for name, value in attributes.items():
        description.set(f"{{{CRS}}}{name}", value)


def _crop_attributes(crop: Crop | None) -> dict[str, str]:
    if crop is None or crop.is_full_frame:
        return {"HasCrop": "False"}
    return {
        "HasCrop": "True",
        "CropTop": f"{crop.top:.6f}",
        "CropLeft": f"{crop.left:.6f}",
        "CropBottom": f"{crop.bottom:.6f}",
        "CropRight": f"{crop.right:.6f}",
        "CropAngle": f"{geometry.to_crop_angle(crop.angle_degrees):.4f}",
    }


def _develop_attributes(develop: Develop | None) -> dict[str, str]:
    if develop is None:
        return {}

    attributes = {
        "Exposure2012": f"{develop.exposure2012:+.2f}",
        "Contrast2012": f"{develop.contrast2012:+d}",
        "Highlights2012": f"{develop.highlights2012:+d}",
        "Shadows2012": f"{develop.shadows2012:+d}",
        "Whites2012": f"{develop.whites2012:+d}",
        "Blacks2012": f"{develop.blacks2012:+d}",
        "Vibrance": f"{develop.vibrance:+d}",
        "Saturation": f"{develop.saturation:+d}",
    }
    # ホワイトバランスは根拠が無いうちは書かない（カメラ設定のまま）
    if develop.temperature is not None and develop.tint is not None:
        attributes["WhiteBalance"] = "Custom"
        attributes["Temperature"] = f"{develop.temperature:d}"
        attributes["Tint"] = f"{develop.tint:d}"
    return attributes


def _set_subject(description: ElementTree.Element, tags: list[str]) -> None:
    """dc:subject を置き換える。再実行してもタグが重複しないよう、毎回作り直す。"""
    existing = description.find(f"{{{DC}}}subject")
    if existing is not None:
        description.remove(existing)
    if not tags:
        return

    subject = ElementTree.SubElement(description, f"{{{DC}}}subject")
    bag = ElementTree.SubElement(subject, f"{{{RDF}}}Bag")
    for tag in tags:
        ElementTree.SubElement(bag, f"{{{RDF}}}li").text = tag


def xmp_path_for(raw_path: Path) -> Path:
    return raw_path.with_suffix(".xmp")
