"""TDD: 写作模型产显式游戏清单 → 盖 article.metrics["game_positions"]。

显式清单 web 生产者（2026-06-29）：generate_article_from_prompt 让强写作模型在正文后
追加 ```json {"games":[...]}``` 哨兵块，_split_games_block 剥块取名，盘点文盖 metrics、
散文/无块不盖（零回归）。消费侧（方案运行 / illustrate_one）据此走确定性落图。
"""

import pytest

from server.app.modules.ai_generation.article_writer import _split_games_block
from server.tests.utils import build_test_app

# ── 纯函数：_split_games_block（无需 DB）──────────────────────────────────


def test_split_extracts_trailing_games_block():
    md = '# 十大推荐\n\n## 《原神》\n正文…\n\n```json\n{"games": ["原神", "明日方舟"]}\n```\n'
    body, games = _split_games_block(md)
    assert games == ["原神", "明日方舟"]
    assert "```json" not in body  # 哨兵块被剥
    assert "原神" in body  # 正文里的小标题保留


def test_split_prose_empty_games():
    md = '# 综述\n散文正文…\n\n```json\n{"games": []}\n```'
    body, games = _split_games_block(md)
    assert games == []
    assert "```json" not in body  # 空 games 也是哨兵，照剥


def test_split_no_block_unchanged():
    md = "# 标题\n正文没有 json 块"
    body, games = _split_games_block(md)
    assert games == []
    assert body == md  # 零回归：原样返回


def test_split_malformed_or_non_sentinel_left_as_content():
    # 没有 games 键（非哨兵）→ 不剥离、当正文、games 空（绝不误吃正文代码块）
    md = '# 教程\n讲讲 JSON：\n```json\n{"foo": 1}\n```'
    body, games = _split_games_block(md)
    assert games == []
    assert body == md


def test_split_takes_last_block_only():
    md = '# 标题\n```json\n{"foo": 1}\n```\n中间正文\n```json\n{"games": ["鸣潮"]}\n```'
    body, games = _split_games_block(md)
    assert games == ["鸣潮"]
    # 仅最后一个哨兵块被剥；前面的非哨兵块当正文留着
    assert '{"foo": 1}' in body


# ── 集成：generate_article_from_prompt 盖 metrics（需 DB）──────────────────


def _fake_completion(text: str):
    class _Msg:
        content = text

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    return _Resp()


@pytest.mark.mysql
def test_generate_stamps_game_positions(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        content = (
            "# 十大国风游戏\n\n## 《原神》\n好玩。\n\n## 《明日方舟》\n也好玩。\n\n"
            '```json\n{"games": ["原神", "明日方舟"]}\n```\n'
        )
        monkeypatch.setattr("litellm.completion", lambda **kw: _fake_completion(content))

        from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id

        aid = generate_article_from_prompt(
            session_factory=app.session_factory,
            user_id=admin_id,
            template_content="写：{{问题}}",
            question_text="q",
            model=None,
        )
        with app.session_factory() as db:
            art = db.get(Article, aid)
            assert (art.metrics or {}).get("game_positions") == [
                {"game": "原神"},
                {"game": "明日方舟"},
            ]
            assert "```json" not in (art.plain_text or "")  # json 块不泄进正文
    finally:
        app.cleanup()


@pytest.mark.mysql
def test_generate_no_stamp_when_prose(monkeypatch):
    app = build_test_app(monkeypatch)
    try:
        monkeypatch.setattr(
            "litellm.completion",
            lambda **kw: _fake_completion("# 综述\n\n纯散文，没有清单块。"),
        )
        from server.app.modules.ai_generation.article_writer import generate_article_from_prompt
        from server.app.modules.articles.models import Article
        from server.app.modules.system.models import User

        with app.session_factory() as db:
            admin_id = db.query(User).filter(User.username == "testadmin").first().id

        aid = generate_article_from_prompt(
            session_factory=app.session_factory,
            user_id=admin_id,
            template_content="写：{{问题}}",
            question_text="q",
            model=None,
        )
        with app.session_factory() as db:
            art = db.get(Article, aid)
            assert "game_positions" not in (art.metrics or {})
    finally:
        app.cleanup()
