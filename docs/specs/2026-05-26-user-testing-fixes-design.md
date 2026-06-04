# 用户测试反馈修复设计文档

**日期**：2026-05-26  
**状态**：待实施  
**涉及模块**：AI 格式化、图片插入、前端跨工作台同步、账号 ZIP 导入

---

## 问题总览

| # | 问题 | 性质 | 优先级 |
|---|------|------|--------|
| 1 | AI 排版识别慢 | 性能 bug（重复发送节点列表） | 高 |
| 2 | 配图位置不智能、图片实际不插入 | 逻辑 bug + 需求对齐 | 高 |
| 3 | 跨工作台数据不同步，需手动 F5 | 架构缺陷 | 高 |
| 4 | ZIP 导入账号显示 valid 但 session 已失效 | 状态误导 | 中 |

---

## 问题 1：AI 排版速度慢

### 根因

`run_ai_format` 中节点列表被发送了**两次**：

1. `system_prompt`（由 Jinja 模板渲染）已包含完整节点列表
2. `user` message 里的 `listing` 变量又重复了一遍相同内容

对长文章（30+ 节点），重复内容导致 token 数量翻倍，请求处理时间相应增加。

### 方案

将 `user` message 从完整节点列表改为简短指令：

```python
# 修改前
messages=[
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": listing},   # ← 与 system prompt 重复
]

# 修改后
messages=[
    {"role": "system", "content": system_prompt},
    {"role": "user", "content": "请按上述要求完成分析，仅返回 JSON。"},
]
```

`listing` 变量的构建代码同步删除（不再需要）。

### 预期收益

减少约 40–60% token 用量，对应减少约 30–50% 响应延迟（模型处理时间与 token 数正相关）。不改变任何功能逻辑。

---

## 问题 2：配图不智能 + 图片实际不插入

### 根因（两层）

**层 1：后端逻辑 bug**  
当前 `_maybe_insert_images` 在 `category_id` 存在时，仍然走 `select_images_by_hints` 做 hint 关键词匹配。`select_images_by_hints` 对 `hint=None` 直接返回 `None`，导致即使 `category_id` 正确，没有 hint 或 hint 不匹配也**静默跳过不插图**。

**层 2：需求对齐偏差**  
原有设计要求"语义选图（hint 匹配）"，实际需求是**按话题对应栏目随机取图**：
- 段落提到"原神" → 从原神 bucket 随机取一张，不挑选
- 段落所属话题没有对应 bucket → 跳过，不插图不插 URL
- 不做图片优选，不依赖 tags/description 标注

### 方案

#### 后端：`_maybe_insert_images` 改为随机取图

当 `category_id` 存在时，调用已有的 `pick_image_id`（随机）替代 `select_images_by_hints`（hint 匹配）：

```python
# 新逻辑（简化）
for pos, hint, requested_category_id in zip(positions, hints, requested_category_ids):
    if requested_category_id is not None and requested_category_id in valid_category_ids:
        # 有明确 category → 随机取图
        image_id = pick_image_id(ImageQuery(category_ids=[requested_category_id]), db)
    else:
        # 无 category → 跳过
        image_id = None
    picked.append((pos, image_id))
```

去掉对 `select_images_by_hints` 的调用（整个 `_maybe_insert_images` 路径简化）。

#### Prompt 模板：去掉 hint，只要 category_id

`ai_format_with_images.j2` 和内置 `_build_system_prompt_with_images` 均修改：

- **移除**：`hint` 字段说明及示例
- **修改**：配图判断说明改为"判断该段落所属话题是否对应某个可用栏目，如果对应则填写 `category_id`，否则跳过"
- **返回格式**：`{"heading_indices":[2,7],"image_positions":[{"index":4,"category_id":12}]}`

`_maybe_insert_images` 中解析 `hint` 的代码一并删除（不再需要）。

### 行为变更

| 场景 | 修改前 | 修改后 |
|------|--------|--------|
| 段落有匹配 category，hint 无匹配 | 不插图（静默丢弃） | 随机插图 ✓ |
| 段落有匹配 category，无 hint 字段 | 不插图（静默丢弃） | 随机插图 ✓ |
| 段落无匹配 category | 不插图 | 不插图（同前） |

---

## 问题 3：跨工作台数据不同步

### 根因

`App.tsx` 中所有工作台通过 `display: none` 隐藏而**非卸载**，初始 mount 后不再重新加载数据。导致：

- ContentWorkspace 保存/创建文章后，TasksWorkspace 的文章下拉列表不更新
- 任务执行完成后，ContentWorkspace 的 `published_count` 不更新
- 任务 SSE `done` 事件触发后，最终日志可能尚未写入，没有兜底拉取

### 方案

#### 3-A：工作台激活时刷新数据

在 `App.tsx` 中给每个工作台传 `isActive` prop；各工作台在 `isActive` 变为 `true` 时重新加载核心数据。

```tsx
// App.tsx 片段
<TasksWorkspace isActive={activeNav === "tasks"} />
<ContentWorkspace isActive={activeNav === "content"} dirtyCheckRef={contentDirtyRef} />
<AccountsWorkspace isActive={activeNav === "media"} />
```

```tsx
// TasksWorkspace 片段
useEffect(() => {
  if (!isActive) return;
  void loadInitial();  // 重新拉取 tasks、articles、accounts、groups
}, [isActive]);
```

ContentWorkspace 在 `isActive` 变为 `true` 时重新拉取文章列表（不影响当前打开的文章正文）。

**效果**：
- 用户从 Content 切到 Tasks → Tasks 立即看到最新文章列表
- 用户从 Tasks 切回 Content → Content 更新 published_count

**开销**：只在用户实际切换 tab 时触发，不切换不请求。

#### 3-B：SSE 结束后兜底拉取

在 `TasksWorkspace` 的 SSE `done` 事件处理中，补一次延迟拉取：

```tsx
es.addEventListener("done", () => {
  es.close();
  // 500ms 后兜底拉取最终状态
  setTimeout(() => void refreshDetail(taskId).catch(() => {}), 500);
  setAutoRefreshTaskIds(...);
});
```

**效果**：任务完成后日志和状态立即最终同步，不再需要手动刷新。

---

## 问题 4：ZIP 导入账号 session 状态误导

### 根因

`import_accounts_auth_package` 直接把 manifest 中的 `status` 字段写入账号记录（可能是 "valid"），但未验证 storage_state.json 中 cookies 是否仍然有效，导致 cookies 已过期的账号也显示 "valid"。

### 方案：导入时校验 cookie 有效性

在写入文件后、创建账号记录前，解析 storage_state.json 并判断 cookies 状态：

```python
def _assess_imported_status(state_path: Path) -> str:
    """解析 storage_state.json，评估 cookies 有效性。
    
    返回：
      "valid"   — cookies 非空，且至少有一个 session cookie 或未来过期的 cookie
      "expired" — cookies 为空，或全部已过期
      "unknown" — 文件无法解析
    """
    import time
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return "unknown"

    cookies = data.get("cookies") or []
    if not cookies:
        return "expired"

    now = time.time()
    for cookie in cookies:
        expires = cookie.get("expires", -1)
        if expires == -1 or expires > now:  # session cookie 或未过期
            return "valid"

    return "expired"
```

在 `import_accounts_auth_package` 中，把 `imported_status = entry.get("status", "unknown")` 替换为调用此函数的结果。

#### 前端：导入结果提示增强

在 AccountsWorkspace 的导入结果弹窗中补充提示：

> "导入成功 {n} 个账号。账号有效性取决于平台 session 是否仍在线，请点击「校验」确认后再发布。"

### 局限说明（设计决策）

此方案只能排除「明显过期」（所有 cookie 时间戳已过期）和「空文件」两种情况，无法检测平台服务端已主动吊销的 session（如 IP 变更触发的安全机制）。这是 cookie 迁移的固有限制，在设计上接受。

---

## 变更文件清单

| 文件 | 变更类型 | 原因 |
|------|----------|------|
| `server/app/modules/articles/ai_format.py` | 修改 | 去掉重复 listing；简化 `_maybe_insert_images` 逻辑 |
| `server/app/modules/articles/prompts/ai_format_with_images.j2` | 修改 | 去掉 hint，只保留 category_id |
| `server/app/modules/image_library/selector.py` | 可选删减 | `select_images_by_hints` 在主流程不再被调用 |
| `server/app/modules/accounts/auth.py` | 修改 | 新增 `_assess_imported_status`，替换 status 赋值 |
| `web/src/App.tsx` | 修改 | 给各工作台传 `isActive` prop |
| `web/src/features/tasks/TasksWorkspace.tsx` | 修改 | `isActive` 触发刷新；SSE done 后兜底 fetch |
| `web/src/features/content/ContentWorkspace.tsx` | 修改 | `isActive` 触发文章列表刷新 |
| `web/src/features/accounts/AccountsWorkspace.tsx` | 修改 | `isActive` 触发账号列表刷新；导入结果提示增强 |

---

## 不在本次范围内

- 平台 IP 绑定导致 session 失效（固有限制，接受）
- 图片语义优选 / AI 生图（后续迭代）
- WebSocket 全局推送（当前方案已满足需求，此为过度设计）
- 图库标注工作流
