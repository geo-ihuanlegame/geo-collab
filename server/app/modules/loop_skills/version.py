"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-24-v1"

KNOWN_BUNDLE_SHAS: frozenset[str] = frozenset(
    {
        "49f824a36606c285b84c71ade5aec406ea3c545599b7a3ccf59f863b521667dd",
    }
)
