"""作業ディレクトリ。RAW ディレクトリには *.xmp 以外を書かない。"""

from __future__ import annotations

from pathlib import Path

from .models import PhotoResult


class Workspace:
    def __init__(self, raw_dir: Path, work_dir_name: str) -> None:
        self.raw_dir = raw_dir
        self.root = raw_dir / work_dir_name
        self.root.mkdir(parents=True, exist_ok=True)

    def photo_dir(self, photo_id: str) -> Path:
        d = self.root / "photos" / photo_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def overlay_dir(self, photo_id: str) -> Path:
        d = self.photo_dir(photo_id) / "overlay"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def result_path(self, photo_id: str) -> Path:
        return self.photo_dir(photo_id) / "result.json"

    def save(self, result: PhotoResult) -> None:
        self.result_path(result.photo_id).write_text(
            result.model_dump_json(indent=2), encoding="utf-8"
        )

    def load(self, photo_id: str) -> PhotoResult | None:
        p = self.result_path(photo_id)
        if not p.exists():
            return None
        return PhotoResult.model_validate_json(p.read_text(encoding="utf-8"))
