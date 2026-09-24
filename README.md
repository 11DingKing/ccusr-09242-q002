# 水果深加工招商台账后端服务

记录合作主体、园区、项目、洽谈、立项、里程碑和投产后的产能兑现情况，为招商团队提供可追溯的业务接口。

## 运行约定

服务端代码位于 `app` 目录，默认使用项目目录中的 SQLite 文件保存业务数据。环境变量可以覆盖数据库位置和接口前缀，临时配置不应提交到仓库。

## 测试

在项目根目录执行：

```bash
python3 -m unittest discover -s tests -v
```

## 编译检查

在项目根目录执行：

```bash
python3 -m compileall -q app tests
```

## 启动服务

准备依赖后可执行 `uvicorn app.main:app --host 127.0.0.1 --port 8000`，根路径返回服务状态，接口文档位于 `/docs`。

## 合作意向分阶段审批

合作意向提交后不再直接进入洽谈，须由配置的必审角色（投资 / 法务 / 园区运营）并行审阅：

- `PUT /api/v1/approvals/config`：版本化配置必审角色；旧版本保留，在途与已结束轮次锁定开轮时的快照，配置变更不改写历史。
- `POST /api/v1/workflow/intents/{id}/reviews`：发起第 1 轮审批；补件退回后凭 `resubmit_comment` 发起重审。
- `POST /api/v1/workflow/intents/{id}/reviews/roles/{role}/opinions`：角色提交独立意见（`通过` / `补件退回` / `拒绝`，含附件摘要）。角色支持中文名（`投资`）或常量名（`INVESTMENT`）。
- `GET /api/v1/approvals/todos?role=投资`：按角色查询待办。
- `GET /api/v1/workflow/intents/{id}/reviews`：还原完整轨迹（各轮快照、阶段状态、意见、附件摘要、事件流）。

全部必审角色通过后意向才进入「洽谈中」并联动项目状态；任一角色补件退回即关闭本轮（意见全保留、阻止登记洽谈与后续意见），拒绝则整单终止。重复与并发写入返回 409，响应体 `detail.code` 为机器可读冲突码，`detail.context` 回带轮次真实状态。

