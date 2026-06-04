> ⚠️ **已作废 (SUPERSEDED 2026-06-04)**：本计划针对 `content-library-public` / `pc-admin-conetnt-library-public` 两个**参考项目**编写。这两个项目仅供架构参照、不可改动。实际改造落在 `geo-collab` 主仓库，见 `2026-06-04-geo-collab-pipeline-orchestration.md`。以下内容仅作参考保留，**不要执行**。

---

# 可视化流程编排轻量增强 Implementation Plan（已作废）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有线性 pipLine 之上增量补齐连线依赖/数据传递、草稿暂存、版本回溯、属性面板完善，不引图库、不改线性执行模型、向后兼容。

**Architecture:** 后端在 `pip_line_item` 加 `flow_meta`、`pip_line` 加 `draft_snapshot`/`has_draft`、新建 `pip_line_version` 表；纯逻辑（数据传递求值、快照序列化、版本号计算）以 JUnit 单测驱动；草稿/版本/执行接入复用现有 MyBatis-Plus + `R<T>` + service-impl 模式。前端在现有节点抽屉加"数据传递"分栏与草稿/版本 UI，复用 view-ui-plus。

**Tech Stack:** 后端 Java 17 / Spring Boot / MyBatis-Plus / MySQL / JUnit5 + Mockito；前端 Vue 3 + view-ui-plus + vant + axios 封装（`src/api/index.js`）。

---

## 约定（零上下文工程师必读）

- **两个工程根目录**（均在本仓库内，独立于 geo-collab 主应用）：
  - 后端：`content-library-public/`（Maven，`pom.xml`）
  - 前端：`pc-admin-conetnt-library-public/`（`package.json`，pnpm/npm）
- **后端响应包装**：所有 controller 返回 `R<T>`。无返回值用 `R.ok()`，有数据用 `R.ok(data)`，失败抛 `BusinessException("msg")`（全局异常处理器转 `R.failed`）。
- **后端 ORM**：MyBatis-Plus。Mapper 接口 `extends BaseMapper<Entity>` 即获得 `selectById/selectList/insert/updateById/deleteById`。复杂查询用 `LambdaQueryWrapper`。下划线↔驼峰自动映射已开启（`accountId`↔`account_id`）。
- **当前登录账号**：`WebContextFilter.getAccountInfoRequired()` 返回 `AccountInfo`，`.getId()` 为 accountId。
- **JSON 工具**：`com.jide.content.util.JsonUtil`（项目已有，提供对象↔JSON 字符串）。新增纯逻辑里如需 JSON，统一用 `JsonUtil`，禁止新引入 JSON 库。
- **事务**：写多表/重建行用 `@Transactional(rollbackFor = Exception.class)`。
- **包路径**：实体 `com.jide.content.domain.entity`；request `com.jide.content.domain.request.pipline`；VO `com.jide.content.domain.vo.pipline`；mapper `com.jide.content.mapper`；service `com.jide.content.service` / impl `com.jide.content.service.impl`；controller `com.jide.content.controller`。
- **验证策略（重要，环境受限）**：
  - **纯逻辑类**（求值器、序列化器、版本号计算）：JUnit5 单测，`mvn -pl . test -Dtest=XxxTest`，**不依赖 DB/Spring 上下文**（与现有 `src/test` 风格一致）。
  - **DB/接口装配**：仓库无 MySQL 集成测试套件——用 `mvn -q -DskipTests compile` 验证编译通过 + 文末「手动冒烟」curl 脚本验证接口。**不要新建集成测试框架**（YAGNI）。
  - **前端**：仓库无前端测试框架——用 `pnpm --filter ... build` + `pnpm --filter ... lint` + 「手动冒烟」浏览器步骤验证。**不要引入 jest/vitest**。
  - 所有 `mvn`/`pnpm` 命令在具备相应工具链的环境（dev 容器）执行。
- **DDL 文件位置**：增量 SQL 放 `content-library-public/sql/` 新文件，并把同样语句追加到根 `content-library-public/migration.sql`（与现有约定一致）。
- **快照 JSON 结构**：所有快照/flow_meta JSON 顶层带 `"schemaVersion": 1`，便于未来兼容旧快照。
- **提交粒度**：每个 Task 末尾提交一次，message 用 `feat(pipline): ...` / `test(pipline): ...`。

---

## Phase 0 — 迁移与表结构核对

### Task 0: 核对 `data` 列并编写 DDL 增量迁移

**Files:**
- Create: `content-library-public/sql/2026-06-04-pipline-orchestration.sql`
- Modify: `content-library-public/migration.sql`（追加）

- [ ] **Step 1: 核对线上/DDL 中 `pip_line_item.data` 列是否存在**

Run（在能连到目标 MySQL 的环境，或检查最新 DDL）:
```bash
grep -rn "pip_line_item" content-library-public/sql/ content-library-public/migration.sql | grep -i "data"
```
判定：
- 若 `pip_line_item` 已有 `data` 列 → 本任务**不**补 `data` 列。
- 若没有（`sql/工作流dml.sql:61` 的建表段确实未列出，但 `PipLineItem.java` 实体有 `data` 字段，说明很可能后续迁移补过；以实际目标库为准）→ 在下一步迁移里**一并补** `data` 列。

记录判定结论到提交说明。

- [ ] **Step 2: 编写增量迁移 SQL**

写入 `content-library-public/sql/2026-06-04-pipline-orchestration.sql`：
```sql
-- 可视化流程编排轻量增强：连线元数据 / 草稿 / 版本

-- 1) 节点连线元数据（数据传递 + 依赖 + 条件）
ALTER TABLE pip_line_item
    ADD COLUMN flow_meta longtext NULL COMMENT '连线元数据JSON(dependsOnIndex/inputMapping/condition)';

-- 若核对发现缺 data 列，取消下一行注释一并补（默认假定已存在，保持注释）：
-- ALTER TABLE pip_line_item ADD COLUMN data longtext NULL COMMENT '节点表单配置JSON';

-- 2) 工作流草稿
ALTER TABLE pip_line
    ADD COLUMN draft_snapshot longtext NULL COMMENT '未发布草稿全量快照JSON',
    ADD COLUMN has_draft smallint NOT NULL DEFAULT 0 COMMENT '是否存在未发布草稿:0-否 1-是';

-- 3) 工作流版本
CREATE TABLE pip_line_version
(
    id          bigint auto_increment PRIMARY KEY,
    pip_line_id bigint                             NOT NULL COMMENT '工作流id',
    version_no  int                                NOT NULL COMMENT '工作流内递增版本号',
    snapshot    longtext                           NOT NULL COMMENT '该版本全量items+flow_meta快照JSON',
    remark      varchar(255)                       NULL COMMENT '发布备注',
    created_by  bigint                             NOT NULL COMMENT '发布人account_id',
    create_time datetime default CURRENT_TIMESTAMP NOT NULL COMMENT '创建时间',
    update_time datetime default CURRENT_TIMESTAMP NOT NULL ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  ROW_FORMAT = DYNAMIC COMMENT = '工作流版本';

CREATE INDEX pip_line_version_index ON pip_line_version (pip_line_id, version_no);
```

- [ ] **Step 3: 追加到 migration.sql**

把 Step 2 的全部语句追加到 `content-library-public/migration.sql` 末尾，加日期注释分隔。

- [ ] **Step 4: 提交**

```bash
git add content-library-public/sql/2026-06-04-pipline-orchestration.sql content-library-public/migration.sql
git commit -m "feat(pipline): DDL for flow_meta / draft / version"
```

---

## Phase 1 — 后端纯逻辑（JUnit TDD）

### Task 1: FlowMeta / Snapshot 数据模型 POJO

**Files:**
- Create: `content-library-public/src/main/java/com/jide/content/domain/pipline/FlowMeta.java`
- Create: `content-library-public/src/main/java/com/jide/content/domain/pipline/FlowCondition.java`
- Create: `content-library-public/src/main/java/com/jide/content/domain/pipline/FlowInputMapping.java`
- Create: `content-library-public/src/main/java/com/jide/content/domain/pipline/PipLineSnapshot.java`
- Create: `content-library-public/src/main/java/com/jide/content/domain/pipline/SnapshotItem.java`

- [ ] **Step 1: 创建 FlowInputMapping**

```java
package com.jide.content.domain.pipline;

import lombok.Data;

@Data
public class FlowInputMapping {
    /** 上游输出字段名 */
    private String from;
    /** 本节点输入字段名 */
    private String to;
}
```

- [ ] **Step 2: 创建 FlowCondition**

```java
package com.jide.content.domain.pipline;

import lombok.Data;

@Data
public class FlowCondition {
    /** 参与判断的字段名（取自上游累积上下文） */
    private String field;
    /** 操作符: eq / neq / contains */
    private String op;
    /** 比较值 */
    private String value;
}
```

- [ ] **Step 3: 创建 FlowMeta**

```java
package com.jide.content.domain.pipline;

import lombok.Data;
import java.util.List;

@Data
public class FlowMeta {
    private Integer schemaVersion = 1;
    /** 上游节点 item_index；null 表示默认上一个节点 */
    private Integer dependsOnIndex;
    private List<FlowInputMapping> inputMapping;
    /** 可选跳过条件；null 表示不跳过 */
    private FlowCondition condition;
}
```

- [ ] **Step 4: 创建 SnapshotItem 与 PipLineSnapshot**

```java
package com.jide.content.domain.pipline;

import lombok.Data;

@Data
public class SnapshotItem {
    private String icon;
    private String name;
    private String code;
    private Integer itemIndex;
    /** 节点表单配置JSON字符串 */
    private String data;
    /** 连线元数据JSON字符串 */
    private String flowMeta;
}
```

```java
package com.jide.content.domain.pipline;

import lombok.Data;
import java.util.List;

@Data
public class PipLineSnapshot {
    private Integer schemaVersion = 1;
    private List<SnapshotItem> items;
}
```

- [ ] **Step 5: 编译验证**

Run: `cd content-library-public && mvn -q -DskipTests compile`
Expected: BUILD SUCCESS

- [ ] **Step 6: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/domain/pipline/
git commit -m "feat(pipline): flow-meta and snapshot model pojos"
```

---

### Task 2: 数据传递求值器 FlowMetaEvaluator（TDD）

纯逻辑：给定 FlowMeta + 上游上下文 Map，产出注入字段；并判断是否跳过。无 Spring/DB 依赖。

**Files:**
- Create: `content-library-public/src/main/java/com/jide/content/service/pipline/FlowMetaEvaluator.java`
- Test: `content-library-public/src/test/java/com/jide/content/service/pipline/FlowMetaEvaluatorTest.java`

- [ ] **Step 1: 写失败测试**

```java
package com.jide.content.service.pipline;

import com.jide.content.domain.pipline.FlowCondition;
import com.jide.content.domain.pipline.FlowInputMapping;
import com.jide.content.domain.pipline.FlowMeta;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.jupiter.api.Assertions.*;

class FlowMetaEvaluatorTest {

    private final FlowMetaEvaluator evaluator = new FlowMetaEvaluator();

    @Test
    void applyInputMapping_copiesUpstreamFieldsToTargetNames() {
        FlowMeta meta = new FlowMeta();
        FlowInputMapping m = new FlowInputMapping();
        m.setFrom("title");
        m.setTo("sourceTitle");
        meta.setInputMapping(List.of(m));

        Map<String, Object> upstream = new HashMap<>();
        upstream.put("title", "Hello");

        Map<String, Object> result = evaluator.applyInputMapping(meta, upstream);
        assertEquals("Hello", result.get("sourceTitle"));
    }

    @Test
    void applyInputMapping_nullMetaReturnsEmpty() {
        assertTrue(evaluator.applyInputMapping(null, Map.of("a", "b")).isEmpty());
    }

    @Test
    void shouldSkip_eqConditionMet_returnsFalse() {
        FlowMeta meta = new FlowMeta();
        FlowCondition c = new FlowCondition();
        c.setField("status"); c.setOp("eq"); c.setValue("ok");
        meta.setCondition(c);
        assertFalse(evaluator.shouldSkip(meta, Map.of("status", "ok")));
    }

    @Test
    void shouldSkip_eqConditionNotMet_returnsTrue() {
        FlowMeta meta = new FlowMeta();
        FlowCondition c = new FlowCondition();
        c.setField("status"); c.setOp("eq"); c.setValue("ok");
        meta.setCondition(c);
        assertTrue(evaluator.shouldSkip(meta, Map.of("status", "bad")));
    }

    @Test
    void shouldSkip_noCondition_returnsFalse() {
        assertFalse(evaluator.shouldSkip(new FlowMeta(), Map.of()));
        assertFalse(evaluator.shouldSkip(null, Map.of()));
    }

    @Test
    void shouldSkip_containsAndNeq() {
        FlowMeta meta = new FlowMeta();
        FlowCondition c = new FlowCondition();
        c.setField("tags"); c.setOp("contains"); c.setValue("news");
        meta.setCondition(c);
        assertFalse(evaluator.shouldSkip(meta, Map.of("tags", "hot,news")));
        assertTrue(evaluator.shouldSkip(meta, Map.of("tags", "hot")));

        c.setOp("neq"); c.setValue("x");
        assertFalse(evaluator.shouldSkip(meta, Map.of("tags", "y")));
        assertTrue(evaluator.shouldSkip(meta, Map.of("tags", "x")));
    }
}
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd content-library-public && mvn -q test -Dtest=FlowMetaEvaluatorTest`
Expected: 编译失败 / FAIL（`FlowMetaEvaluator` 不存在）

- [ ] **Step 3: 实现 FlowMetaEvaluator**

```java
package com.jide.content.service.pipline;

import com.jide.content.domain.pipline.FlowCondition;
import com.jide.content.domain.pipline.FlowInputMapping;
import com.jide.content.domain.pipline.FlowMeta;
import org.springframework.stereotype.Component;

import java.util.HashMap;
import java.util.Map;

@Component
public class FlowMetaEvaluator {

    /** 按 inputMapping 把上游字段拷贝到目标字段名。meta/mapping 为空返回空 map。 */
    public Map<String, Object> applyInputMapping(FlowMeta meta, Map<String, Object> upstream) {
        Map<String, Object> out = new HashMap<>();
        if (meta == null || meta.getInputMapping() == null || upstream == null) {
            return out;
        }
        for (FlowInputMapping m : meta.getInputMapping()) {
            if (m == null || m.getFrom() == null || m.getTo() == null) {
                continue;
            }
            if (upstream.containsKey(m.getFrom())) {
                out.put(m.getTo(), upstream.get(m.getFrom()));
            }
        }
        return out;
    }

    /** 条件不满足则返回 true（跳过本节点）。无 condition 永不跳过。 */
    public boolean shouldSkip(FlowMeta meta, Map<String, Object> ctx) {
        if (meta == null || meta.getCondition() == null) {
            return false;
        }
        FlowCondition c = meta.getCondition();
        Object raw = ctx == null ? null : ctx.get(c.getField());
        String actual = raw == null ? "" : String.valueOf(raw);
        String expected = c.getValue() == null ? "" : c.getValue();
        String op = c.getOp() == null ? "eq" : c.getOp();
        boolean conditionMet;
        switch (op) {
            case "neq":     conditionMet = !actual.equals(expected); break;
            case "contains":conditionMet = actual.contains(expected); break;
            case "eq":
            default:        conditionMet = actual.equals(expected); break;
        }
        return !conditionMet;
    }
}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd content-library-public && mvn -q test -Dtest=FlowMetaEvaluatorTest`
Expected: PASS（6 tests）

- [ ] **Step 5: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/service/pipline/FlowMetaEvaluator.java content-library-public/src/test/java/com/jide/content/service/pipline/FlowMetaEvaluatorTest.java
git commit -m "test(pipline): flow-meta evaluator with input-mapping and condition"
```

---

### Task 3: 快照序列化器 PipLineSnapshotCodec（TDD）

把 `List<PipLineItem>(+flowMeta)` ↔ snapshot JSON 字符串互转。用 `JsonUtil`。

**Files:**
- Create: `content-library-public/src/main/java/com/jide/content/service/pipline/PipLineSnapshotCodec.java`
- Test: `content-library-public/src/test/java/com/jide/content/service/pipline/PipLineSnapshotCodecTest.java`

- [ ] **Step 1: 写失败测试**

```java
package com.jide.content.service.pipline;

import com.jide.content.domain.entity.PipLineItem;
import com.jide.content.domain.pipline.PipLineSnapshot;
import org.junit.jupiter.api.Test;

import java.util.List;

import static org.junit.jupiter.api.Assertions.*;

class PipLineSnapshotCodecTest {

    private final PipLineSnapshotCodec codec = new PipLineSnapshotCodec();

    @Test
    void roundTrip_preservesItemsOrderAndFields() {
        PipLineItem a = new PipLineItem();
        a.setIcon("i0"); a.setName("源"); a.setCode("src"); a.setItemIndex(0);
        a.setData("{\"k\":\"v\"}"); a.setFlowMeta(null);
        PipLineItem b = new PipLineItem();
        b.setIcon("i1"); b.setName("生文"); b.setCode("ai"); b.setItemIndex(1);
        b.setData("{}"); b.setFlowMeta("{\"schemaVersion\":1}");

        String json = codec.toJson(List.of(a, b));
        PipLineSnapshot snap = codec.fromJson(json);

        assertEquals(1, snap.getSchemaVersion());
        assertEquals(2, snap.getItems().size());
        assertEquals("src", snap.getItems().get(0).getCode());
        assertEquals(Integer.valueOf(1), snap.getItems().get(1).getItemIndex());
        assertEquals("{\"schemaVersion\":1}", snap.getItems().get(1).getFlowMeta());
    }

    @Test
    void fromJson_blankReturnsNull() {
        assertNull(codec.fromJson(null));
        assertNull(codec.fromJson(""));
    }
}
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd content-library-public && mvn -q test -Dtest=PipLineSnapshotCodecTest`
Expected: FAIL（类不存在）

- [ ] **Step 3: 实现 PipLineSnapshotCodec**

```java
package com.jide.content.service.pipline;

import com.jide.content.domain.entity.PipLineItem;
import com.jide.content.domain.pipline.PipLineSnapshot;
import com.jide.content.domain.pipline.SnapshotItem;
import com.jide.content.util.JsonUtil;
import org.springframework.stereotype.Component;
import org.springframework.util.StringUtils;

import java.util.ArrayList;
import java.util.List;

@Component
public class PipLineSnapshotCodec {

    public String toJson(List<PipLineItem> items) {
        PipLineSnapshot snap = new PipLineSnapshot();
        List<SnapshotItem> list = new ArrayList<>();
        if (items != null) {
            for (PipLineItem it : items) {
                SnapshotItem si = new SnapshotItem();
                si.setIcon(it.getIcon());
                si.setName(it.getName());
                si.setCode(it.getCode());
                si.setItemIndex(it.getItemIndex());
                si.setData(it.getData());
                si.setFlowMeta(it.getFlowMeta());
                list.add(si);
            }
        }
        snap.setItems(list);
        return JsonUtil.toJson(snap);
    }

    public PipLineSnapshot fromJson(String json) {
        if (!StringUtils.hasText(json)) {
            return null;
        }
        return JsonUtil.toObject(json, PipLineSnapshot.class);
    }
}
```

> 注：若 `JsonUtil` 的方法名不同（如 `parse` / `toBean`），在本步替换为项目实际方法名——先 `grep -n "public static" content-library-public/src/main/java/com/jide/content/util/JsonUtil.java` 确认签名再写。

- [ ] **Step 4: 运行，确认通过**

Run: `cd content-library-public && mvn -q test -Dtest=PipLineSnapshotCodecTest`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/service/pipline/PipLineSnapshotCodec.java content-library-public/src/test/java/com/jide/content/service/pipline/PipLineSnapshotCodecTest.java
git commit -m "test(pipline): snapshot codec round-trip"
```

---

## Phase 2 — 后端持久化与接口

### Task 4: 实体字段 + PipLineVersion 实体 + Mapper

**Files:**
- Modify: `content-library-public/src/main/java/com/jide/content/domain/entity/PipLine.java`
- Modify: `content-library-public/src/main/java/com/jide/content/domain/entity/PipLineItem.java`
- Create: `content-library-public/src/main/java/com/jide/content/domain/entity/PipLineVersion.java`
- Create: `content-library-public/src/main/java/com/jide/content/mapper/PipLineVersionMapper.java`

- [ ] **Step 1: PipLine 加字段**

在 `PipLine.java` 字段区末尾（`description` 之后）添加：
```java
    private String draftSnapshot;

    private Integer hasDraft;
```

- [ ] **Step 2: PipLineItem 加字段**

在 `PipLineItem.java` `data` 字段之后添加：
```java
    private String flowMeta;
```
（若 Task 0 判定 `data` 字段缺失，确保此处同时存在 `private String data;`。）

- [ ] **Step 3: 创建 PipLineVersion 实体**

```java
package com.jide.content.domain.entity;

import lombok.Data;
import lombok.EqualsAndHashCode;

@EqualsAndHashCode(callSuper = true)
@Data
public class PipLineVersion extends BaseAutoIncrementEntity {
    private Long pipLineId;
    private Integer versionNo;
    private String snapshot;
    private String remark;
    private Long createdBy;
}
```

- [ ] **Step 4: 创建 Mapper**

```java
package com.jide.content.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.jide.content.domain.entity.PipLineVersion;
import org.apache.ibatis.annotations.Mapper;

@Mapper
public interface PipLineVersionMapper extends BaseMapper<PipLineVersion> {
}
```
> 确认 `@Mapper` vs `@MapperScan` 约定：先看现有 `PipLineMapper.java` 是否带 `@Mapper`，与之保持一致。

- [ ] **Step 5: 编译验证**

Run: `cd content-library-public && mvn -q -DskipTests compile`
Expected: BUILD SUCCESS

- [ ] **Step 6: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/domain/entity/ content-library-public/src/main/java/com/jide/content/mapper/PipLineVersionMapper.java
git commit -m "feat(pipline): entity fields + version entity/mapper"
```

---

### Task 5: 版本服务 PipLineVersionService（版本号计算 TDD）

**Files:**
- Create: `content-library-public/src/main/java/com/jide/content/service/PipLineVersionService.java`
- Create: `content-library-public/src/main/java/com/jide/content/service/impl/PipLineVersionServiceImpl.java`
- Test: `content-library-public/src/test/java/com/jide/content/service/impl/PipLineVersionServiceImplTest.java`

- [ ] **Step 1: 接口**

```java
package com.jide.content.service;

import com.jide.content.domain.entity.PipLineVersion;
import java.util.List;

public interface PipLineVersionService {
    /** 写入一条新版本，version_no = 该 pipLine 现有最大版本号 + 1，返回新版本号 */
    int createVersion(Long pipLineId, String snapshot, String remark, Long createdBy);
    List<PipLineVersion> listByPipLine(Long pipLineId);
    PipLineVersion getById(Long id);
}
```

- [ ] **Step 2: 写失败测试（mock mapper，验证版本号自增逻辑）**

```java
package com.jide.content.service.impl;

import com.jide.content.domain.entity.PipLineVersion;
import com.jide.content.mapper.PipLineVersionMapper;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;

import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.mockito.Mockito.*;

class PipLineVersionServiceImplTest {

    @Test
    void createVersion_incrementsFromMaxExisting() {
        PipLineVersionMapper mapper = mock(PipLineVersionMapper.class);
        PipLineVersion existing = new PipLineVersion();
        existing.setVersionNo(3);
        when(mapper.selectList(any())).thenReturn(List.of(existing));

        PipLineVersionServiceImpl svc = new PipLineVersionServiceImpl();
        svc.setMapperForTest(mapper);

        int newNo = svc.createVersion(100L, "{}", "r", 9L);

        assertEquals(4, newNo);
        ArgumentCaptor<PipLineVersion> cap = ArgumentCaptor.forClass(PipLineVersion.class);
        verify(mapper).insert(cap.capture());
        assertEquals(Integer.valueOf(4), cap.getValue().getVersionNo());
        assertEquals(Long.valueOf(100L), cap.getValue().getPipLineId());
    }

    @Test
    void createVersion_firstVersionIsOne() {
        PipLineVersionMapper mapper = mock(PipLineVersionMapper.class);
        when(mapper.selectList(any())).thenReturn(List.of());
        PipLineVersionServiceImpl svc = new PipLineVersionServiceImpl();
        svc.setMapperForTest(mapper);
        assertEquals(1, svc.createVersion(1L, "{}", null, 1L));
    }
}
```

- [ ] **Step 3: 运行，确认失败**

Run: `cd content-library-public && mvn -q test -Dtest=PipLineVersionServiceImplTest`
Expected: FAIL（类不存在）

- [ ] **Step 4: 实现 impl**

```java
package com.jide.content.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.jide.content.domain.entity.PipLineVersion;
import com.jide.content.mapper.PipLineVersionMapper;
import com.jide.content.service.PipLineVersionService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;

import java.util.List;

@Service
public class PipLineVersionServiceImpl implements PipLineVersionService {

    @Autowired
    private PipLineVersionMapper pipLineVersionMapper;

    /** 仅供单测注入 mock */
    void setMapperForTest(PipLineVersionMapper mapper) {
        this.pipLineVersionMapper = mapper;
    }

    @Override
    public int createVersion(Long pipLineId, String snapshot, String remark, Long createdBy) {
        int next = listByPipLine(pipLineId).stream()
                .map(PipLineVersion::getVersionNo)
                .filter(java.util.Objects::nonNull)
                .max(Integer::compareTo)
                .orElse(0) + 1;
        PipLineVersion v = new PipLineVersion();
        v.setPipLineId(pipLineId);
        v.setVersionNo(next);
        v.setSnapshot(snapshot);
        v.setRemark(remark);
        v.setCreatedBy(createdBy);
        pipLineVersionMapper.insert(v);
        return next;
    }

    @Override
    public List<PipLineVersion> listByPipLine(Long pipLineId) {
        LambdaQueryWrapper<PipLineVersion> qw = new LambdaQueryWrapper<>();
        qw.eq(PipLineVersion::getPipLineId, pipLineId);
        qw.orderByDesc(PipLineVersion::getVersionNo);
        return pipLineVersionMapper.selectList(qw);
    }

    @Override
    public PipLineVersion getById(Long id) {
        return pipLineVersionMapper.selectById(id);
    }
}
```

- [ ] **Step 5: 运行，确认通过**

Run: `cd content-library-public && mvn -q test -Dtest=PipLineVersionServiceImplTest`
Expected: PASS（2 tests）

- [ ] **Step 6: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/service/PipLineVersionService.java content-library-public/src/main/java/com/jide/content/service/impl/PipLineVersionServiceImpl.java content-library-public/src/test/java/com/jide/content/service/impl/PipLineVersionServiceImplTest.java
git commit -m "test(pipline): version service with per-pipline incrementing version_no"
```

---

### Task 6: 草稿服务与端点（saveDraft / publishDraft / discardDraft）

**Files:**
- Create: `content-library-public/src/main/java/com/jide/content/domain/request/pipline/PipLineDraftSaveRequest.java`
- Create: `content-library-public/src/main/java/com/jide/content/domain/request/pipline/PipLinePublishRequest.java`
- Create: `content-library-public/src/main/java/com/jide/content/service/PipLineDraftService.java`
- Create: `content-library-public/src/main/java/com/jide/content/service/impl/PipLineDraftServiceImpl.java`
- Modify: `content-library-public/src/main/java/com/jide/content/controller/PipLineController.java`

- [ ] **Step 1: 请求 DTO**

```java
package com.jide.content.domain.request.pipline;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

@Data
public class PipLineDraftSaveRequest {
    @NotNull(message = "工作流id不能为空")
    private Long pipLineId;
    /** 前端序列化好的草稿快照 JSON 字符串（PipLineSnapshot 结构） */
    @NotNull(message = "草稿快照不能为空")
    private String snapshot;
}
```

```java
package com.jide.content.domain.request.pipline;

import jakarta.validation.constraints.NotNull;
import lombok.Data;

@Data
public class PipLinePublishRequest {
    @NotNull(message = "工作流id不能为空")
    private Long pipLineId;
    private String remark;
}
```

- [ ] **Step 2: 接口**

```java
package com.jide.content.service;

import com.jide.content.domain.request.pipline.PipLineDraftSaveRequest;
import com.jide.content.domain.request.pipline.PipLinePublishRequest;

public interface PipLineDraftService {
    void saveDraft(PipLineDraftSaveRequest request);
    /** 应用草稿到 live items + 写版本快照，返回新版本号 */
    int publishDraft(PipLinePublishRequest request);
    void discardDraft(Long pipLineId);
}
```

- [ ] **Step 3: 实现（事务：重建 item 行 + 写版本）**

```java
package com.jide.content.service.impl;

import com.baomidou.mybatisplus.core.conditions.query.LambdaQueryWrapper;
import com.jide.content.domain.entity.PipLine;
import com.jide.content.domain.entity.PipLineItem;
import com.jide.content.domain.pipline.PipLineSnapshot;
import com.jide.content.domain.pipline.SnapshotItem;
import com.jide.content.domain.pojo.AccountInfo;
import com.jide.content.domain.request.pipline.PipLineDraftSaveRequest;
import com.jide.content.domain.request.pipline.PipLinePublishRequest;
import com.jide.content.exception.BusinessException;
import com.jide.content.filter.WebContextFilter;
import com.jide.content.mapper.PipLineItemMapper;
import com.jide.content.mapper.PipLineMapper;
import com.jide.content.service.PipLineDraftService;
import com.jide.content.service.PipLineVersionService;
import com.jide.content.service.pipline.PipLineSnapshotCodec;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.util.StringUtils;

import java.util.List;

@Service
public class PipLineDraftServiceImpl implements PipLineDraftService {

    @Autowired private PipLineMapper pipLineMapper;
    @Autowired private PipLineItemMapper pipLineItemMapper;
    @Autowired private PipLineVersionService pipLineVersionService;
    @Autowired private PipLineSnapshotCodec snapshotCodec;

    private PipLine loadOwned(Long pipLineId) {
        AccountInfo acc = WebContextFilter.getAccountInfoRequired();
        PipLine pl = pipLineMapper.selectById(pipLineId);
        if (pl == null) {
            throw new BusinessException("工作流不存在");
        }
        boolean common = pl.getCommon() != null && pl.getCommon() == 1;
        if (!common && !acc.getId().equals(pl.getAccountId())) {
            throw new BusinessException("无权操作该工作流");
        }
        return pl;
    }

    @Override
    public void saveDraft(PipLineDraftSaveRequest request) {
        PipLine pl = loadOwned(request.getPipLineId());
        pl.setDraftSnapshot(request.getSnapshot());
        pl.setHasDraft(1);
        pipLineMapper.updateById(pl);
    }

    @Override
    @Transactional(rollbackFor = Exception.class)
    public int publishDraft(PipLinePublishRequest request) {
        PipLine pl = loadOwned(request.getPipLineId());
        if (!StringUtils.hasText(pl.getDraftSnapshot()) ||
                pl.getHasDraft() == null || pl.getHasDraft() == 0) {
            throw new BusinessException("没有可发布的草稿");
        }
        PipLineSnapshot snap = snapshotCodec.fromJson(pl.getDraftSnapshot());
        if (snap == null || snap.getItems() == null || snap.getItems().isEmpty()) {
            throw new BusinessException("草稿内容为空");
        }
        // 1) 删除该 pipLine 现有 item 行
        LambdaQueryWrapper<PipLineItem> del = new LambdaQueryWrapper<>();
        del.eq(PipLineItem::getPipLineId, pl.getId());
        pipLineItemMapper.delete(del);
        // 2) 按草稿插入新 item 行
        for (SnapshotItem si : snap.getItems()) {
            PipLineItem it = new PipLineItem();
            it.setAccountId(pl.getAccountId());
            it.setPipLineId(pl.getId());
            it.setIcon(si.getIcon());
            it.setName(si.getName());
            it.setCode(si.getCode());
            it.setItemIndex(si.getItemIndex());
            it.setData(si.getData());
            it.setFlowMeta(si.getFlowMeta());
            pipLineItemMapper.insert(it);
        }
        // 3) 写版本快照（用 live items 的规范化快照）
        List<PipLineItem> liveItems = pipLineItemMapper.selectList(
                new LambdaQueryWrapper<PipLineItem>()
                        .eq(PipLineItem::getPipLineId, pl.getId())
                        .orderByAsc(PipLineItem::getItemIndex));
        int versionNo = pipLineVersionService.createVersion(
                pl.getId(), snapshotCodec.toJson(liveItems),
                request.getRemark(), WebContextFilter.getAccountInfoRequired().getId());
        // 4) 清空草稿
        pl.setDraftSnapshot(null);
        pl.setHasDraft(0);
        pipLineMapper.updateById(pl);
        return versionNo;
    }

    @Override
    public void discardDraft(Long pipLineId) {
        PipLine pl = loadOwned(pipLineId);
        pl.setDraftSnapshot(null);
        pl.setHasDraft(0);
        pipLineMapper.updateById(pl);
    }
}
```

- [ ] **Step 4: 加 controller 端点**

在 `PipLineController.java` 注入 `PipLineDraftService` 并添加（在 class 内、已有方法之后）：
```java
    @Autowired
    private PipLineDraftService pipLineDraftService;

    @PostMapping("/saveDraft")
    public R<Void> saveDraft(@RequestBody @Validated PipLineDraftSaveRequest request) {
        pipLineDraftService.saveDraft(request);
        return R.ok();
    }

    @PostMapping("/publishDraft")
    public R<Integer> publishDraft(@RequestBody @Validated PipLinePublishRequest request) {
        return R.ok(pipLineDraftService.publishDraft(request));
    }

    @PostMapping("/discardDraft")
    public R<Void> discardDraft(@RequestBody @Validated IdRequest request) {
        pipLineDraftService.discardDraft(request.getId());
        return R.ok();
    }
```
顶部补 import：`PipLineDraftService`、`PipLineDraftSaveRequest`、`PipLinePublishRequest`（`IdRequest`/`R`/`@Validated` 已有）。确认 `IdRequest` 有 `getId()`。

- [ ] **Step 5: 编译验证**

Run: `cd content-library-public && mvn -q -DskipTests compile`
Expected: BUILD SUCCESS

- [ ] **Step 6: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/domain/request/pipline/PipLineDraftSaveRequest.java content-library-public/src/main/java/com/jide/content/domain/request/pipline/PipLinePublishRequest.java content-library-public/src/main/java/com/jide/content/service/PipLineDraftService.java content-library-public/src/main/java/com/jide/content/service/impl/PipLineDraftServiceImpl.java content-library-public/src/main/java/com/jide/content/controller/PipLineController.java
git commit -m "feat(pipline): draft save/publish/discard endpoints"
```

---

### Task 7: 版本端点（list / detail / rollback）

回溯 = 把指定版本快照写回 `draft_snapshot`（不覆盖 live），由用户在前端确认后再 publish。

**Files:**
- Create: `content-library-public/src/main/java/com/jide/content/domain/vo/pipline/PipLineVersionVO.java`
- Create: `content-library-public/src/main/java/com/jide/content/controller/PipLineVersionController.java`
- Modify: `content-library-public/src/main/java/com/jide/content/service/PipLineDraftService.java`
- Modify: `content-library-public/src/main/java/com/jide/content/service/impl/PipLineDraftServiceImpl.java`

- [ ] **Step 1: VO**

```java
package com.jide.content.domain.vo.pipline;

import lombok.Data;
import java.time.LocalDateTime;

@Data
public class PipLineVersionVO {
    private Long id;
    private Long pipLineId;
    private Integer versionNo;
    private String remark;
    private Long createdBy;
    private LocalDateTime createTime;
    /** detail 时才带 snapshot；list 时为 null */
    private String snapshot;
}
```

- [ ] **Step 2: rollback 逻辑加到 DraftService**

接口加：
```java
    /** 把指定版本快照载入草稿（不覆盖 live） */
    void rollbackToDraft(Long versionId);
```
impl 加（注入 `PipLineVersionService` 已在 Task 6 有）：
```java
    @Override
    public void rollbackToDraft(Long versionId) {
        var version = pipLineVersionService.getById(versionId);
        if (version == null) {
            throw new BusinessException("版本不存在");
        }
        PipLine pl = loadOwned(version.getPipLineId());
        pl.setDraftSnapshot(version.getSnapshot());
        pl.setHasDraft(1);
        pipLineMapper.updateById(pl);
    }
```
（`PipLineVersionService` 字段已在 Task 6 注入。补 import：`com.jide.content.domain.entity.PipLineVersion` 或用 `var`。）

- [ ] **Step 3: Controller**

```java
package com.jide.content.controller;

import com.jide.content.domain.entity.PipLineVersion;
import com.jide.content.domain.pojo.R;
import com.jide.content.domain.request.IdRequest;
import com.jide.content.domain.vo.pipline.PipLineVersionVO;
import com.jide.content.service.PipLineDraftService;
import com.jide.content.service.PipLineVersionService;
import org.springframework.beans.BeanUtils;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.validation.annotation.Validated;
import org.springframework.web.bind.annotation.*;

import java.util.ArrayList;
import java.util.List;

@RestController
@RequestMapping("/pipLineVersion")
public class PipLineVersionController {

    @Autowired private PipLineVersionService pipLineVersionService;
    @Autowired private PipLineDraftService pipLineDraftService;

    @PostMapping("/list")
    public R<List<PipLineVersionVO>> list(@RequestBody PipLineVersionListRequest request) {
        List<PipLineVersionVO> result = new ArrayList<>();
        for (PipLineVersion v : pipLineVersionService.listByPipLine(request.getPipLineId())) {
            PipLineVersionVO vo = new PipLineVersionVO();
            BeanUtils.copyProperties(v, vo);
            vo.setSnapshot(null);
            result.add(vo);
        }
        return R.ok(result);
    }

    @PostMapping("/detail")
    public R<PipLineVersionVO> detail(@RequestBody @Validated IdRequest request) {
        PipLineVersion v = pipLineVersionService.getById(request.getId());
        if (v == null) return R.failed("版本不存在");
        PipLineVersionVO vo = new PipLineVersionVO();
        BeanUtils.copyProperties(v, vo);
        return R.ok(vo);
    }

    @PostMapping("/rollback")
    public R<Void> rollback(@RequestBody @Validated IdRequest request) {
        pipLineDraftService.rollbackToDraft(request.getId());
        return R.ok();
    }

    @lombok.Data
    static class PipLineVersionListRequest {
        private Long pipLineId;
    }
}
```
> 若团队约定 request 不用内部类，改为独立 `PipLineVersionListRequest.java` 放 `domain.request.pipline`。

- [ ] **Step 4: 编译验证**

Run: `cd content-library-public && mvn -q -DskipTests compile`
Expected: BUILD SUCCESS

- [ ] **Step 5: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/domain/vo/pipline/PipLineVersionVO.java content-library-public/src/main/java/com/jide/content/controller/PipLineVersionController.java content-library-public/src/main/java/com/jide/content/service/PipLineDraftService.java content-library-public/src/main/java/com/jide/content/service/impl/PipLineDraftServiceImpl.java
git commit -m "feat(pipline): version list/detail/rollback endpoints"
```

---

### Task 8: 执行器接入 inputMapping / condition

把 `FlowMetaEvaluator` 接入 `PipLineRunServiceImpl` 的 work 节点循环：跑节点前按 flowMeta 注入输入、按 condition 跳过。

**Files:**
- Modify: `content-library-public/src/main/java/com/jide/content/service/impl/PipLineRunServiceImpl.java`
- Read first: 完整通读 `PipLineRunServiceImpl.java` 的 work 节点遍历段（约 line 130-200），定位"取 workList 中每个 item、调用 `textWorkServiceMap.get(code)` 执行"的位置。

- [ ] **Step 1: 通读并定位插入点**

Run: `grep -n "workList\|textWorkServiceMap\|TextPipLineContext\|flowMeta\|getData" content-library-public/src/main/java/com/jide/content/service/impl/PipLineRunServiceImpl.java`
确认：(a) work 节点循环变量名；(b) 上游上下文对象（`TextPipLineContext`）如何暴露字段为 Map（若无 `toMap()`，则下一步用其现有 getter 构造 Map，或读其内部 `Map data` 字段）。

- [ ] **Step 2: 注入 evaluator 字段**

在类字段区加：
```java
    @Autowired
    private com.jide.content.service.pipline.FlowMetaEvaluator flowMetaEvaluator;
```

- [ ] **Step 3: 在 work 节点执行前应用 flowMeta**

在每个 work `item` 执行前插入（变量名按 Step 1 实际调整；`ctxMap` 为上游上下文字段 Map）：
```java
    com.jide.content.domain.pipline.FlowMeta meta = null;
    if (org.springframework.util.StringUtils.hasText(item.getFlowMeta())) {
        meta = com.jide.content.util.JsonUtil.toObject(item.getFlowMeta(),
                com.jide.content.domain.pipline.FlowMeta.class);
    }
    if (flowMetaEvaluator.shouldSkip(meta, ctxMap)) {
        // 记 INFO 日志并跳过本节点（沿用现有 addLog 调用签名）
        pipLineLogService.addLog(/* INFO */, "条件不满足，跳过节点: " + item.getName(), ...);
        continue;
    }
    java.util.Map<String, Object> injected = flowMetaEvaluator.applyInputMapping(meta, ctxMap);
    // 把 injected 合并进将传给 workService 的输入（沿用现有传参方式：
    // 若 workService.run 接收 node.data(JSON) ，则把 injected 合并进解析后的 data map 再序列化回去）
```
> 关键约束（CLAUDE 风格说明）：保持现有 `_release` / 日志 / 异常路径不变；本改动只在"取到 item 后、调用 workService 前"插入，不改变线性遍历结构。`addLog` 的具体参数顺序以现有调用为准（Step 1 已确认）。

- [ ] **Step 4: 编译验证**

Run: `cd content-library-public && mvn -q -DskipTests compile`
Expected: BUILD SUCCESS

- [ ] **Step 5: 回归既有单测**

Run: `cd content-library-public && mvn -q test`
Expected: 既有测试 + 新增 evaluator/codec/version 测试全部 PASS

- [ ] **Step 6: 提交**

```bash
git add content-library-public/src/main/java/com/jide/content/service/impl/PipLineRunServiceImpl.java
git commit -m "feat(pipline): apply input-mapping and skip-condition during run"
```

---

## Phase 3 — 前端（pc-admin-conetnt-library-public）

> 无前端测试框架。每个 Task 用 `pnpm --filter ... build` + lint 验证，外加手动冒烟。前端命令（仓库根）：
> `pnpm --filter <pkgname> build` —— 先 `grep '"name"' pc-admin-conetnt-library-public/package.json` 取包名。

### Task 9: api/index.js 新增 6 个端点封装

**Files:**
- Modify: `pc-admin-conetnt-library-public/src/api/index.js`

- [ ] **Step 1: 在 pipLine 端点区（`cronList` 之后）追加**

```javascript
// 保存工作流草稿
export const savePipLineDraft = params => http.post('pipLine/saveDraft', params)

// 发布工作流草稿
export const publishPipLineDraft = params => http.post('pipLine/publishDraft', params)

// 丢弃工作流草稿
export const discardPipLineDraft = params => http.post('pipLine/discardDraft', params)

// 工作流版本列表
export const pipLineVersionList = params => http.post('pipLineVersion/list', params)

// 工作流版本详情
export const pipLineVersionDetail = params => http.post('pipLineVersion/detail', params)

// 回溯到指定版本（载入草稿）
export const pipLineVersionRollback = params => http.post('pipLineVersion/rollback', params)
```

- [ ] **Step 2: 构建验证**

Run: `pnpm --filter <pkgname> build`
Expected: 构建成功，无未定义引用

- [ ] **Step 3: 提交**

```bash
git add pc-admin-conetnt-library-public/src/api/index.js
git commit -m "feat(pipline): frontend api bindings for draft and version"
```

---

### Task 10: 节点属性抽屉新增「数据传递」分栏

**Files:**
- Modify: `pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue`
- Read first: 通读该文件的抽屉（`<Drawer>`）模板段与 `selectPlugin` / 节点保存方法，确认表单字段来源（插件 `form` 定义）。

- [ ] **Step 1: 通读抽屉与保存逻辑**

Run: `grep -n "Drawer\|selectPlugin\|updatePipLineItem\|form\|drawer" pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue`
记录：当前节点对象变量名、保存方法名、上游节点列表来源。

- [ ] **Step 2: 在抽屉表单后加「数据传递」分栏（template）**

在现有插件表单 `<Form>` 之后插入（变量名按 Step 1 调整）：
```html
<Divider plain>数据传递</Divider>
<FormItem label="上游节点">
  <Select v-model="flowMeta.dependsOnIndex" clearable placeholder="默认上一个节点">
    <Option v-for="n in upstreamNodes" :key="n.itemIndex" :value="n.itemIndex">
      {{ n.name }}（#{{ n.itemIndex }}）
    </Option>
  </Select>
</FormItem>
<FormItem label="字段映射">
  <div v-for="(m, i) in flowMeta.inputMapping" :key="i" class="mapping-row">
    <Input v-model="m.from" placeholder="上游字段" style="width:42%" />
    <span style="margin:0 6px">→</span>
    <Input v-model="m.to" placeholder="本节点字段" style="width:42%" />
    <Button type="text" @click="flowMeta.inputMapping.splice(i,1)">删除</Button>
  </div>
  <Button size="small" @click="flowMeta.inputMapping.push({from:'',to:''})">+ 添加映射</Button>
</FormItem>
<FormItem label="跳过条件">
  <Input v-model="flowMeta.condition.field" placeholder="字段" style="width:30%" />
  <Select v-model="flowMeta.condition.op" style="width:22%;margin:0 4px">
    <Option value="eq">等于</Option>
    <Option value="neq">不等于</Option>
    <Option value="contains">包含</Option>
  </Select>
  <Input v-model="flowMeta.condition.value" placeholder="值" style="width:30%" />
</FormItem>
```

- [ ] **Step 3: data() 加 flowMeta 模型 + 打开抽屉时回填**

在组件 `data()` 返回对象加：
```javascript
flowMeta: { schemaVersion: 1, dependsOnIndex: null, inputMapping: [], condition: { field: '', op: 'eq', value: '' } },
```
新增 computed `upstreamNodes`（返回当前节点之前的节点列表，来源为已加载的节点数组——用 Step 1 记录的变量名）：
```javascript
upstreamNodes() {
  return (this.nodeList || []).filter(n => n && n.itemIndex < this.currentItemIndex)
}
```
打开抽屉时（编辑某节点的方法内）回填：
```javascript
const fm = node.flowMeta ? JSON.parse(node.flowMeta) : null
this.flowMeta = fm
  ? { schemaVersion: 1, dependsOnIndex: fm.dependsOnIndex ?? null,
      inputMapping: fm.inputMapping || [],
      condition: fm.condition || { field: '', op: 'eq', value: '' } }
  : { schemaVersion: 1, dependsOnIndex: null, inputMapping: [], condition: { field: '', op: 'eq', value: '' } }
```

- [ ] **Step 4: 保存节点时把 flowMeta 序列化进节点对象**

在节点保存方法里，组装提交体时加入：
```javascript
const condition = this.flowMeta.condition && this.flowMeta.condition.field
  ? this.flowMeta.condition : null
const flowMetaStr = JSON.stringify({
  schemaVersion: 1,
  dependsOnIndex: this.flowMeta.dependsOnIndex,
  inputMapping: (this.flowMeta.inputMapping || []).filter(m => m.from && m.to),
  condition
})
// 写入将随草稿快照保存的节点对象的 flowMeta 字段
node.flowMeta = flowMetaStr
```
> 注意：节点改动不再直接调 `updatePipLineItem` 即时落库，而是更新内存节点对象，由 Task 11 的"保存草稿"统一提交（见下）。若当前实现是即时落库，本步保留即时落库**且**把 `flowMeta` 一并带上，草稿流为叠加能力。

- [ ] **Step 5: 构建验证 + 手动冒烟**

Run: `pnpm --filter <pkgname> build`
Expected: 构建成功。手动：打开节点抽屉 → 配置映射/条件 → 保存 → 重新打开能回填。

- [ ] **Step 6: 提交**

```bash
git add pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue
git commit -m "feat(pipline): data-passing panel in node drawer"
```

---

### Task 11: 草稿/发布/丢弃按钮 + has_draft 状态

**Files:**
- Modify: `pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue`

- [ ] **Step 1: 顶部工具区加按钮（template）**

在编排页头部操作区加：
```html
<Badge v-if="hasDraft" dot>
  <Tag color="orange">有未发布草稿</Tag>
</Badge>
<Button @click="onSaveDraft">保存草稿</Button>
<Button type="primary" @click="onPublish">发布</Button>
<Button @click="onDiscardDraft" :disabled="!hasDraft">丢弃草稿</Button>
<Button @click="versionDrawerOpen = true">版本历史</Button>
```

- [ ] **Step 2: data() + 方法**

data 加：`hasDraft: false, versionDrawerOpen: false`。引入 api：
```javascript
import { savePipLineDraft, publishPipLineDraft, discardPipLineDraft } from '@/api'
```
方法（`buildSnapshot()` 把当前内存节点数组转成后端 `PipLineSnapshot` 结构 JSON 字符串）：
```javascript
buildSnapshot() {
  const items = (this.nodeList || []).filter(Boolean).map(n => ({
    icon: n.icon, name: n.name, code: n.code, itemIndex: n.itemIndex,
    data: typeof n.data === 'string' ? n.data : JSON.stringify(n.data || {}),
    flowMeta: n.flowMeta || null
  }))
  return JSON.stringify({ schemaVersion: 1, items })
},
async onSaveDraft() {
  await savePipLineDraft({ pipLineId: this.pipLineId, snapshot: this.buildSnapshot() })
  this.hasDraft = true
  this.$Message.success('草稿已保存')
},
async onPublish() {
  // 发布前确保草稿是最新的
  await savePipLineDraft({ pipLineId: this.pipLineId, snapshot: this.buildSnapshot() })
  const res = await publishPipLineDraft({ pipLineId: this.pipLineId })
  this.hasDraft = false
  this.$Message.success('已发布，版本 v' + res.data)
},
onDiscardDraft() {
  this.$Modal.confirm({
    title: '丢弃草稿', content: '将丢弃未发布的改动，确认？',
    onOk: async () => {
      await discardPipLineDraft({ id: this.pipLineId })
      this.hasDraft = false
      this.$Message.success('草稿已丢弃')
      this.reloadNodes()  // 用 Step1(Task10) 记录的节点加载方法名
    }
  })
}
```
> `pipLineId` / `nodeList` / `reloadNodes` 用文件实际变量名（Task 10 Step 1 已记录）。`res.data` 为后端返回的新版本号。打开页面加载工作流时，把 `pipLine.hasDraft` 读入 `this.hasDraft`。

- [ ] **Step 3: 构建验证 + 手动冒烟**

Run: `pnpm --filter <pkgname> build`
手动：改节点 → 保存草稿（运行版本不变）→ 发布（节点落库 + 提示版本号）→ 丢弃草稿恢复。

- [ ] **Step 4: 提交**

```bash
git add pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue
git commit -m "feat(pipline): draft save/publish/discard controls"
```

---

### Task 12: 版本历史抽屉

**Files:**
- Create: `pc-admin-conetnt-library-public/src/views/pipLine/list/item/VersionHistoryDrawer.vue`
- Modify: `pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue`（引用组件）

- [ ] **Step 1: 创建组件**

```html
<template>
  <Drawer title="版本历史" :model-value="modelValue" width="420"
          @on-close="$emit('update:modelValue', false)">
    <Table :columns="columns" :data="rows" />
  </Drawer>
</template>

<script>
import { pipLineVersionList, pipLineVersionRollback } from '@/api'
export default {
  name: 'VersionHistoryDrawer',
  props: { modelValue: Boolean, pipLineId: { type: [Number, String], default: null } },
  emits: ['update:modelValue', 'rolled-back'],
  data() {
    return {
      rows: [],
      columns: [
        { title: '版本', key: 'versionNo', width: 80 },
        { title: '备注', key: 'remark' },
        { title: '时间', key: 'createTime' },
        {
          title: '操作', width: 90,
          render: (h, { row }) => h('Button', {
            props: { type: 'text', size: 'small' },
            on: { click: () => this.rollback(row) }
          }, '回溯')
        }
      ]
    }
  },
  watch: {
    modelValue(v) { if (v && this.pipLineId) this.load() }
  },
  methods: {
    async load() {
      const res = await pipLineVersionList({ pipLineId: this.pipLineId })
      this.rows = res.data || []
    },
    rollback(row) {
      this.$Modal.confirm({
        title: '回溯到 v' + row.versionNo,
        content: '将把该版本载入草稿，需手动发布后才生效。继续？',
        onOk: async () => {
          await pipLineVersionRollback({ id: row.id })
          this.$Message.success('已载入草稿，请在编辑器中确认后发布')
          this.$emit('rolled-back')
          this.$emit('update:modelValue', false)
        }
      })
    }
  }
}
</script>
```
> `render` 函数 API 以项目 view-ui-plus 版本为准（若用 `h(resolveComponent('Button'))` 则相应调整；先看现有 Table render 用法 `grep -rn "render:" pc-admin-conetnt-library-public/src/views/pipLine`）。

- [ ] **Step 2: 在 PipLineItem.vue 引用**

```html
<VersionHistoryDrawer v-model="versionDrawerOpen" :pip-line-id="pipLineId" @rolled-back="onRolledBack" />
```
script：
```javascript
import VersionHistoryDrawer from './VersionHistoryDrawer.vue'
// components: { VersionHistoryDrawer, ... }
// methods:
onRolledBack() { this.hasDraft = true; this.reloadNodes() }
```

- [ ] **Step 3: 构建验证 + 手动冒烟**

Run: `pnpm --filter <pkgname> build`
手动：发布 ≥2 次产生多版本 → 打开版本历史 → 回溯旧版本 → 提示载入草稿 → 编辑器显示有草稿 → 发布生成新版本号。

- [ ] **Step 4: 提交**

```bash
git add pc-admin-conetnt-library-public/src/views/pipLine/list/item/VersionHistoryDrawer.vue pc-admin-conetnt-library-public/src/views/pipLine/list/item/PipLineItem.vue
git commit -m "feat(pipline): version history drawer with rollback"
```

---

## 手动冒烟脚本（端到端验证）

> 在后端可运行 + DB 已执行迁移的环境。替换 `$TOKEN`（登录态 cookie/header）、`$BASE`、`$PID`（工作流 id）。沿用项目鉴权方式（`WebContextFilter` 取账号——通常靠 cookie/session）。

```bash
BASE=http://localhost:8080
# 1) 保存草稿
curl -s -X POST $BASE/pipLine/saveDraft -H 'Content-Type: application/json' \
  -d '{"pipLineId":'$PID',"snapshot":"{\"schemaVersion\":1,\"items\":[{\"icon\":\"i\",\"name\":\"源\",\"code\":\"src\",\"itemIndex\":0,\"data\":\"{}\",\"flowMeta\":null}]}"}'
# 2) 发布草稿（应返回新版本号）
curl -s -X POST $BASE/pipLine/publishDraft -H 'Content-Type: application/json' -d '{"pipLineId":'$PID',"remark":"smoke"}'
# 3) 版本列表
curl -s -X POST $BASE/pipLineVersion/list -H 'Content-Type: application/json' -d '{"pipLineId":'$PID'}'
# 4) 回溯（version id 取自上一步）
curl -s -X POST $BASE/pipLineVersion/rollback -H 'Content-Type: application/json' -d '{"id":<versionId>}'
# 5) 校验：pip_line.has_draft=1 且 draft_snapshot 非空；丢弃
curl -s -X POST $BASE/pipLine/discardDraft -H 'Content-Type: application/json' -d '{"id":'$PID'}'
```

验收对照 spec §7：1 数据传递落库（节点 flow_meta）；2 执行注入/跳过留痕（看 pip_line_log）；3 草稿不影响线上、发布后一致并生成版本；4 版本列表/回溯载入草稿；5 旧工作流行为不变。

---

## Self-Review 结果

- **Spec 覆盖**：§3.1 数据传递→Task 1/2/8/10；§3.2 草稿→Task 4/6/11；§3.3 版本→Task 4/5/7/12；§3.4 属性面板→Task 10；DDL/风险#1→Task 0；§7 验收→手动冒烟脚本。无遗漏。
- **占位符扫描**：无 TBD/TODO；纯逻辑步骤均给完整代码；DB/UI 步骤给完整新增代码 + 明确插入点 +「先 grep 确认现有签名」的核对指令（因不能假设未读文件的精确变量名）。
- **类型一致性**：`FlowMeta`/`SnapshotItem`/`PipLineSnapshot` 跨 Task 字段名一致；`createVersion(...)` 签名在 Task 5 定义、Task 6 调用一致；前端 `flowMeta`/`buildSnapshot`/`hasDraft` 跨 Task 10/11/12 一致。
