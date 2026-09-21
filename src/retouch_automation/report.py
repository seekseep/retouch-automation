"""処理レポート。失敗した写真と、人間の確認が要る写真を一覧にする。"""

from __future__ import annotations

import json
from pathlib import Path

from .models import PhotoResult, Status


def build(results: list[PhotoResult]) -> dict[str, object]:
    return {
        "total": len(results),
        "xmp_written": sum(1 for r in results if r.xmp_written),
        "failed": [r.photo_id for r in results if r.errors],
        "needs_review": [r.photo_id for r in results if r.needs_review],
        "photos": [_summary(r) for r in results],
    }


def _summary(result: PhotoResult) -> dict[str, object]:
    parts = {
        "classification": result.classification,
        "detection": result.detection,
        "straightening": result.straightening,
        "crop": result.crop,
        "develop": result.develop,
    }
    return {
        "photo_id": result.photo_id,
        "group_id": result.classification.group_id if result.classification else None,
        "xmp_written": result.xmp_written,
        "status": {k: (v.status if v else Status.FAILED) for k, v in parts.items()},
        "errors": result.errors,
    }


def write(results: list[PhotoResult], path: Path) -> dict[str, object]:
    data = build(results)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return data
