"""採否の監査。人が Lightroom で付けた採用・却下を読み、作品タグごとに食い違いを出す。

このシステムは採否を判断しない。ここも人が付けた採否を読むだけ。
既定では XMP にも作業ディレクトリにも書き込まない。--tag を付けたときだけ、
見直すべき作品の写真に「確認が必要」のキーワードを付け、見直しが済んだ写真からは外す。
Lightroom でそのキーワードで絞り込めるようにするため。

Lightroom 9.1 は採否フラグを xmpDM:good に書く（docs/xmp.md）。
  "True"   … 採用
  "False"  … 却下
  属性なし … 未判定

作品タグの番号は撮影ディレクトリごとに 0001 から振り直すので、1 ディレクトリの中だけで突き合わせる。
グループは XMP の dc:subject から取る。Lightroom でキーワードを付け直した場合もそれを正とするため。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from xml.etree import ElementTree

from .tools.raw_loader import find_raw_files
from .tools.xmp_writer import NAMESPACES, set_keyword, xmp_path_for

RDF = NAMESPACES["rdf"]
DC = NAMESPACES["dc"]
GOOD = f"{{{NAMESPACES['xmpDM']}}}good"


class Pick(StrEnum):
    PICKED = "picked"
    REJECTED = "rejected"
    UNFLAGGED = "unflagged"


_PICKS = {"True": Pick.PICKED, "False": Pick.REJECTED}


@dataclass(frozen=True)
class Sidecar:
    photo_id: str
    xmp_path: Path
    #: 作品タグ（artwork_0001 など）。付いていなければ None
    group_id: str | None
    pick: Pick


@dataclass(frozen=True)
class Group:
    group_id: str
    photos: list[Sidecar]

    def ids(self, pick: Pick) -> list[str]:
        return [p.photo_id for p in self.photos if p.pick == pick]


@dataclass(frozen=True)
class AuditResult:
    sidecars: list[Sidecar]
    #: XMP が無い ARW
    missing_xmp: list[str]

    @property
    def groups(self) -> list[Group]:
        photos: dict[str, list[Sidecar]] = defaultdict(list)
        for sidecar in self.sidecars:
            if sidecar.group_id is not None:
                photos[sidecar.group_id].append(sidecar)
        return [Group(group_id=g, photos=photos[g]) for g in sorted(photos)]

    @property
    def untagged(self) -> list[str]:
        return [s.photo_id for s in self.sidecars if s.group_id is None]

    @property
    def duplicate_picks(self) -> list[Group]:
        """同じ作品で採用が 2 枚以上。"""
        return [g for g in self.groups if len(g.ids(Pick.PICKED)) >= 2]

    @property
    def all_rejected(self) -> list[Group]:
        """作品の写真がすべて却下。"""
        return [g for g in self.groups if len(g.ids(Pick.REJECTED)) == len(g.photos)]

    @property
    def unpicked_undecided(self) -> list[Group]:
        """採用が 0 枚で、未判定の写真が残っている。全部却下とは分けて出す。"""
        return [g for g in self.groups if not g.ids(Pick.PICKED) and g.ids(Pick.UNFLAGGED)]

    @property
    def review_targets(self) -> list[Sidecar]:
        """見直すべき作品の写真。採用と却下を見比べて選び直せるよう、作品の全枚数を含める。"""
        groups = [*self.duplicate_picks, *self.all_rejected, *self.unpicked_undecided]
        return sorted((p for g in groups for p in g.photos), key=lambda s: s.photo_id)

    def count(self, pick: Pick) -> int:
        return sum(1 for s in self.sidecars if s.pick == pick)


def read_sidecar(xmp_path: Path, group_tag_prefix: str) -> Sidecar:
    root = ElementTree.parse(xmp_path).getroot()
    description = root.find(f".//{{{RDF}}}Description")
    if description is None:
        raise ValueError(f"rdf:Description が見つからない XMP: {xmp_path}")

    tags = [
        li.text or ""
        for li in description.iterfind(f"{{{DC}}}subject/{{{RDF}}}Bag/{{{RDF}}}li")
    ]
    groups = [t for t in tags if t.startswith(group_tag_prefix)]

    good = description.get(GOOD)
    if good is not None and good not in _PICKS:
        raise ValueError(f"xmpDM:good の値が想定外: {good!r} ({xmp_path})")

    return Sidecar(
        photo_id=xmp_path.stem,
        xmp_path=xmp_path,
        group_id=groups[0] if groups else None,
        pick=_PICKS[good] if good is not None else Pick.UNFLAGGED,
    )


def audit(raw_dir: Path, group_tag_prefix: str) -> AuditResult:
    sidecars: list[Sidecar] = []
    missing_xmp: list[str] = []
    for raw_path in find_raw_files(raw_dir):
        xmp_path = xmp_path_for(raw_path)
        if not xmp_path.exists():
            missing_xmp.append(raw_path.stem)
            continue
        sidecars.append(read_sidecar(xmp_path, group_tag_prefix))
    return AuditResult(sidecars=sidecars, missing_xmp=missing_xmp)


def tag_for_review(result: AuditResult, keyword: str) -> tuple[list[str], list[str]]:
    """見直すべき写真に keyword を付け、それ以外からは外す。

    見直しが済んで再実行したときに、古いキーワードが残らないようにするため外す側も行う。
    戻り値は（付けた写真, 外した写真）。変わらない写真の XMP には触れない。
    """
    targets = {s.photo_id for s in result.review_targets}
    added: list[str] = []
    removed: list[str] = []
    for sidecar in result.sidecars:
        present = sidecar.photo_id in targets
        if set_keyword(sidecar.xmp_path, keyword, present):
            (added if present else removed).append(sidecar.photo_id)
    return added, removed


def render(result: AuditResult, title: str) -> str:
    lines = [
        f"{title}: {len(result.groups)} 作品 / {len(result.sidecars)} 枚"
        f"（採用 {result.count(Pick.PICKED)}・却下 {result.count(Pick.REJECTED)}"
        f"・未判定 {result.count(Pick.UNFLAGGED)}）"
    ]

    lines.append(f"重複して採用: {len(result.duplicate_picks)} 件")
    for group in result.duplicate_picks:
        picked = group.ids(Pick.PICKED)
        lines.append(_row(group.group_id, f"採用 {len(picked)} / {len(group.photos)} 枚", picked))

    lines.append(f"全部却下: {len(result.all_rejected)} 件")
    for group in result.all_rejected:
        ids = [p.photo_id for p in group.photos]
        lines.append(_row(group.group_id, f"{len(ids)} 枚", ids))

    lines.append(f"採用なし・未判定あり: {len(result.unpicked_undecided)} 件")
    for group in result.unpicked_undecided:
        undecided = group.ids(Pick.UNFLAGGED)
        lines.append(
            _row(group.group_id, f"未判定 {len(undecided)} / {len(group.photos)} 枚", undecided)
        )

    # 取りこぼしは有るときだけ出す
    if result.untagged:
        lines.append(f"作品タグ無し: {len(result.untagged)} 枚")
        lines.append("  " + " ".join(result.untagged))
    if result.missing_xmp:
        lines.append(f"XMP 無し: {len(result.missing_xmp)} 枚")
        lines.append("  " + " ".join(result.missing_xmp))

    return "\n".join(lines)


def _row(group_id: str, summary: str, photo_ids: list[str]) -> str:
    return f"  {group_id}  {summary}  {' '.join(photo_ids)}"
