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

合作意向提交后不再自动进入洽谈，须由投资、法务、园区运营三类角色并行审阅：

- `PUT /api/v1/workflow/intents/{id}/review-config` 配置必审角色（默认三角色全审）
- `POST /api/v1/workflow/intents/{id}/reviews/start` 开启审批（快照本轮必审角色）
- `POST /api/v1/workflow/intents/{id}/reviews/decisions` 提交角色意见（通过 / 退回补件 / 拒绝）
- `POST /api/v1/workflow/intents/{id}/reviews/resubmit` 补件后重开新一轮（历史意见保留）
- `GET /api/v1/workflow/intents/reviews/todo?role=法务` 按角色查询待办
- `GET /api/v1/workflow/intents/{id}/reviews` 还原完整审批轨迹

必审角色全部通过后意向才进入洽谈、项目同步由「招商中」转「洽谈中」；任一角色退回补件或整单拒绝都会保留已有意见并阻止推进。配置变更只对之后开启的轮次生效。重复审批与并发更新返回 409 并携带 `code/message/state` 说明冲突原因与当前轮次状态。

