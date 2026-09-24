"""分阶段审批能力测试。

覆盖：
- 三角色并行全部通过后才进入洽谈、项目状态联动；
- 补件退回保留已完成意见、补件后重审；
- 必审角色缺席阻止推进、非必审角色被拒；
- 配置版本化：在途/已结束轮次锁定快照，配置变更不改写历史；
- 重复审批返回可解释冲突；
- 同角色/不同角色并发写入的串行化结果；
- 服务重启（重新建引擎指向同一数据库文件）后的状态恢复。
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as dbmod
from app.database import Base, install_sqlite_write_guards
from app.main import app
from app import models
from app.enums import (
    Region,
    ParkType,
    ProjectStatus,
    IntentStatus,
    ReviewRole,
    ReviewDecision,
    ReviewRoundStatus,
)

API = "/api/v1"


class ApprovalTestBase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db", prefix="review_test_")
        os.close(fd)
        self._start_engine()
        Base.metadata.drop_all(bind=self.engine)
        Base.metadata.create_all(bind=self.engine)
        self._seed_base_data()
        self.client = TestClient(app)

    def _start_engine(self):
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            connect_args={"check_same_thread": False},
        )
        install_sqlite_write_guards(self.engine)
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        # 让 get_db 与 run_immediate 都落到本用例的临时库
        self._orig_engine = dbmod.engine
        self._orig_session = dbmod.SessionLocal
        dbmod.engine = self.engine
        dbmod.SessionLocal = self.SessionLocal

    def _stop_engine(self):
        dbmod.engine = self._orig_engine
        dbmod.SessionLocal = self._orig_session
        self.engine.dispose()

    def tearDown(self):
        self.client.close()
        self._stop_engine()
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)

    def _seed_base_data(self):
        db = self.SessionLocal()
        park = models.IndustrialPark(
            name="测试园区",
            park_type=ParkType.FREE_TRADE,
            city="南宁市",
        )
        initiator = models.Entity(
            name="发起主体公司",
            region=Region.GUANGXI,
            country_or_province="广西",
            contact_person="张三",
            contact_phone="13800000001",
        )
        submitter = models.Entity(
            name="提交主体公司",
            region=Region.ASSOCIATION_SOUTH_EAST_ASIAN_NATIONS,
            country_or_province="泰国",
            contact_person="Somchai",
            contact_phone="13800000002",
        )
        db.add_all([park, initiator, submitter])
        db.flush()
        self.park_id = park.id
        self.initiator_id = initiator.id
        self.submitter_id = submitter.id
        project = models.Project(
            name="测试水果深加工项目",
            investment_direction="果汁加工",
            planned_investment_10k=10000.0,
            park_id=park.id,
            initiator_id=initiator.id,
            status=ProjectStatus.ATTRACTING_INVESTMENT,
        )
        db.add(project)
        db.commit()
        self.project_id = project.id
        db.close()

    # -- 业务辅助 -----------------------------------------------------------

    def create_project(self, name=None):
        db = self.SessionLocal()
        project = models.Project(
            name=name or f"项目-{datetime.utcnow().timestamp()}",
            investment_direction="水果加工",
            planned_investment_10k=5000.0,
            park_id=self.park_id,
            initiator_id=self.initiator_id,
            status=ProjectStatus.ATTRACTING_INVESTMENT,
        )
        db.add(project)
        db.commit()
        pid = project.id
        db.close()
        return pid

    def create_intent(self, content="意向内容", project_id=None):
        resp = self.client.post(
            f"{API}/workflow/intents",
            json={
                "project_id": project_id or self.project_id,
                "submitter_id": self.submitter_id,
                "cooperation_content": content,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def put_config(self, roles, remark=None, by="管理员"):
        resp = self.client.put(
            f"{API}/approvals/config",
            json={
                "required_roles": roles,
                "change_remark": remark,
                "created_by": by,
            },
        )
        return resp

    def setup_default_config(self):
        resp = self.put_config(
            ["INVESTMENT", "LEGAL", "PARK_OPERATIONS"], remark="初始三角色必审"
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        return resp.json()["version"]

    def open_round(self, intent_id, **payload):
        body = {}
        if payload:
            body.update(payload)
        return self.client.post(
            f"{API}/workflow/intents/{intent_id}/reviews", json=body
        )

    def submit(self, intent_id, role, decision="通过", **kw):
        body = {"decision": decision}
        body.update(kw)
        return self.client.post(
            f"{API}/workflow/intents/{intent_id}/reviews/roles/{role}/opinions",
            json=body,
        )

    def approve_all(self, intent_id):
        for role in ("INVESTMENT", "LEGAL", "PARK_OPERATIONS"):
            resp = self.submit(intent_id, role, comment=f"{role}同意")
            self.assertEqual(resp.status_code, 201, resp.text)
        return resp

    def trace(self, intent_id):
        resp = self.client.get(f"{API}/workflow/intents/{intent_id}/reviews")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def todos(self, role):
        resp = self.client.get(f"{API}/approvals/todos", params={"role": role})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def get_intent(self, intent_id):
        resp = self.client.get(f"{API}/workflow/intents/{intent_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def get_project(self, project_id):
        resp = self.client.get(f"{API}/projects/{project_id}")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def add_negotiation(self, intent_id):
        return self.client.post(
            f"{API}/workflow/intents/{intent_id}/negotiations",
            json={
                "round": 1,
                "title": "首轮洽谈",
                "held_at": datetime.utcnow().isoformat(),
                "key_topics": "合作条款",
            },
        )


class HappyPathTests(ApprovalTestBase):
    def test_submit_intent_does_not_move_project_without_review(self):
        self.setup_default_config()
        intent_id = self.create_intent()
        # 提交意向后项目仍保持招商中，意向保持已提交
        self.assertEqual(
            self.get_project(self.project_id)["status"], "招商中"
        )
        self.assertEqual(self.get_intent(intent_id)["status"], "已提交")
        # 未审批不可登记洽谈
        resp = self.add_negotiation(intent_id)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()["detail"]["code"], "REVIEW_NEGOTIATION_BLOCKED_NO_REVIEW"
        )

    def test_parallel_roles_all_approve_then_negotiation(self):
        self.setup_default_config()
        intent_id = self.create_intent()
        resp = self.open_round(intent_id, operator="招商主管")
        self.assertEqual(resp.status_code, 201, resp.text)
        opened = resp.json()
        self.assertEqual(opened["round_no"], 1)
        self.assertEqual(opened["config_version"], 1)
        self.assertEqual(
            opened["required_roles"], ["投资", "法务", "园区运营"]
        )
        self.assertEqual(self.get_intent(intent_id)["status"], "评审中")

        # 三个角色在待办中，顺序无关地并行出现
        for role, name in (
            ("INVESTMENT", "投资"),
            ("LEGAL", "法务"),
            ("PARK_OPERATIONS", "园区运营"),
        ):
            todo = self.todos(role)
            self.assertEqual(len(todo), 1)
            self.assertEqual(todo[0]["intent_id"], intent_id)

        r1 = self.submit(intent_id, "INVESTMENT", comment="投资回报可行",
                         reviewer="李投资")
        self.assertEqual(r1.status_code, 201, r1.text)
        body1 = r1.json()
        self.assertEqual(body1["round_status"], "待审批")
        self.assertEqual(body1["pending_roles"], ["法务", "园区运营"])

        r2 = self.submit(intent_id, "LEGAL", comment="合同无重大风险",
                         reviewer="王法务")
        self.assertEqual(r2.status_code, 201, r2.text)
        self.assertEqual(r2.json()["pending_roles"], ["园区运营"])

        # 缺席任一角色都不可推进洽谈
        blocked = self.add_negotiation(intent_id)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(
            blocked.json()["detail"]["code"],
            "REVIEW_NEGOTIATION_BLOCKED_PENDING",
        )
        self.assertIn("园区运营", blocked.json()["detail"]["message"])

        r3 = self.submit(
            intent_id,
            "PARK_OPERATIONS",
            comment="用地与配套可落实",
            attachment_summary="用地预审意见扫描件1份",
            attachments=[
                {
                    "file_name": "用地预审.pdf",
                    "file_size_bytes": 204800,
                    "digest": "sha256:abc",
                    "summary": "园区B区320亩用地符合规划",
                }
            ],
            reviewer="陈运营",
        )
        self.assertEqual(r3.status_code, 201, r3.text)
        body3 = r3.json()
        self.assertEqual(body3["round_status"], "全部通过")
        self.assertEqual(body3["intent_status"], "洽谈中")

        # 意向与项目双双进入洽谈
        self.assertEqual(self.get_intent(intent_id)["status"], "洽谈中")
        self.assertEqual(
            self.get_project(self.project_id)["status"], "洽谈中"
        )
        # 待办全部清空
        for role in ("INVESTMENT", "LEGAL", "PARK_OPERATIONS"):
            self.assertEqual(self.todos(role), [])

        # 可以登记洽谈
        neg = self.add_negotiation(intent_id)
        self.assertEqual(neg.status_code, 200, neg.text)

        # 轨迹可完整还原
        trace = self.trace(intent_id)
        self.assertEqual(len(trace["rounds"]), 1)
        rnd = trace["rounds"][0]
        self.assertEqual(rnd["status"], "全部通过")
        self.assertEqual(rnd["config_version"], 1)
        self.assertEqual(
            [s["status"] for s in rnd["stages"]],
            ["已通过", "已通过", "已通过"],
        )
        ops_opinion = [
            o for o in rnd["opinions"] if o["role"] == "园区运营"
        ][0]
        self.assertEqual(ops_opinion["attachment_summary"], "用地预审意见扫描件1份")
        self.assertEqual(ops_opinion["attachments"][0]["file_name"], "用地预审.pdf")
        self.assertEqual(
            ops_opinion["attachments"][0]["summary"], "园区B区320亩用地符合规划"
        )
        event_types = [e["event_type"] for e in trace["events"]]
        self.assertEqual(
            event_types,
            [
                "round_opened",
                "opinion_submitted",
                "opinion_submitted",
                "opinion_submitted",
                "round_approved",
            ],
        )
        for opinion in rnd["opinions"]:
            self.assertIsNotNone(opinion["submitted_at"])


class ReturnAndResubmitTests(ApprovalTestBase):
    def test_return_keeps_opinions_and_resubmit_round_reviews(self):
        self.setup_default_config()
        intent_id = self.create_intent()
        self.open_round(intent_id)

        # 投资先通过，法务要求补件
        r_inv = self.submit(intent_id, "INVESTMENT", comment="数据基本可信")
        self.assertEqual(r_inv.status_code, 201)
        r_legal = self.submit(
            intent_id,
            "LEGAL",
            decision="补件退回",
            comment="缺少环评承诺函与合作方资信证明",
        )
        self.assertEqual(r_legal.status_code, 201, r_legal.text)
        body = r_legal.json()
        self.assertEqual(body["round_status"], "已退回")

        # 轮次关闭，第三个角色再提交被阻止
        r_ops = self.submit(intent_id, "PARK_OPERATIONS", comment="迟到的意见")
        self.assertEqual(r_ops.status_code, 409)
        self.assertEqual(
            r_ops.json()["detail"]["code"], "REVIEW_ROUND_RETURNED"
        )

        # 已完成的投资意见与退回意见都保留
        trace = self.trace(intent_id)
        self.assertEqual(len(trace["rounds"][0]["opinions"]), 2)
        decisions = {
            o["role"]: o["decision"]
            for o in trace["rounds"][0]["opinions"]
        }
        self.assertEqual(decisions["投资"], "通过")
        self.assertEqual(decisions["法务"], "补件退回")
        self.assertEqual(trace["rounds"][0]["status"], "已退回")

        # 退回期间登记洽谈被阻止
        blocked = self.add_negotiation(intent_id)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(
            blocked.json()["detail"]["code"],
            "REVIEW_NEGOTIATION_BLOCKED_PENDING_RESUBMIT",
        )

        # 不填补件说明不能发起重审
        bad = self.open_round(intent_id)
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(
            bad.json()["detail"]["code"], "REVIEW_RESUBMIT_REMARK_REQUIRED"
        )

        # 补件后发起第 2 轮
        ok = self.open_round(
            intent_id,
            resubmit_comment="已补充环评承诺函及泰国合作方银行资信证明",
            operator="招商主管",
        )
        self.assertEqual(ok.status_code, 201, ok.text)
        self.assertEqual(ok.json()["round_no"], 2)

        # 第 2 轮三个角色都在待办（重新独立审阅）
        for role in ("INVESTMENT", "LEGAL", "PARK_OPERATIONS"):
            self.assertEqual(len(self.todos(role)), 1)

        self.approve_all(intent_id)
        self.assertEqual(self.get_intent(intent_id)["status"], "洽谈中")

        trace = self.trace(intent_id)
        self.assertEqual(len(trace["rounds"]), 2)
        self.assertEqual(trace["rounds"][0]["status"], "已退回")
        self.assertEqual(trace["rounds"][1]["status"], "全部通过")
        # 第 1 轮意见仍在
        self.assertEqual(len(trace["rounds"][0]["opinions"]), 2)
        self.assertEqual(len(trace["rounds"][1]["opinions"]), 3)
        self.assertEqual(
            [e["event_type"] for e in trace["events"]].count("round_opened"), 2
        )

        # 重审通过后可以洽谈
        self.assertEqual(self.add_negotiation(intent_id).status_code, 200)

    def test_reject_terminates_intent_and_keeps_history(self):
        self.setup_default_config()
        intent_id = self.create_intent()
        self.open_round(intent_id)
        self.submit(intent_id, "INVESTMENT", comment="通过")
        rej = self.submit(
            intent_id, "LEGAL", decision="拒绝", comment="合作方存在重大诉讼"
        )
        self.assertEqual(rej.status_code, 201)
        self.assertEqual(rej.json()["round_status"], "已拒绝")
        self.assertEqual(self.get_intent(intent_id)["status"], "已拒绝")

        # 不可重审
        again = self.open_round(
            intent_id, resubmit_comment="试图重启"
        )
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"]["code"], "REVIEW_INTENT_REJECTED")
        # 不可洽谈
        self.assertEqual(self.add_negotiation(intent_id).status_code, 409)
        # 意见保留
        trace = self.trace(intent_id)
        self.assertEqual(len(trace["rounds"][0]["opinions"]), 2)
        self.assertEqual(trace["rounds"][0]["close_reason"], "法务审阅拒绝整单")


class ConfigSnapshotTests(ApprovalTestBase):
    def test_role_absent_from_config_cannot_submit(self):
        # 只要投资 + 法务
        resp = self.put_config(["INVESTMENT", "LEGAL"], remark="运营不再必审")
        self.assertEqual(resp.status_code, 201, resp.text)
        intent_id = self.create_intent()
        self.open_round(intent_id)

        ops = self.submit(intent_id, "PARK_OPERATIONS", comment="非必审也要说")
        self.assertEqual(ops.status_code, 409)
        detail = ops.json()["detail"]
        self.assertEqual(detail["code"], "REVIEW_ROLE_NOT_REQUIRED")
        self.assertIn("配置第 1 版", detail["message"])
        # 待办里没有园区运营
        self.assertEqual(self.todos("PARK_OPERATIONS"), [])
        self.assertEqual(len(self.todos("LEGAL")), 1)

        self.submit(intent_id, "INVESTMENT")
        self.submit(intent_id, "LEGAL")
        self.assertEqual(self.get_intent(intent_id)["status"], "洽谈中")

    def test_config_change_does_not_rewrite_open_or_finished_rounds(self):
        self.setup_default_config()
        intent_a = self.create_intent("意向A")
        self.open_round(intent_a)
        self.submit(intent_a, "INVESTMENT", comment="A:投资通过")

        # 配置缩为两角色：生成第 2 版，在途的第 1 轮不受影响
        resp = self.put_config(["INVESTMENT", "LEGAL"], remark="园区运营改为知会")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["version"], 2)

        trace_a = self.trace(intent_a)
        self.assertEqual(
            trace_a["rounds"][0]["required_roles"],
            ["投资", "法务", "园区运营"],
        )
        self.assertEqual(trace_a["rounds"][0]["config_version"], 1)
        # 园区运营仍需在第 1 轮完成意见
        self.assertEqual(len(self.todos("PARK_OPERATIONS")), 1)
        self.submit(intent_a, "LEGAL")
        self.submit(intent_a, "PARK_OPERATIONS")
        self.assertEqual(self.get_intent(intent_a)["status"], "洽谈中")

        # 配置变更后新开的轮次锁定第 2 版快照
        project_b = self.create_project("测试水果深加工项目B")
        intent_b = self.create_intent("意向B", project_id=project_b)
        opened = self.open_round(intent_b)
        self.assertEqual(opened.json()["config_version"], 2)
        self.assertEqual(opened.json()["required_roles"], ["投资", "法务"])
        self.submit(intent_b, "INVESTMENT")
        self.submit(intent_b, "LEGAL")
        self.assertEqual(self.get_intent(intent_b)["status"], "洽谈中")

        # 已结束的意向 A 轨迹不被配置变化改写
        trace_a_after = self.trace(intent_a)
        self.assertEqual(
            trace_a_after["rounds"][0]["required_roles"],
            ["投资", "法务", "园区运营"],
        )
        self.assertEqual(trace_a_after["rounds"][0]["status"], "全部通过")

        # 历史版本都可查
        versions = self.client.get(f"{API}/approvals/config/versions").json()
        self.assertEqual([v["version"] for v in versions], [2, 1])
        self.assertEqual(versions[1]["is_active"], False)

    def test_same_config_rejected(self):
        self.setup_default_config()
        resp = self.put_config(["LEGAL", "INVESTMENT", "PARK_OPERATIONS"])
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], "REVIEW_CONFIG_UNCHANGED")


class DuplicateTests(ApprovalTestBase):
    def test_duplicate_opinion_and_round_conflict(self):
        self.setup_default_config()
        intent_id = self.create_intent()
        self.open_round(intent_id)
        first = self.submit(intent_id, "INVESTMENT", comment="第一次",
                            reviewer="李投资")
        self.assertEqual(first.status_code, 201)
        second = self.submit(intent_id, "INVESTMENT", comment="第二次")
        self.assertEqual(second.status_code, 409)
        detail = second.json()["detail"]
        self.assertEqual(detail["code"], "REVIEW_DUPLICATE")
        self.assertIn("已提交过", detail["message"])
        # 冲突结果回带当前真实轮次状态，可解释
        self.assertEqual(detail["context"]["round_no"], 1)
        self.assertEqual(detail["context"]["finished_roles"], ["投资"])
        self.assertEqual(
            detail["context"]["pending_roles"], ["法务", "园区运营"]
        )

        # 在途轮次不可重复发起
        reopen = self.open_round(intent_id)
        self.assertEqual(reopen.status_code, 409)
        self.assertEqual(
            reopen.json()["detail"]["code"], "REVIEW_ROUND_IN_PROGRESS"
        )

    def test_status_cannot_be_forced_via_generic_update(self):
        self.setup_default_config()
        intent_id = self.create_intent()
        resp = self.client.put(
            f"{API}/workflow/intents/{intent_id}",
            json={"status": "洽谈中"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(
            resp.json()["detail"]["code"], "REVIEW_STATUS_DRIVEN_BY_APPROVAL"
        )
        self.assertEqual(self.get_intent(intent_id)["status"], "已提交")


class ConcurrentWriteTests(ApprovalTestBase):
    """直接对服务层施加真实线程并发，验证 BEGIN IMMEDIATE 串行化。"""

    def _call_submit(self, intent_id, role, results, idx):
        from app.services import approvals as svc
        try:
            result = svc.submit_opinion(
                intent_id=intent_id,
                role=role,
                decision=ReviewDecision.APPROVED,
                comment=f"{role.value}并发意见",
                reviewer=f"{role.value}审阅人",
            )
            results[idx] = ("ok", result)
        except svc.ApprovalFlowError as e:
            results[idx] = ("conflict", e.code, _ctx_roles(e))
        except Exception as e:  # noqa: BLE001
            results[idx] = ("error", repr(e))

    def test_concurrent_same_role_only_one_wins(self):
        from app.services import approvals as svc
        svc.create_config_version(
            [ReviewRole.INVESTMENT, ReviewRole.LEGAL, ReviewRole.PARK_OPERATIONS]
        )
        db = self.SessionLocal()
        intent = models.CooperationIntent(
            project_id=self.project_id,
            submitter_id=self.submitter_id,
            cooperation_content="并发测试意向",
        )
        db.add(intent)
        db.commit()
        intent_id = intent.id
        db.close()
        svc.open_review_round(intent_id)

        results = [None, None]
        threads = [
            threading.Thread(
                target=self._call_submit,
                args=(intent_id, ReviewRole.INVESTMENT, results, i),
            )
            for i in range(2)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        kinds = sorted(r[0] for r in results)
        self.assertEqual(kinds, ["conflict", "ok"])
        ok = next(r for r in results if r[0] == "ok")
        conflict = next(r for r in results if r[0] == "conflict")
        self.assertEqual(ok[1]["round_no"], 1)
        self.assertEqual(conflict[1], "REVIEW_DUPLICATE")
        # 只落了一条投资意见
        db = self.SessionLocal()
        round_obj = svc.get_latest_round(db, intent_id)
        self.assertEqual(
            [o.role for o in round_obj.opinions], [ReviewRole.INVESTMENT]
        )
        db.close()

    def test_concurrent_different_roles_all_succeed(self):
        from app.services import approvals as svc
        svc.create_config_version(
            [ReviewRole.INVESTMENT, ReviewRole.LEGAL, ReviewRole.PARK_OPERATIONS]
        )
        db = self.SessionLocal()
        intent = models.CooperationIntent(
            project_id=self.project_id,
            submitter_id=self.submitter_id,
            cooperation_content="并行三角色意向",
        )
        db.add(intent)
        db.commit()
        intent_id = intent.id
        db.close()
        svc.open_review_round(intent_id)

        roles = [
            ReviewRole.INVESTMENT,
            ReviewRole.LEGAL,
            ReviewRole.PARK_OPERATIONS,
        ]
        results = [None, None, None]
        threads = [
            threading.Thread(
                target=self._call_submit, args=(intent_id, role, results, i)
            )
            for i, role in enumerate(roles)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertTrue(
            all(r[0] == "ok" for r in results),
            f"并行不同角色应全部成功，实际：{results}",
        )
        statuses = {r[1]["round_status"] for r in results}
        self.assertIn(ReviewRoundStatus.ALL_APPROVED, statuses)
        # 恰好一个请求观察到"全部通过"
        winners = [
            r for r in results if r[1]["round_status"] == ReviewRoundStatus.ALL_APPROVED
        ]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.get_intent(intent_id)["status"], "洽谈中")
        self.assertEqual(
            self.get_project(self.project_id)["status"], "洽谈中"
        )

    def test_concurrent_open_round_only_one_created(self):
        from app.services import approvals as svc
        svc.create_config_version([ReviewRole.INVESTMENT, ReviewRole.LEGAL])
        db = self.SessionLocal()
        intent = models.CooperationIntent(
            project_id=self.project_id,
            submitter_id=self.submitter_id,
            cooperation_content="并发发起意向",
        )
        db.add(intent)
        db.commit()
        intent_id = intent.id
        db.close()

        results = [None, None]

        def open_round(idx):
            try:
                no = svc.open_review_round(intent_id)
                results[idx] = ("ok", no)
            except svc.ApprovalFlowError as e:
                results[idx] = ("conflict", e.code)

        threads = [threading.Thread(target=open_round, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(sorted(r[0] for r in results), ["conflict", "ok"])
        self.assertEqual(
            next(r for r in results if r[0] == "conflict")[1],
            "REVIEW_ROUND_IN_PROGRESS",
        )
        db = self.SessionLocal()
        count = (
            db.query(models.ApprovalRound)
            .filter(models.ApprovalRound.intent_id == intent_id)
            .count()
        )
        db.close()
        self.assertEqual(count, 1)


def _ctx_roles(error):
    ctx = error.context or {}
    return ctx.get("finished_roles")


class RestartRecoveryTests(ApprovalTestBase):
    def test_state_recovers_after_engine_restart(self):
        self.setup_default_config()
        intent_id = self.create_intent("待恢复意向")
        self.open_round(intent_id)
        self.submit(intent_id, "INVESTMENT", comment="投资意见持久化")
        self.submit(
            intent_id, "LEGAL", decision="补件退回", comment="法务要求补件"
        )

        # 模拟服务重启：释放引擎，按同一数据库文件重新建引擎和会话工厂
        self.client.close()
        self._stop_engine()
        self._start_engine()
        self.client = TestClient(app)

        # 轨迹完整恢复：第 1 轮已退回，两条意见都在
        trace = self.trace(intent_id)
        self.assertEqual(trace["intent_status"], "评审中")
        self.assertEqual(len(trace["rounds"]), 1)
        rnd = trace["rounds"][0]
        self.assertEqual(rnd["status"], "已退回")
        self.assertEqual(len(rnd["opinions"]), 2)
        self.assertEqual(rnd["opinions"][0]["comment"], "投资意见持久化")
        self.assertEqual(len(trace["events"]), 4)

        # 待办状态正确：第 1 轮已关闭，所有角色待办为空
        for role in ("INVESTMENT", "LEGAL", "PARK_OPERATIONS"):
            self.assertEqual(self.todos(role), [])

        # 配置也恢复
        cfg = self.client.get(f"{API}/approvals/config").json()
        self.assertEqual(cfg["version"], 1)
        self.assertEqual(
            cfg["required_roles"], ["投资", "法务", "园区运营"]
        )

        # 重启后继续业务：补件重审，全部通过
        reopened = self.open_round(
            intent_id, resubmit_comment="服务重启后补交补充材料"
        )
        self.assertEqual(reopened.status_code, 201, reopened.text)
        self.assertEqual(reopened.json()["round_no"], 2)
        self.approve_all(intent_id)
        self.assertEqual(self.get_intent(intent_id)["status"], "洽谈中")
        self.assertEqual(
            self.get_project(self.project_id)["status"], "洽谈中"
        )

        # 再重启一次，终态保持
        self.client.close()
        self._stop_engine()
        self._start_engine()
        self.client = TestClient(app)
        self.assertEqual(self.get_intent(intent_id)["status"], "洽谈中")
        trace = self.trace(intent_id)
        self.assertEqual(len(trace["rounds"]), 2)
        self.assertEqual(trace["rounds"][1]["status"], "全部通过")


class LegacyIntentTests(ApprovalTestBase):
    def test_legacy_intent_already_in_discussion_can_negotiate(self):
        # 特性上线前已进入洽谈、没有任何审批轮次的历史意向（种子数据场景）
        db = self.SessionLocal()
        legacy = models.CooperationIntent(
            project_id=self.project_id,
            submitter_id=self.submitter_id,
            cooperation_content="历史遗留意向",
            status=IntentStatus.IN_DISCUSSION,
            reviewer="旧流程",
        )
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
        db.close()

        resp = self.add_negotiation(legacy_id)
        self.assertEqual(resp.status_code, 200, resp.text)


if __name__ == "__main__":
    unittest.main()
