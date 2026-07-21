# Session Memory 系统前后端功能一致性分析报告

> 日期: 2026-07-12 | 后端: http://100.77.184.40:8000 | 前端: / (Dashboard) + /app (Session Center)

---

## 1. API 全景 vs 前端实现对照

### 后端统计：49 个 API 端点，10 个分组

| 分组 | 端点数 | 前端已用 | 前端缺失 | 状态 |
|------|--------|----------|----------|------|
| admin (config/dashboard/ops) | 9 | 6 | 3 | 部分 |
| atoms | 1 | 1 | 0 | ✓ |
| kg (图谱+实体+关系) | 9 | 1 | **8** | **严重缺失** |
| plugin-state | 8 | 0 | **8** | **完全缺失** |
| projects | 2 | 0 | **2** | **完全缺失** |
| query (统一检索) | 1 | 0 | **1** | **关键缺失** |
| sessions (CRUD+子资源) | 20 | 5 | **15** | **大量缺失** |
| users (persona/scenarios) | 5 | 1 | **4** | **严重缺失** |
| hybrid-search / proactive-prefetch | 2 | 0 | **2** | **关键缺失** |
| health/metrics | 2 | 1 | 1 | 部分 |
| **合计** | **59** | **15** | **44** | **覆盖率 25%** |

---

## 2. 前端缺失的关键后端功能（按优先级排序）

### P0 — 关键业务功能缺失（用户直接可见价值高）

| 缺失功能 | 后端端点 | 用户影响 | 建议 |
|----------|----------|----------|------|
| **混合检索（全局搜索）** | `POST /users/{id}/hybrid-search` | 无法跨会话搜索知识点 | 在 /app 增加全局搜索 Tab |
| **统一查询接口** | `POST /api/v1/query` | 无法跨会话实体搜索 | 同上 |
| **知识图谱可视化** | `GET /api/v1/kg/graph` | 无法看到实体关联关系 | 增加 D3/Cytoscape 图组件 |
| **KG 语义搜索** | `POST /api/v1/kg/search` | 实体搜索能力缺失 | 全局搜索 Tab |
| **实体浏览器** | `GET /api/v1/kg/entities` | 无法查看/管理全部实体 | 增加实体管理页面 |

### P1 — 管理运维功能（影响效率）

| 缺失功能 | 后端端点 | 用户影响 | 建议 |
|----------|----------|----------|------|
| **Session 管理（归档/删除）** | `DELETE /api/v1/sessions/{id}`, `/archive`, `/unarchive` | 无法清理无用会话 | Session 列表增加操作按钮 |
| **管理员操作** | `/admin/ops/repair-stuck-session`, `/requeue-kg-deadletters`, `/run-kg-worker-once` | 运维靠 SSH 手工 | 增加 Admin 操作面板 |
| **用户 Persona 展示** | `GET /api/v1/users/{id}/persona` | 无法看到 AI 对你的画像 | 侧边栏/Profile 页 |
| **Scenarios 场景管理** | `GET /api/v1/users/{id}/scenarios` | 无法看到自动识别的场景 | Persona 页内嵌 |
| **Memory Atoms** | `GET /api/v1/users/{id}/atoms` | 无法浏览记忆原子 | Memory Tab |
| **数据导入导出** | `/users/{id}/memory/export`, `/import` | 无法迁移个人数据 | 设置页 |

### P2 — 增强功能

| 缺失功能 | 后端端点 | 用户影响 | 建议 |
|----------|----------|----------|------|
| **Proactive Prefetch 可视化** | `POST /users/{id}/proactive-prefetch` | 无法看到记忆注入效果 | 搜索页增加"toggle task prefetch" |
| **Session Chat** | `POST /api/v1/sessions/{id}/chat` | | 从前端直接与历史会话对话 |
| **Canvas 可视化** | `GET/PUT /api/v1/sessions/{id}/canvas` | | 画布展示 |
| **摘要触发器** | `POST /api/v1/sessions/{id}/summarize` | | 手动触发摘要 |
| **KG 强制抽取** | `POST /api/v1/sessions/{id}/pyramid/force-extract` | | 手动触发抽取 |
| **Project 视图** | `/api/v1/projects/{id}/search/kg`, `/summaries` | | 项目级视图 |
| **Entity 可见性切换** | `/api/v1/kg/entities/by-visibility` | | 实体管理页 |
| **continuation 管理** | `/api/v1/plugin-state/continuation/*` | Resume 功能的增强 | Resume Tab 增强 |

---

## 3. 已知 BUG 与问题列表

### 3.1 Frontend-Dashboard 问题

| 问题 | 位置 | 根因 | 建议 |
|------|------|------|------|
| sqlite SSH 错误 | Dashboard `check_plugin_state` | 前端硬编码 SSH 到 10.100.1.18 查 sqlite，该机离线 | 移除；不再依赖外部 sqlite |
| plugin SSH 错误 | Dashboard `check_continuation_state` | 同理 SSH 到本地查文件 | 移除；continuation 状态应由后端 manage |
| Dashboard 和 /app 部分功能重复 | 两套前端并存 | Dashboard (75KB) 只做模型配置和状态展示，/app (77KB) 做操作 | 合并到 /app，Dashboard 只保留极简核心指标 |

### 3.2 API 500 错误

| 端点 | 根因 | 修复方案 |
|------|------|----------|
| `GET /sessions/{id}/context-stats` | `MissingGreenlet: greenlet_spawn has not called` — 异步上下文中触发了同步 SQLAlchemy 调用 | 检查 `Session.metadata_json` 访问和 `session.message_count`/`session.total_tokens` 属性访问是否触发惰性加载 |
| `POST /api/v1/query` (偶发) | 事务状态污染 | 已修复（plainto_tsquery + pool_reset_on_return=rollback） |

### 3.3 数据/一致性

| 问题 | 详情 |
|------|------|
| Patient 的 entity 数量波动 | KG 去重后自动抽取可能新增实体，101K→102K 是正常的 |
| 中文 FTS 效果差 | `pg_trgm` trigram 索引可处理中文（按字符 n-gram），已在 knowledge_graph.py 修复 |
| hardcoded user "jimwong" | 所有 API 端点 `/{user_id}` 由前端写死，无法多用户 |

---

## 4. 前端 🫴 后端功能对照表

```
符号: ✅ = 已实现 | ⚠️ = 部分实现 | ❌ = 缺失

Administrator Layer
├── ✅ Model Config     (POST /api/v1/admin/config/model) — Dashboard 有
├── ✅ Dashboard        (GET  /api/v1/admin/dashboard)   — /app Overview 响
├── ⚠️  Health/Alerts   (健康+告警) — Dashboard 有，/app 略有重复
├── ❌ Run KG Worker    (POST /admin/ops/run-kg-worker-once) — 前端未暴露
├── ❌ Requeue Dead     (POST /admin/ops/requeue-kg-deadletters) — 前端未暴露
├── ❌ Repair Session   (POST /admin/ops/repair-stuck-session) — 完全缺失
└── ❌ Event Stats      (GET  /admin/raw-events/stats) — 完全缺失

Search & Retrieval Layer
├── ❌ Global Hybrid    (POST   /users/{id}/hybrid-search) — **核心搜索功能缺失**
├── ❌ Global Query     (POST   /api/v1/query) — 跨会话实体搜索未暴露
├── ✅ Session Search  (POST   /sessions/{id}/search) — /app Search Tab ✅
├── ❌ KG Search       (POST   /api/v1/kg/search) — 知识搜索缺失
├── ❌ Proact Prefetch (POST   /users/{id}/proactive-prefetch) — 未暴露

Knowledge Graph Layer
├── ❌ Graph View      (GET /api/v1/kg/graph) — **核心可视化缺失**
├── ❌ Entity Browser  (GET /api/v1/kg/entities) — 实体列表未实现
├── ❌ Relation Browser(GET /api/v1/kg/relations) — 关系列表未实现
├── ❌ Entity Visibility (by-visibility) — 可见性管理缺失
├── ⚠️  KG Self-Check   (Partial) — Atomic status only
└── ❌ Entity Entities(by sessions) — 反查会话

Session Management Layer
├── ⚠️  Session List   (GET /sessions/recent) — /app Sessions Tab 有
├── ⚠️  Session Detail (GET /sessions/{id}, messages) — /app Session Detail 有
├── ⚠️  Search in Session — 有
├── ❌ Archive / Unarchive — 缺失（无法归档清理）
├── ❌ Delete Session — 缺失（无法删除无用会话）
├── ❌ Export Session  (/sessions/{id}/export) — 缺失
├── ❌ Import Memory   (/users/{id}/memory/import) — 缺失
├── ⚠️  Resume/Continuation (/sessions/{id}/context-stats) — 有 Tab 但 500 报错
├── ❌ Chat w/ Session (/sessions/{id}/chat) — 缺失
├── ❌ Gen Summary      (/sessions/{id}/summarize) — 缺失
├── ❌ Context Stats    (context-stats 500 修复后可用)
└── ❌ Force Extract    (/sessions/{id}/pyramid/force-extract) — 缺失

User Memory Layer
├── ❌ Persona View    (GET /users/{id}/persona) — **画像无法看到**
├── ❌ Scenario List   (GET /users/{id}/scenarios) — **场景无法看到**
├── ❌ Atoms Browser   (GET /users/{id}/atoms) — **记忆原子无法浏览**
├── ❌ Export All Data (/users/{id}/memory/export) — 缺失
└── ❌ Import Data     (/users/{id}/memory/import) — 缺失

Plugin/Integrity Layer
├── ⚠️  Cont. Latest   (Plugin probe only)
├── ❌ Cont. CRUD      (/plugin-state/continuation) — 缺失
├── ❌ Message State   (/plugin-state/message-state) — 缺失
└── ❌ Session Map    (/plugin-state/session-map) — 缺失

Project Layer
├── ❌ Project KG      (/projects/{id}/search/kg) — 缺失
└── ❌ Project Summary (/projects/{id}/search/summaries) — 缺失
```

---

## 5. 前端人机工程与界面优化建议

### 5.1 信息架构重组

当前问题：
- 两个 HTML 文件 (Dashboard `/` 和 `/app`) 各 75-77KB，功能部分重叠
- Sidebar 导航在 /app 有 6 项但实际内容不均衡
- Dashboard 的"模型配置"是低频操作，放在首页浪费首屏空间

建议重组为单页 Dashboard `/`：

```
┌─────────────────────────────────────────────────────────┐
│  [Session Memory]          🔍 Global Search    [User ▾] │ ← 顶部工具栏
├──────────┬──────────────────────────────────────────────┤
│ Sidebar  │  Main Content (Tab-based)                    │
│          │                                              │
│ ► Overview│  [Overview] [Search] [Memory] [KG] [Sessions]│
│   Health  │  [Admin]                                     │
│   Search  │                                              │
│   Memory  │  ← Tab 切换而非页面跳转                       │
│   KG      │                                              │
│   Sessions│                                              │
│   Admin   │                                              │
│          │                                              │
└──────────┴──────────────────────────────────────────────┘
```

### 5.2 内容优先级（首屏 Should-Have）

屏1 (不经 tab 切换用户能见到的)：
1. **Global Hybrid Search Bar** — 跨+单会话检索 (Ctrl+K 聚焦)
2. **Recent Sessions** (最近 5 条)
3. **KG Quicks** (queue ready/deadletter/continuation 状态)
4. **Health OK/Warning**

需点击进入的：
- Model Config (低频)
- Admin Ops (低频维护)
- Persona/Scenarios (次低频)

### 5.3 交互改进

| 现状 | 问题 | 改进 |
|------|------|------|
| Search Tab 无结果反馈 | 搜索后无 loading 状态 | + Skeleton/Spinner |
| Sessions Tab 只读 | 点无任何操作 | + Archive/Delete/Batch 按钮 |
| Session Detail 堆砌 | 全字段展示 | 折叠 Details，默认折叠长消息 |
| Memory Tab 空有 nav 无内容 | | 展示 Persona + Scenarios + Atoms |
| 缺少 Keyboard 快捷 | power user 效率低 | Ctrl+K 聚焦搜索，Del 删除 |
| Dashboard 的 SSH 报错未经处理 | 每次刷新红框 | 移除该检测 — 应由后端提供 unified metrics |

### 5.4 视觉/UX 一致性

| 现状 | 改进 |
|------|------|
| 两套 HTML 独立 CSS class (Tailwind + 内联 style) | 统一 Tailwind 配置 |
| Dashboard 用 `status-ok/warning/critical/unknown` class | 统一配色 token |
| /app 用 `info-card` 另套 class | 统一组件 |
| 500 错误无友好提示 | 前端应 catch 并提示 "接口异常" |
| 无 dark mode | 支持 dark theme (CSS variables) |

---

## 6. 建议修复优先级路线图

### Phase 1: 核心搜索体验 (1-2 days)
- [ ] 新增 Global Search Tab → 调用 `/users/{id}/hybrid-search` + `/api/v1/query`
- [ ] 新增 KG Search Tab → 调用 `/api/v1/kg/search`
- [ ] /app Header 增加 Global Search Bar (Ctrl+K)

### Phase 2: KG 可视化 (1 day)
- [ ] 新增 Knowledge Graph Tab → 调用 `/api/v1/kg/graph`
- [ ] 用 D3/Cytoscape 渲染图，支持 click → entity detail
- [ ] Entity Browser 子 Tab → `/api/v1/kg/entities` with pagination
- [ ] Visibility toggle (private/public)

### Phase 3: User Memory Layer (1 day)
- [ ] Memory Tab 丰富化: Persona / Scenarios / Atoms 三个子节
- [ ] Persona 卡片 (profile_md 渲染)
- [ ] Scenarios 列表 + 关联会话数
- [ ] Atoms 时间线 (按时间倒序分页)

### Phase 4: Session 管理 (0.5 day)
- [ ] Sessions Tab 增加操作列 (Archive / Delete / Export)
- [ ] Bulk 多选 + Batch Archive
- [ ] Summary manual trigger button per session

### Phase 5: Admin + 修复 (1 day)
- [ ] 修复 context-stats 500 (MissingGreenlet)
- [ ] 新增 Admin Tab (Trigger KG Worker / Requeue Dead / Repair Session)
- [ ] 移除 Dashboard SSH 检查 (用 admin/metrics 替代)
- [ ] 统一 Dashboard + /app 为单页

### Phase 6: 打磨 (0.5 day)
- [ ] Keyboard shortcuts
- [ ] Loading/Error 状态统一
- [ ] Dark mode
- [ ] i18n (中/英文)

---

## 7. 总结

| 维度 | 评估 |
|------|------|
| 后端 API 完整度 | ⭐⭐⭐⭐ 49 端点，覆盖全生命周期 |
| 前端覆盖率 | ⭐⭐ 15/59 (25%) — 大量后端能力前端未暴露 |
| 前端 UX 一致性 | ⭐⭐ 两套 HTML 重叠，部分提示不统一 |
| API 稳定性 | ⭐⭐⭐ context-stats 仍 500，偶发事务污染 |
| 可运维性 | ⭐⭐ 管理员操作需 SSH 手工，缺 Admin UI |

**核心结论：后端功能丰富，但前端只实现了 25% 的能力。最大的缺口是：**

1. **全局混合搜索** (影响日常高频使用)
2. **KG 可视化** (核心价值无法看到)
3. **Session 管理操作** (无法归档/删除)
4. **Persona/Atoms/Scenarios** (AI 对用户的理解无法可视化)

修复 ROI 最高：先实现"全局搜索 + KG 图"，投入 1-2 天可让系统从"能用"升级为"好用"。
