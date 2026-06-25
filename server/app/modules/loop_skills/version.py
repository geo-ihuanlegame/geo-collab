"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-25-v2"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset(
    {
        # v1 (2026-06-24): 保留——本地未升级用户的 KNOWN 校验仍要认
        "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",
        # v2 (2026-06-25): writer step 5 / 矩阵特例 + orchestrator 日志中文化 + README onboarding 6 步
        "abd8416c51f0b591c85cee0c3635645a10a313a2cedbeb52b89953a2c41e7fea",
    }
)
