from pathlib import Path
from server.app.modules.articles.parser import BodySegment
from server.app.modules.tasks.drivers.toutiao import BodyParagraph, _group_paragraphs


def test_heading_becomes_heading_paragraph():
    segs = [
        BodySegment(kind="text", text="Title", heading_level=1),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="Body"),
        BodySegment(kind="text", text="\n"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 2
    assert paras[0].kind == "heading"
    assert paras[0].heading_level == 1
    assert paras[0].runs == (("Title", False),)
    assert paras[1].kind == "text"
    assert paras[1].runs == (("Body", False),)


def test_bold_runs_preserved_in_paragraph():
    segs = [
        BodySegment(kind="text", text="plain ", bold=False),
        BodySegment(kind="text", text="bold", bold=True),
        BodySegment(kind="text", text="\n"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 1
    assert paras[0].runs == (("plain ", False), ("bold", True))


def test_image_flushes_text_and_becomes_own_paragraph():
    segs = [
        BodySegment(kind="text", text="Before"),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="image", image_asset_id="abc", image_path=Path("/tmp/img.jpg")),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="After"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 3
    assert paras[0].kind == "text"
    assert paras[1].kind == "image"
    assert paras[1].image_asset_id == "abc"
    assert paras[2].kind == "text"


def test_blank_only_paragraph_is_skipped():
    segs = [
        BodySegment(kind="text", text="   "),
        BodySegment(kind="text", text="\n"),
        BodySegment(kind="text", text="Real"),
    ]
    paras = _group_paragraphs(segs)
    assert len(paras) == 1
    assert paras[0].runs == (("Real", False),)
