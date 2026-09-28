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
- `GET /api/records/{id}`：记录详情（含当前两名还款人与责任比例）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/borrower-changes`：该贷款的共同借款人变更单列表。
- `GET /api/borrower-changes/{id}`：变更单详情（变更前后还款人、原因、发起人/复核人）。
- `GET /api/collection`：催收名单，只反映已确认生效的还款人；已结清贷款不返回，在途变更以`pending_change_id`提示。
- `GET /api/stats`：状态统计，`borrower_changes`字段给出变更单各状态数量。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`，`data.borrowers`必须为两名还款人。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/borrower-changes`：客服发起共同借款人变更，请求体为`{"data":{"after_borrowers":[...],"reason":"..."}}`。
- `POST /api/borrower-changes/{id}/confirm`：另一复核人确认，`{"data":{"review_note":"..."}}`，确认后新名单立即生效。
- `POST /api/borrower-changes/{id}/reject`：复核人驳回，原名单保持不变。
- `POST /api/borrower-changes/{id}/cancel`：发起人（或admin）撤销待确认变更单。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 共同借款人变更规则

- 每笔贷款记录两名还款人（`person_id`、`name`、`share_pct`），两人不能为同一人，各自比例大于0，合计必须为100%。
- 变更由客服角色`intake_officer`发起；复核角色为`underwriter`。发起人不能复核自己的变更单（按用户身份拦截，与角色无关）。
- 每笔未结清贷款至多保留一张`pending`变更单。发起变更不修改贷款记录本身：确认前催收名单、业务动作权限均不受影响。
- 复核确认在一个事务内更新还款人名单、贷款版本+1并写入审计；驳回不改动贷款。撤销/驳回后可重新发起。
- 新增`settle`动作（`servicer`执行，可在`active/cured/defaulted`后执行）将贷款置为`settled`；已结清贷款不能发起或确认变更，也不进入催收名单。
- 变更前后责任人、原因、发起与复核信息可通过变更单详情和`GET /api/records/{id}/audit`时间线查询。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及共同借款人变更的双人复核、确认前后催收名单、结清拦截、时间线与统计。
