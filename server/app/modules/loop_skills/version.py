"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v3"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset(
    {
        # v1 (2026-06-24)
        "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",
        # v2 (2026-06-25): writer step 5 + 矩阵特例 + orchestrator 日志中文化
        "abd8416c51f0b591c85cee0c3635645a10a313a2cedbeb52b89953a2c41e7fea",
        # v3 (2026-06-25, PR #152): writer 矩阵段 + README step 5 改为 list_stock_categories
        # 自助路径。两个 sha 是同一份内容的两种行尾：CRLF（Windows 本地装的副本）与 LF（CI /
        # Linux）。build_bundle 读字节后直接 sha256，行尾不归一化——两边都要认。
        "58448672effda8290f97dc5afdfb6c4146ea9c8b7cc7c432b2d4a76274b65856",  # CRLF
        "515c9202f1e0880a1657e4da768954e83bb8a2a5cdfc220355a369862d2cefc6",  # LF (CI canonical)
        # v3 (2026-06-25, this PR): 在 #152 基础上加 writer step 5 改 4 类信号检查
        # (format_error/cover_error/warning/images_inserted=0) + 返回格式 illustration_warnings
        # 字段固定输出，配合服务端 ai_format silent zero 修复（加 skip_reason → warning）。
        # 与 #152 同属 v3 字符串，但 bundle sha 不同（SKILL.md 内容增量）——保留 #152 的
        # 两个 sha 是为已装那份的用户兜底；CI 只校验下面这个 LF sha (Linux 容器读字节)。
        "c8050b24111efc69b56259194bed5d4b236b537ce7effc777a5e2786b3e44acc",  # LF (CI canonical)
    }
)
