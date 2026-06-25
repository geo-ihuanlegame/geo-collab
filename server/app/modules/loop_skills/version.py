"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v4"

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
        # v3 (2026-06-25, illustration_warnings PR #154): writer step 5 改 4 类信号检查 +
        # illustration_warnings 字段。
        "ee9659ae08d68a6bdabecfce9f60324fab2093b7ad4535f056f5cdc4ea6a77e8",  # LF
        "c8050b24111efc69b56259194bed5d4b236b537ce7effc777a5e2786b3e44acc",  # CRLF
        # v4 (2026-06-25, this PR): orchestrator skill 主对话叙述深度中文化
        # （6 行日志 / 伪码 echo / notify_feishu / subagent description + 新增叙述规范段）
        "506d2a045eee9106962e97bad0cdf287d6a36f0de2cf2b62265c904be3f22b5c",  # CRLF (Windows host)
        "3c668186ff4edcd42b95f6b59cdc79b2c9045c09bd00692f4f11990c2de6f53b",  # LF (CI canonical)
    }
)
