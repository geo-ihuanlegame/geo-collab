"""手工维护的 bundle 版本号 + 已审核 sha 集合。

CI 测试 (test_loop_skill_bundle.py::test_bundle_sha_is_known) 会校验：
如果 build_bundle().bundle_sha256 没记录在 KNOWN_BUNDLE_SHAS 集合里，
fail + 提示开发者：把新 sha 加进 KNOWN_BUNDLE_SHAS 并 bump
LOOP_SKILL_BUNDLE_VERSION，强制「改模板必同步 bump 版本」纪律。
"""

LOOP_SKILL_BUNDLE_VERSION = "2026-06-29-v9"

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
        # v5 (2026-06-26, web_fallback): writer SKILL 配图段 + 调用约定加
        # web_fallback=True（图库里没有对应栏目的游戏走百度联网补图）。
        "614fb5176177ec1dc703c625a2e2318f2634699d780e22b6cc78a1e5ae6e818d",  # CRLF (Windows host)
        "957ba2c02327a257cd34dc5eede070d30abbea3b7e6983aa9981a2c46cbcc12d",  # LF (CI canonical)
        # v6 (2026-06-26, 部分配图盲区): writer SKILL step5 加第 5 类信号 missed>0
        # （partial：应配 N 张只来 M 张，即便有图也记 illustration_warnings）。
        "091addcab9c96b0f78e72cc59d0b4b33f71cb272c8c13d35c75569b78f2debe6",  # CRLF (Windows host)
        "54b826b5273fe83546adffbc537a018e6f6b26c117e8e07f04b19742ca035b24",  # LF (CI canonical)
        # v6 merge 产物 (2026-06-26): 把 feat 分支 merge 进 main 时，GitLab 与 GitHub 两边的
        # 3-way merge 都会产出这第三种字节序列（语义=干净 v6，writer SKILL 不重复，仅行级合并
        # 产物 sha 不同）。GitLab pipeline #342 在 merge commit b7b5912 上实测算出此值；GitHub
        # PR #162 merge 也算出同值。两边 CI 都需要它放行（早前注释误判「GitLab 不需要」，已订正）。
        "9dfb1db0508d9f930d99e74a25ee6b257c78ed12c4caf2301b1faf4ed708be4b",  # LF (3-way merge product)
        # v7 (2026-06-29, 显式游戏清单产清单): writer SKILL step5 + 调用约定 加 game_positions —
        # 每款一标题的推荐 / 盘点文逐款产 game_positions 走确定性落图（修弱模型漏点缺图），
        # 散文 / 综述回退现有模型识别路径。配合已合并的端点 + MCP 工具 game_positions 形参。
        "06df0c1fb709cc3e5e8f4edbf110a7005e1fa1bce16597c9ef42dde9ee5a5c36",  # Windows 本地工作区 (autocrlf → uniform CRLF)
        # CI canonical = git blob 字节（模板全是 LF；Linux runner autocrlf=off，checkout 即 blob）。
        # 实测 GitHub Actions run 28347499953 backend-tests(2)。算法务必读 blob 而非工作区归一化。
        "23707c7eb05343471cd8bc313d2685822395a93fa9f75ebdff6d4d692d16d7c6",  # LF (CI canonical, blob)
        # v8 (2026-06-29, prompt_template_name 展示参数): writer SKILL step 4 + save_article
        # MCP 工具签名加 prompt_template_name + question_text_preview 两个可选展示参数。
        # Claude Code UI 在工具调用渲染时会同时显示中文名（如
        # `prompt_template_id: 13, prompt_template_name: "游戏情绪清单"`），运营在主对话里
        # 不用回头查数字。后端 SaveArticleFromMcpPayload 用 Pydantic 默认 extra='ignore'
        # 丢弃这两字段，无需后端 schema 改动。
        "128eb1d6a0198fe62313474ef47f0c63788068c348814177853cb790cec404b7",  # CRLF (Windows host)
        "e9da575b99e750f919713f1f8f678f0fbb17d71713ce91849bcf911321343111",  # LF (CI canonical)
        # v9 (2026-06-29, tpl_id 命令行覆盖): orchestrator SKILL Goal Parsing 表加 tpl_id
        # 字段 + 主循环 `tpl_id = target.tpl_id or templates[attempts % len(templates)].id`。
        # 用户在 /goal 里写 `生文提示词Id=13` / `生文提示词 #13` / `用生文提示词 13`（也
        # 兼容旧写法 `tpl=13` / `模板 #13`）即固定走该提示词；缺省仍是全提示词 round-robin
        # 轮转。启动检查日志加「生文提示词：<#id 写死|轮转>」字段让运营在主对话里一眼可见；
        # 中文叙述对照表把「模板」统一改成「生文提示词」，与后端 PromptTemplate schema
        # 命名一致。
        "b360c4086ec0aca8db7ffdbd961172bd5c9be15951180236ec71944353045f61",  # CRLF (Windows host)
        "f40f9c28625e8e430fd4ebaa538d83a44d32b804d5c7e6cb5fc5662731218001",  # LF (CI canonical)
    }
)
