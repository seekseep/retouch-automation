"""採否の監査。Lightroom 9.1 が書く xmpDM:good と dc:subject を合成 XMP で確かめる。"""

from pathlib import Path

import pytest

from retouch_automation.audit import Pick, audit, read_sidecar, render, tag_for_review
from retouch_automation.tools.xmp_writer import set_keyword

PREFIX = "artwork_"
REVIEW = "確認が必要"
FIXTURE = Path(__file__).parent / "fixtures" / "lightroom_sidecar.xmp"


def write_photo(directory: Path, photo_id: str, group_id: str | None, good: str | None) -> Path:
    """空の ARW と、Lightroom 9.1 が書き戻すのと同じ形の最小 XMP を置く。"""
    (directory / f"{photo_id}.ARW").write_bytes(b"")
    attribute = f' xmpDM:good="{good}"' if good is not None else ""
    tags = ["ikebana", group_id] if group_id else ["ikebana"]
    items = "".join(f"<rdf:li>{tag}</rdf:li>" for tag in tags)
    xmp_path = directory / f"{photo_id}.xmp"
    xmp_path.write_text(
        f"""<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-c000">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"
    xmlns:dc="http://purl.org/dc/elements/1.1/"{attribute}>
   <dc:subject>
    <rdf:Bag>{items}</rdf:Bag>
   </dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
""",
        encoding="utf-8",
    )
    return xmp_path


@pytest.mark.parametrize(
    ("good", "expected"),
    [("True", Pick.PICKED), ("False", Pick.REJECTED), (None, Pick.UNFLAGGED)],
)
def test_good_attribute_is_read_as_pick(tmp_path, good, expected):
    xmp_path = write_photo(tmp_path, "DSC00001", "artwork_0001", good)

    sidecar = read_sidecar(xmp_path, PREFIX)

    assert sidecar.photo_id == "DSC00001"
    assert sidecar.group_id == "artwork_0001"
    assert sidecar.pick is expected


def test_unexpected_good_value_is_an_error(tmp_path):
    xmp_path = write_photo(tmp_path, "DSC00001", "artwork_0001", "1")

    with pytest.raises(ValueError, match="xmpDM:good"):
        read_sidecar(xmp_path, PREFIX)


def make_shoot(tmp_path: Path) -> Path:
    photos = [
        # 採用 1 枚。どの区分にも出ない
        ("DSC00001", "artwork_0001", "True"),
        ("DSC00002", "artwork_0001", "False"),
        # 採用 2 枚。重複
        ("DSC00003", "artwork_0002", "True"),
        ("DSC00004", "artwork_0002", "False"),
        ("DSC00005", "artwork_0002", "True"),
        # 全部却下
        ("DSC00006", "artwork_0003", "False"),
        ("DSC00007", "artwork_0003", "False"),
        # 採用なしで未判定が残っている
        ("DSC00008", "artwork_0004", "False"),
        ("DSC00009", "artwork_0004", None),
        # 作品タグ無し
        ("DSC00010", None, "True"),
    ]
    for photo_id, group_id, good in photos:
        write_photo(tmp_path, photo_id, group_id, good)
    # XMP が無い ARW
    (tmp_path / "DSC00011.ARW").write_bytes(b"")
    return tmp_path


def test_groups_are_sorted_into_categories(tmp_path):
    result = audit(make_shoot(tmp_path), PREFIX)

    assert [g.group_id for g in result.groups] == [
        "artwork_0001",
        "artwork_0002",
        "artwork_0003",
        "artwork_0004",
    ]
    assert [g.group_id for g in result.duplicate_picks] == ["artwork_0002"]
    assert result.duplicate_picks[0].ids(Pick.PICKED) == ["DSC00003", "DSC00005"]
    assert [g.group_id for g in result.all_rejected] == ["artwork_0003"]
    assert [g.group_id for g in result.unpicked_undecided] == ["artwork_0004"]
    assert result.untagged == ["DSC00010"]
    assert result.missing_xmp == ["DSC00011"]
    assert result.count(Pick.PICKED) == 4
    assert result.count(Pick.REJECTED) == 5
    assert result.count(Pick.UNFLAGGED) == 1


def test_render_lists_each_category(tmp_path):
    text = render(audit(make_shoot(tmp_path), PREFIX), "20260101_test")

    assert text.splitlines() == [
        "20260101_test: 4 作品 / 10 枚（採用 4・却下 5・未判定 1）",
        "重複して採用: 1 件",
        "  artwork_0002  採用 2 / 3 枚  DSC00003 DSC00005",
        "全部却下: 1 件",
        "  artwork_0003  2 枚  DSC00006 DSC00007",
        "採用なし・未判定あり: 1 件",
        "  artwork_0004  未判定 1 / 2 枚  DSC00009",
        "作品タグ無し: 1 枚",
        "  DSC00010",
        "XMP 無し: 1 枚",
        "  DSC00011",
    ]


def test_audit_does_not_write_anything(tmp_path):
    shoot = make_shoot(tmp_path)
    before = {p: p.read_bytes() for p in shoot.iterdir()}

    audit(shoot, PREFIX)

    assert {p: p.read_bytes() for p in shoot.iterdir()} == before


def subject(xmp_path: Path) -> list[str]:
    text = xmp_path.read_text(encoding="utf-8")
    return [t for t in ["ikebana", "artwork_0001", "artwork_0002", REVIEW] if f">{t}<" in text]


def test_tag_marks_every_photo_of_groups_needing_review(tmp_path):
    shoot = make_shoot(tmp_path)
    untouched = (shoot / "DSC00001.xmp").read_bytes()

    added, removed = tag_for_review(audit(shoot, PREFIX), REVIEW)

    assert added == [f"DSC0000{i}" for i in range(3, 10)]
    assert removed == []
    assert subject(shoot / "DSC00003.xmp") == ["ikebana", "artwork_0002", REVIEW]
    # 採用が 1 枚の作品とタグ無しの写真には付けない。ファイルにも触れない
    assert (shoot / "DSC00001.xmp").read_bytes() == untouched
    assert REVIEW not in (shoot / "DSC00010.xmp").read_text(encoding="utf-8")
    # Lightroom が付けた採否はそのまま残る
    assert read_sidecar(shoot / "DSC00003.xmp", PREFIX).pick is Pick.PICKED
    assert read_sidecar(shoot / "DSC00004.xmp", PREFIX).pick is Pick.REJECTED
    assert read_sidecar(shoot / "DSC00009.xmp", PREFIX).pick is Pick.UNFLAGGED
    assert 'xmpDM:good="True"' in (shoot / "DSC00003.xmp").read_text(encoding="utf-8")


def test_rerun_after_review_removes_tag(tmp_path):
    shoot = make_shoot(tmp_path)
    tag_for_review(audit(shoot, PREFIX), REVIEW)

    # 全部却下だった artwork_0003 で 1 枚を採用に直す
    xmp_path = shoot / "DSC00006.xmp"
    xmp_path.write_text(
        xmp_path.read_text(encoding="utf-8").replace(
            'xmpDM:good="False"', 'xmpDM:good="True"'
        ),
        encoding="utf-8",
    )
    added, removed = tag_for_review(audit(shoot, PREFIX), REVIEW)

    assert added == []
    assert removed == ["DSC00006", "DSC00007"]
    assert tag_for_review(audit(shoot, PREFIX), REVIEW) == ([], [])


def test_set_keyword_keeps_lightroom_metadata(tmp_path):
    xmp_path = tmp_path / "DSC02460.xmp"
    xmp_path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    assert set_keyword(xmp_path, REVIEW, present=True)
    assert not set_keyword(xmp_path, REVIEW, present=True)

    text = xmp_path.read_text(encoding="utf-8")
    assert f"<rdf:li>{REVIEW}</rdf:li>" in text
    assert 'xmp:Rating="0"' in text
    assert 'crs:RawFileName="DSC02460.ARW"' in text
    assert "xmpMM:History" in text

    assert set_keyword(xmp_path, REVIEW, present=False)
    assert REVIEW not in xmp_path.read_text(encoding="utf-8")
