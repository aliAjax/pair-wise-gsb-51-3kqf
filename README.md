# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，含`payload.borrowers`两名还款人及当前待确认变更单`pending_borrower_change`。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/borrower-changes`：该贷款的共同借款人变更单列表。
- `GET /api/collections`：催收名单（active/defaulted），按当前生效还款人及责任份额列出催收对象与责任金额。
- `GET /api/stats`：状态统计、变更单状态统计和各还款人责任份额汇总。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.borrowers`必须是两名还款人`[{"person_id","name","share"}]`，`share`为0-100的比例，两人合计必须等于100。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`；新增`settle`动作（active/cured→settled结清）。
- `POST /api/records/{id}/borrower-changes`：客服（servicer）发起共同借款人变更，请求体为`{"expected_version":4,"data":{"reason":"离异，共同还款人更换","borrowers":[...]}}`。
- `POST /api/borrower-changes/{changeId}/confirm`：另一名客服复核确认，请求体为`{"data":{"review_note":"材料齐全"}}`；`reject`为驳回。

### 共同借款人变更规则

- 每笔贷款固定记录两名还款人及责任比例，合计达到100%，允许一方承担100%、另一方0%（如离异后一方退出）。
- 贷款未结清时，每笔贷款只允许保留一张`pending`变更单；贷款结清（settled）后禁止再变更，有待确认变更单时也不能结清。
- 变更单由客服发起，必须由**另一名**客服复核确认或驳回，发起人不能自己批（admin同样受限）。
- 确认前，记录责任人、催收名单与催收动作权限均不变（变更单中的新名单仅作为`pending_borrower_change`预览）；确认后记录版本号+1，才按新名单和责任份额生效。
- 发起（`borrower_change_requested`）、确认（`borrower_change_confirmed`）、驳回（`borrower_change_rejected`）均写入审计时间线，包含变更前后两名还款人、责任份额和变更原因；变更单列表保留全部历史单据。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突和共同借款人变更（双人100%校验、单一待确认变更单、发起人不能自批、确认前后催收名单切换、结清限制、审计与统计）。
