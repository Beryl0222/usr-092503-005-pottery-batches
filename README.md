# 校园陶艺烧制批次管控

面向教务、教师、非遗传承人与监护人的服务端管控平台：管理工艺版本与安全前置、
记录作品全工序签认与材料批号，按干燥时间、釉料兼容和教师复核控制排批，
窑炉取消或质检失败时保留证据并逐件处置，公开视图不暴露未成年人身份。

## 运行与测试

```bash
# 运行测试（领域规则 34 项 + HTTP 端到端 16 项，共 50 项）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src

# 启动服务（默认 127.0.0.1:8080，SQLite 文件持久化）
python3 -m src.pottery.api --db pottery.db --port 8080
```

## 角色

| 角色 | 权限 |
| --- | --- |
| coordinator 课程负责人 | 账户/学籍/监护关系、转班、工艺版本、材料目录、时钟控制、质检与处置 |
| master 非遗传承人 | 工艺版本/釉料兼容/材料目录、质检、失败处置 |
| teacher 教师 | 作品建档、工序签认（含代签）、复核（四眼原则）、排批、点火/烧成、取消窑次 |
| guardian 监护人 | 对监护学生作品授予/撤回展示授权 |
| 匿名访客 | 只读公开视图 |

## 核心规则

- **工序不能越级**：揉泥→拉坯→修坯→施釉→烧制；烧制由窑次流程自动签认。
- **工艺版本钉选**：作品绑定建档时该年级的有效版本；新版本发布后旧作品仍按旧版本执行。
- **安全前置**：建档时必须逐条确认该版本全部安全前置条件。
- **材料管控**：工序必须使用对应材料种类（泥/釉）与有效批号；按年级设置材料禁忌；停用批号禁用。
- **釉料兼容**：釉料族须与工艺要求一致，或已登记跨族兼容放行。
- **干燥时间**：施釉距修坯完成不少于工艺版本规定小时数（可控时钟校验）。
- **教师复核**：施釉后进入待复核，复核人不得是施釉签认人，通过后方可排批。
- **容量并发**：排批在 `BEGIN IMMEDIATE` 事务内计数，并发请求下窑次绝不超容量；
  同一作品不能同时在两个在制窑次。
- **失败可恢复**：质检失败/窑炉取消保留窑次、成员、签认全部证据，
  逐件决定返工（只能从修坯或施釉重做）、报废（终态）、重新排批；
  作废签认先写入事件日志再删除。
- **展示授权**：监护人授予后成品才进入公开视图；撤回立即生效。
  公开视图仅含公开编号、工艺版本、窑次、材料来源与经手教师姓名，
  **不含**学生姓名、学籍号、年级班级等任何未成年人身份字段。
- **转班**：学籍即时变更，历史作品保留原班级快照，新作品使用新班级。
- **设备防重**：`X-Device-Id` + `X-Content-Hash` 相同的建档请求拒绝；
  写接口还支持 `Idempotency-Key` 请求级重放。

## HTTP 接口（均为 JSON，令牌用 `Authorization: Bearer <token>`）

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `GET /api/health` | 匿名 | 健康检查与当前时钟 |
| `GET /api/public/works` | 匿名 | 公开展品（白名单字段） |
| `POST /api/admin/clock` | coordinator | `freeze`/`advance`/`resume` 可控时钟 |
| `POST /api/users` `/api/students` | coordinator | 账户、学籍 |
| `POST /api/students/{id}/transfer` | coordinator | 转班 |
| `POST /api/guardian-links` | coordinator | 建立监护关系 |
| `POST /api/craft-versions` | coordinator/master | 发布工艺新版本 |
| `POST /api/glaze-compat` | coordinator/master | 釉料跨族兼容放行 |
| `POST /api/materials` `.../deactivate` | coordinator/master | 材料批号登记/停用 |
| `POST /api/works` | teacher | 作品建档（设备/幂等头） |
| `GET /api/works` `/api/works/{id}` | 教职工 | 作品列表/详情 |
| `POST /api/works/{id}/steps` | teacher | 工序签认（`signed_for` 代签） |
| `POST /api/works/{id}/review` | teacher | 复核（异于施釉人） |
| `POST /api/works/{id}/storage` | teacher | 保管位置变更 |
| `POST /api/works/{id}/consent/grant|withdraw` | guardian | 展示授权 |
| `GET /api/works/{id}/history?format=json|csv` | 教职工 | **作品沿革导出**（含作废证据） |
| `POST /api/kilns` `GET /api/kilns` | teacher / 教职工 | 窑次创建/列表 |
| `GET /api/kilns/{code}/manifest?format=csv|json` | 教职工 | **窑次清单导出** |
| `POST /api/kilns/{code}/works` `DELETE .../works/{wid}` | teacher | 排批/移出 |
| `POST /api/kilns/{code}/start|done|qc|cancel` | teacher / master / teacher | 点火、烧成、质检、取消 |
| `POST /api/kilns/{code}/dispositions` | 教职工 | 返工/报废/重新排批决定 |

错误响应统一为 `{"error": {"code": ..., "message": ...}}`，
语义状态码：401 未认证、403 角色不足、404 不存在、409 规则冲突（含超容量/越级/重复上传）、422 校验失败。

## 代码结构

```
src/pottery/
  contracts.py   角色/状态/工序顺序等领域契约
  clock.py       可控时钟（冻结、快进、恢复）
  errors.py      领域错误与 HTTP 状态映射
  storage.py     SQLite schema、BEGIN IMMEDIATE 事务
  services.py    全部业务规则（事件日志为沿革事实来源）
  api.py         角色受限 HTTP 接口与 CSV/JSON 导出
tests/
  _fixtures.py   教学世界夹具与出窑流水线
  test_contracts.py / test_domain.py / test_http.py
```
