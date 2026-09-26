# 校园陶艺烧制批次管控

面向校园陶艺课程的服务端管控平台：课程负责人配置年级适用的工艺版本与安全前置条件，教师记录作品的材料批号、工序签认与保管位置，系统只允许满足干燥时间、釉料兼容与教师复核的作品进入容量受限的烧制批次；窑炉取消或质检失败时保留原批次证据并生成返工、报废或重新排批决定。公开展示须经监护人授权，且公开信息不暴露未成年人身份。

## 角色

| 角色 | 职责 |
| --- | --- |
| `course_admin` | 课程负责人：工艺版本、烧制批次、批次关闭与处置 |
| `teacher` | 教师：作品登记、工序签认（可代签）、复核、保管位置 |
| `registrar` | 教务：学生转班 |
| `guardian` | 监护人：作品展示授权 / 撤回 |
| `inheritor` | 非遗传承人：只读查阅与导出 |

## 运行

```bash
python3 -m src.pottery.api          # 监听 127.0.0.1:8080，数据落盘 pottery.db
```

所有内部接口要求请求头 `X-User-Id`；`/api/public/*` 无需认证且输出已匿名化。

## 主要接口

| 方法与路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /api/craft-versions` | course_admin | 新建工艺版本（年级、干燥小时、允许陶土、釉料互斥表、安全前置条件） |
| `GET /api/craft-versions` | 教职工 | 列出工艺版本，可按 `?grade=` 过滤 |
| `POST /api/works` | teacher | 登记作品（材料批号、陶土、釉料、保管位置、安全确认）；`device_id`+`client_record_id` 幂等去重 |
| `POST /api/works/{id}/steps` | teacher | 工序签认（揉泥→拉坯→修坯→施釉，不可越级；非本班教师须代签对象与原因） |
| `POST /api/works/{id}/review` | teacher | 教师复核（须独立于施釉签认教师） |
| `POST /api/works/{id}/storage` | teacher | 变更保管位置（留痕） |
| `POST /api/works/{id}/consent` | guardian | 授权 / 撤回展示，撤回立即生效 |
| `GET /api/works/{id}/history` | 教职工 | 作品沿革（材料、经手人、批次、处置、转班）；`?format=csv` 导出 |
| `POST /api/batches` | course_admin | 新建批次（编号 `KILN-YYYY-NNN`，容量受限） |
| `POST /api/batches/{id}/items` | admin/teacher | 排批（事务内校验容量、干燥时间、釉料兼容、复核状态） |
| `POST /api/batches/{id}/finish` | course_admin | 关闭批次：`completed`（逐件质检）/ `cancelled` / `failed`，异常逐件生成返工、报废或重新排批决定 |
| `GET /api/batches/{id}/manifest` | 教职工 | 窑次清单；`?format=csv` 导出 |
| `POST /api/students/{id}/transfer` | registrar | 学生转班（签认权限跟随当前班级） |
| `GET /api/public/exhibits` | 公开 | 已授权展品列表（匿名化） |
| `GET /api/public/works/{id}` | 公开 | 展品卡片：材料批号、经手教师、有效工艺版本，不含学生身份 |

## 关键规则

- **工序不可越级**：施釉前必须依次完成揉泥、拉坯、修坯；烧制只能由批次完成。
- **排批门槛**：全部工序签认 + 独立教师复核通过 + 施釉后干燥满工艺版本规定小时数 + 釉料无互斥 + 不在其他待烧批次中。
- **容量并发安全**：排批在 `BEGIN IMMEDIATE` 事务内检查并占位，并发下不会超容量。
- **设备重复上传**：同一 `device_id` + `client_record_id` 重复提交返回原记录（`deduplicated: true`），不产生重复数据。
- **批次证据保留**：取消或失败的批次及其作品结果、处置决定全部留档，作品沿革可追溯；返工作品须重新复核后才能再排批，报废为终态。
- **未成年人保护**：公开视图只含作品编号、年级、材料与经手教师，不出现学生姓名、学号与班级。

## 验证

```bash
python3 -m unittest discover -s tests -v   # 56 项测试
python3 -m compileall -q src               # 构建检查
```

测试覆盖：并发排批不超容量、工序越级拒绝、授权撤回即时生效、失败批次恢复（返工/报废/重新排批）、设备去重、教师代签、转班权限跟随、持久化重开库后数据完整。
