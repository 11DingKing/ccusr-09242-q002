"""分阶段审批能力测试。

覆盖：
- 投资/法务/园区运营并行审批，全部通过才进入洽谈，项目状态联动
- 任一角色退回补件：保留已完成意见、阻止推进、补件后重审开启新一轮
- 任一角色拒绝：整单终态，意见保留且不可再推进/重审
- 角色缺席：必审角色未全部通过时停留在评审中；非名单角色提交被拒
- 审批配置变化不改写已结束轮次的快照，仅对新一轮生效
- 重复审批（唯一约束兜底）与乐观锁并发冲突返回可解释结果
- 多线程并行写入不同角色意见
- 服务重启（重建引擎/会话）后完整审批轨迹可恢复
- 按角色查询待办
"""

import os
import tempfile
import threading
import unittest
from datetime import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app import models
from app.enums import (
    IntentStatus,
    ParkType,
    ProjectStatus,
    Region,
    ReviewDecision,
    ReviewRole,
    ReviewRoundStatus,
)
from app.services import intent_review
from app.services.intent_review import ReviewConflict


def _make_engine(db_path: str):
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    return engine


class ReviewTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="review-test-")
        self.db_path = os.path.join(self.tmp_dir, "review_test.db")
        self.engine = _make_engine(self.db_path)
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        self._seed()

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()

    # -- 夹具 -------------------------------------------------------------

    def _seed(self):
        db = self.SessionLocal()
        try:
            park = models.IndustrialPark(
                name="测试园区",
                park_type=ParkType.KEY_INDUSTRIAL,
                city="南宁市",
            )
            initiator = models.Entity(
                name="园区平台公司",
                region=Region.GUANGXI,
                country_or_province="广西",
                contact_person="张三",
                contact_phone="0771-0000001",
            )
            submitter = models.Entity(
                name="泰国果汁公司",
                region=Region.ASSOCIATION_SOUTH_EAST_ASIAN_NATIONS,
                country_or_province="泰国",
                contact_person="Somchai",
                contact_phone="+66-00-0000002",
            )
            db.add_all([park, initiator, submitter])
            db.flush()
            project = models.Project(
                name="测试果汁加工项目",
                status=ProjectStatus.ATTRACTING_INVESTMENT,
                investment_direction="果汁加工",
                planned_investment_10k=10000.0,
                park_id=park.id,
                initiator_id=initiator.id,
            )
            db.add(project)
            db.commit()
            self.project_id = project.id
            self.submitter_id = submitter.id
        finally:
            db.close()

    def create_intent(self, content: str = "拟建设果汁加工生产线") -> int:
        resp = self.client.post(
            "/api/v1/workflow/intents",
            json={
                "project_id": self.project_id,
                "submitter_id": self.submitter_id,
                "cooperation_content": content,
                "proposed_investment_10k": 12000.0,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["id"]

    def start(self, intent_id: int):
        return self.client.post(f"/api/v1/workflow/intents/{intent_id}/reviews/start")

    def decide(
        self,
        intent_id: int,
        role: ReviewRole,
        decision: ReviewDecision = ReviewDecision.APPROVED,
        comment: str = "同意",
        attachment_summary: str = "附件1份，共5页",
        reviewer: str = "评审员",
        expected_round=None,
        expected_version=None,
    ):
        body = {
            "role": role.value,
            "decision": decision.value,
            "comment": comment,
            "attachment_summary": attachment_summary,
            "reviewer": reviewer,
        }
        if expected_round is not None:
            body["expected_round"] = expected_round
        if expected_version is not None:
            body["expected_version"] = expected_version
        return self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/decisions",
            json=body,
        )

    def trace(self, intent_id: int):
        resp = self.client.get(f"/api/v1/workflow/intents/{intent_id}/reviews")
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def todo(self, role: ReviewRole):
        resp = self.client.get(
            "/api/v1/workflow/intents/reviews/todo",
            params={"role": role.value},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()


class HappyPathTests(ReviewTestBase):
    def test_submit_intent_does_not_auto_negotiate(self):
        intent_id = self.create_intent()
        proj = self.client.get(f"/api/v1/projects/{self.project_id}").json()
        self.assertEqual(proj["status"], ProjectStatus.ATTRACTING_INVESTMENT.value)
        intent = self.client.get(f"/api/v1/workflow/intents/{intent_id}").json()
        self.assertEqual(intent["status"], IntentStatus.SUBMITTED.value)
        self.assertEqual(intent["review_stage"], 0)

    def test_all_required_roles_approve_then_enter_discussion(self):
        intent_id = self.create_intent()
        resp = self.start(intent_id)
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["intent_status"], IntentStatus.REVIEWING.value)

        # 投资通过后仍在评审中
        r = self.decide(intent_id, ReviewRole.INVESTMENT, reviewer="投资老王")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["intent_status"], IntentStatus.REVIEWING.value)

        # 法务通过后仍在评审中（园区运营缺席）
        r = self.decide(intent_id, ReviewRole.LEGAL, reviewer="法务小李")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["intent_status"], IntentStatus.REVIEWING.value)

        # 园区运营通过 → 整轮通过，进入洽谈
        r = self.decide(intent_id, ReviewRole.PARK_OPERATION, reviewer="运营阿陈")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["intent_status"], IntentStatus.IN_DISCUSSION.value)
        self.assertEqual(body["rounds"][0]["status"], ReviewRoundStatus.PASSED.value)
        self.assertEqual(body["rounds"][0]["pending_roles"], [])

        # 每个角色的意见、附件摘要、时间戳独立保存
        actions = body["rounds"][0]["actions"]
        self.assertEqual([a["role"] for a in actions], ["投资", "法务", "园区运营"])
        for a in actions:
            self.assertEqual(a["decision"], ReviewDecision.APPROVED.value)
            self.assertIsNotNone(a["created_at"])
            self.assertTrue(a["attachment_summary"].startswith("附件"))

        # 项目联动：招商中 → 洽谈中，并写状态日志
        proj = self.client.get(f"/api/v1/projects/{self.project_id}").json()
        self.assertEqual(proj["status"], ProjectStatus.NEGOTIATING.value)
        logs = self.client.get(
            f"/api/v1/projects/{self.project_id}/status-logs"
        ).json()
        self.assertEqual(logs[0]["to_status"], ProjectStatus.NEGOTIATING.value)
        self.assertIn("分阶段审批", logs[0]["reason"])

        # 进入洽谈后可登记洽谈记录
        neg = self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/negotiations",
            json={
                "round": 1,
                "title": "首轮洽谈",
                "held_at": datetime.utcnow().isoformat(),
                "key_topics": "落地条款",
            },
        )
        self.assertEqual(neg.status_code, 200, neg.text)


class ReturnAndResubmitTests(ReviewTestBase):
    def test_returned_keeps_opinions_blocks_progress_and_resubmit_reopens(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(intent_id, ReviewRole.INVESTMENT, reviewer="投资老王")
        r = self.decide(
            intent_id,
            ReviewRole.LEGAL,
            decision=ReviewDecision.RETURNED,
            comment="需补充环评承诺函",
            attachment_summary="退件清单1页",
            reviewer="法务小李",
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["intent_status"], IntentStatus.RETURNED.value)
        self.assertEqual(body["rounds"][0]["status"], ReviewRoundStatus.RETURNED.value)

        # 已完成意见全部保留（投资的通过 + 法务的退回）
        actions = body["rounds"][0]["actions"]
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["decision"], ReviewDecision.APPROVED.value)
        self.assertEqual(actions[1]["decision"], ReviewDecision.RETURNED.value)

        # 项目仍停留在招商中
        proj = self.client.get(f"/api/v1/projects/{self.project_id}").json()
        self.assertEqual(proj["status"], ProjectStatus.ATTRACTING_INVESTMENT.value)

        # 轮次已关闭，园区运营再提交意见被阻止（round_closed）
        r = self.decide(intent_id, ReviewRole.PARK_OPERATION)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "round_closed")
        self.assertEqual(
            r.json()["detail"]["state"]["status"],
            ReviewRoundStatus.RETURNED.value,
        )

        # 评审中状态之外不能登记洽谈
        neg = self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/negotiations",
            json={
                "round": 1,
                "title": "洽谈",
                "held_at": datetime.utcnow().isoformat(),
                "key_topics": "x",
            },
        )
        self.assertEqual(neg.status_code, 409)

        # 补件后重审：开启第 2 轮，历史轮次原样保留
        r = self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/resubmit",
            json={
                "requirements": "已补充环评承诺函与用地预审意见",
                "submitter_comments": "补正材料见附件摘要",
                "proposed_investment_10k": 13500.0,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["intent_status"], IntentStatus.REVIEWING.value)
        self.assertEqual(body["active_round_no"], 2)
        self.assertEqual(len(body["rounds"]), 2)
        self.assertEqual(body["rounds"][0]["status"], ReviewRoundStatus.RETURNED.value)
        self.assertEqual(body["rounds"][1]["status"], ReviewRoundStatus.IN_REVIEW.value)
        self.assertEqual(
            body["rounds"][1]["pending_roles"],
            ["投资", "法务", "园区运营"],
        )

        # 补件字段确实更新
        detail = self.client.get(f"/api/v1/workflow/intents/{intent_id}").json()
        self.assertEqual(detail["requirements"], "已补充环评承诺函与用地预审意见")
        self.assertEqual(detail["proposed_investment_10k"], 13500.0)

        # 第二轮三角色全部通过 → 进入洽谈
        for role, name in [
            (ReviewRole.INVESTMENT, "投资老王"),
            (ReviewRole.LEGAL, "法务小李"),
            (ReviewRole.PARK_OPERATION, "运营阿陈"),
        ]:
            r = self.decide(intent_id, role, reviewer=name)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["intent_status"], IntentStatus.IN_DISCUSSION.value)
        self.assertEqual(
            r.json()["rounds"][1]["status"], ReviewRoundStatus.PASSED.value
        )

    def test_resubmit_only_allowed_after_return(self):
        intent_id = self.create_intent()
        # 尚未开启审批，不能补件
        r = self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/resubmit",
            json={"requirements": "材料更新"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "intent_status_conflict")


class RejectTests(ReviewTestBase):
    def test_any_role_reject_is_terminal_and_opinions_kept(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(intent_id, ReviewRole.INVESTMENT, reviewer="投资老王")
        r = self.decide(
            intent_id,
            ReviewRole.LEGAL,
            decision=ReviewDecision.REJECTED,
            comment="投资方向与园区准入目录冲突，整单拒绝",
            reviewer="法务小李",
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["intent_status"], IntentStatus.REJECTED.value)
        self.assertEqual(body["rounds"][0]["status"], ReviewRoundStatus.REJECTED.value)

        # 投资已完成的通过意见仍然保留
        decisions = [a["decision"] for a in body["rounds"][0]["actions"]]
        self.assertIn(ReviewDecision.APPROVED.value, decisions)
        self.assertIn(ReviewDecision.REJECTED.value, decisions)

        # 拒绝是终态：不能再提交意见
        r = self.decide(intent_id, ReviewRole.PARK_OPERATION)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "intent_status_conflict")

        # 不能补件重审
        r = self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/resubmit",
            json={"requirements": "重新申报"},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "intent_rejected")

        # 项目未被推进
        proj = self.client.get(f"/api/v1/projects/{self.project_id}").json()
        self.assertEqual(proj["status"], ProjectStatus.ATTRACTING_INVESTMENT.value)


class RoleAbsenceTests(ReviewTestBase):
    def test_missing_required_role_keeps_intent_in_review(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(intent_id, ReviewRole.INVESTMENT)
        self.decide(intent_id, ReviewRole.PARK_OPERATION)
        body = self.trace(intent_id)
        self.assertEqual(body["intent_status"], IntentStatus.REVIEWING.value)
        self.assertEqual(body["rounds"][0]["pending_roles"], ["法务"])

        # 法务的待办里能看到，投资/园区运营的待办里已消失
        todo_legal = self.todo(ReviewRole.LEGAL)
        self.assertTrue(any(t["intent_id"] == intent_id for t in todo_legal))
        todo_inv = self.todo(ReviewRole.INVESTMENT)
        self.assertFalse(any(t["intent_id"] == intent_id for t in todo_inv))

    def test_role_not_in_config_cannot_submit(self):
        intent_id = self.create_intent()
        # 配置为本单只需要投资 + 法务
        r = self.client.put(
            f"/api/v1/workflow/intents/{intent_id}/review-config",
            json={"required_roles": ["投资", "法务"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.start(intent_id)

        r = self.decide(intent_id, ReviewRole.PARK_OPERATION)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "role_not_required")

        # 投资、法务通过即可进入洽谈（园区运营缺席不影响，因配置不含该角色）
        self.decide(intent_id, ReviewRole.INVESTMENT)
        r = self.decide(intent_id, ReviewRole.LEGAL)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["intent_status"], IntentStatus.IN_DISCUSSION.value)


class ConfigSnapshotTests(ReviewTestBase):
    def test_config_change_does_not_rewrite_finished_round(self):
        intent_id = self.create_intent()
        self.start(intent_id)  # 第1轮快照：投资、法务、园区运营
        for role in [
            ReviewRole.INVESTMENT,
            ReviewRole.LEGAL,
            ReviewRole.PARK_OPERATION,
        ]:
            self.decide(intent_id, role)

        # 意向已结束审批进入洽谈后改配置
        r = self.client.put(
            f"/api/v1/workflow/intents/{intent_id}/review-config",
            json={"required_roles": ["投资", "法务"]},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(r.json()["applies_from_round"], 2)

        body = self.trace(intent_id)
        # 已结束的第 1 轮快照与结论不变，意向仍在洽谈
        self.assertEqual(
            body["rounds"][0]["required_roles"],
            ["投资", "法务", "园区运营"],
        )
        self.assertEqual(
            body["rounds"][0]["status"], ReviewRoundStatus.PASSED.value
        )
        self.assertEqual(body["intent_status"], IntentStatus.IN_DISCUSSION.value)
        self.assertEqual(body["config"]["required_roles"], ["投资", "法务"])

    def test_config_change_applies_to_next_round_only(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(intent_id, ReviewRole.INVESTMENT)
        self.decide(
            intent_id,
            ReviewRole.LEGAL,
            decision=ReviewDecision.RETURNED,
            comment="补土地证明",
        )

        # 退回后把必审角色改为只含投资、法务（去掉园区运营）
        r = self.client.put(
            f"/api/v1/workflow/intents/{intent_id}/review-config",
            json={"required_roles": ["投资", "法务"]},
        )
        self.assertEqual(r.status_code, 200, r.text)

        r = self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/resubmit",
            json={"requirements": "已补土地证明"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        round2 = r.json()["rounds"][1]
        # 新轮次采用新配置，第 1 轮快照不变
        self.assertEqual(round2["required_roles"], ["投资", "法务"])
        self.assertEqual(
            r.json()["rounds"][0]["required_roles"],
            ["投资", "法务", "园区运营"],
        )

        self.decide(intent_id, ReviewRole.INVESTMENT)
        r = self.decide(intent_id, ReviewRole.LEGAL)
        self.assertEqual(r.json()["intent_status"], IntentStatus.IN_DISCUSSION.value)

    def test_config_optimistic_version_conflict(self):
        intent_id = self.create_intent()
        self.client.put(
            f"/api/v1/workflow/intents/{intent_id}/review-config",
            json={"required_roles": ["投资", "法务"]},
        )
        # 基于不存在的旧版本号更新 → 冲突
        r = self.client.put(
            f"/api/v1/workflow/intents/{intent_id}/review-config",
            json={"required_roles": ["投资"], "expected_version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "config_version_conflict")
        self.assertEqual(r.json()["detail"]["state"]["current_version"], 1)

    def test_empty_config_rejected(self):
        intent_id = self.create_intent()
        r = self.client.put(
            f"/api/v1/workflow/intents/{intent_id}/review-config",
            json={"required_roles": []},
        )
        self.assertEqual(r.status_code, 422)


class DuplicateAndConcurrencyTests(ReviewTestBase):
    def test_duplicate_decision_rejected_with_explainable_conflict(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        r1 = self.decide(intent_id, ReviewRole.INVESTMENT, reviewer="投资老王")
        self.assertEqual(r1.status_code, 200)
        r2 = self.decide(intent_id, ReviewRole.INVESTMENT, reviewer="投资老王")
        self.assertEqual(r2.status_code, 409)
        detail = r2.json()["detail"]
        self.assertEqual(detail["code"], "duplicate_decision")
        self.assertIn("已提交过", detail["message"])
        self.assertEqual(
            detail["state"]["existing_decision"], ReviewDecision.APPROVED.value
        )
        self.assertEqual(detail["state"]["existing_reviewer"], "投资老王")
        self.assertIsNotNone(detail["state"]["existing_at"])
        # 重复提交没有产生第二条意见
        body = self.trace(intent_id)
        self.assertEqual(len(body["rounds"][0]["actions"]), 1)

    def test_start_review_is_idempotent_conflict(self):
        intent_id = self.create_intent()
        self.assertEqual(self.start(intent_id).status_code, 200)
        r = self.start(intent_id)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "review_already_started")

    def test_round_version_optimistic_lock(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(intent_id, ReviewRole.INVESTMENT)  # 轮次版本 1 → 2
        # 法务带着过期版本号提交
        r = self.decide(
            intent_id,
            ReviewRole.LEGAL,
            expected_round=1,
            expected_version=1,
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "round_version_conflict")
        self.assertEqual(r.json()["detail"]["state"]["version"], 2)

    def test_expected_round_moved_after_resubmit(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(intent_id, ReviewRole.INVESTMENT)
        self.decide(
            intent_id, ReviewRole.LEGAL, decision=ReviewDecision.RETURNED,
            comment="补材料",
        )
        self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/resubmit",
            json={"requirements": "补齐"},
        )
        # 客户端仍按第 1 轮提交 → round_moved
        r = self.decide(intent_id, ReviewRole.INVESTMENT, expected_round=1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["detail"]["code"], "round_moved")
        self.assertEqual(r.json()["detail"]["state"]["round_no"], 2)

    def test_concurrent_decisions_distinct_roles_both_persist(self):
        intent_id = self.create_intent()
        intent_review.start_review(self.SessionLocal(), intent_id)

        barrier = threading.Barrier(2)
        outcomes = []

        def worker(role: ReviewRole, reviewer: str):
            session = self.SessionLocal()
            try:
                barrier.wait(timeout=10)
                intent_review.submit_decision(
                    session,
                    intent_id=intent_id,
                    role=role,
                    decision=ReviewDecision.APPROVED,
                    reviewer=reviewer,
                )
                outcomes.append(("ok", role.value))
            except ReviewConflict as exc:
                session.rollback()
                outcomes.append(("conflict", role.value, exc.code))
            finally:
                session.close()

        t1 = threading.Thread(
            target=worker, args=(ReviewRole.INVESTMENT, "投资并发")
        )
        t2 = threading.Thread(
            target=worker, args=(ReviewRole.LEGAL, "法务并发")
        )
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        self.assertEqual(len(outcomes), 2)
        self.assertTrue(all(o[0] == "ok" for o in outcomes), outcomes)

        body = self.trace(intent_id)
        self.assertEqual(len(body["rounds"][0]["actions"]), 2)
        self.assertEqual(
            body["rounds"][0]["pending_roles"], ["园区运营"]
        )

    def test_concurrent_same_role_only_one_wins(self):
        intent_id = self.create_intent()
        intent_review.start_review(self.SessionLocal(), intent_id)

        barrier = threading.Barrier(2)
        outcomes = []

        def worker(reviewer: str):
            session = self.SessionLocal()
            try:
                barrier.wait(timeout=10)
                intent_review.submit_decision(
                    session,
                    intent_id=intent_id,
                    role=ReviewRole.INVESTMENT,
                    decision=ReviewDecision.APPROVED,
                    reviewer=reviewer,
                )
                outcomes.append(("ok", reviewer))
            except ReviewConflict as exc:
                session.rollback()
                outcomes.append(("conflict", reviewer, exc.code))
            finally:
                session.close()

        t1 = threading.Thread(target=worker, args=("投资甲",))
        t2 = threading.Thread(target=worker, args=("投资乙",))
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        self.assertEqual(len(outcomes), 2)
        codes = {o[0]: o for o in outcomes}
        self.assertIn("ok", codes)
        conflict = [o for o in outcomes if o[0] == "conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0][2], "duplicate_decision")

        body = self.trace(intent_id)
        self.assertEqual(len(body["rounds"][0]["actions"]), 1)

    def test_concurrent_resubmit_only_one_new_round(self):
        intent_id = self.create_intent()
        intent_review.start_review(self.SessionLocal(), intent_id)
        db = self.SessionLocal()
        round1 = intent_review._get_round(db, intent_id, 1)
        round1.status = ReviewRoundStatus.RETURNED
        round1.closed_at = datetime.utcnow()
        intent = intent_review._get_intent(db, intent_id)
        intent.status = IntentStatus.RETURNED
        db.commit()
        db.close()

        barrier = threading.Barrier(2)
        outcomes = []

        def worker():
            session = self.SessionLocal()
            try:
                barrier.wait(timeout=10)
                intent_review.resubmit(session, intent_id, {"requirements": "补正"})
                outcomes.append("ok")
            except ReviewConflict as exc:
                session.rollback()
                outcomes.append(exc.code)
            finally:
                session.close()

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        self.assertEqual(sorted(outcomes), ["ok", "round_exists"])
        body = self.trace(intent_id)
        self.assertEqual(len(body["rounds"]), 2)
        self.assertEqual(body["active_round_no"], 2)


class TodoTests(ReviewTestBase):
    def test_todo_lists_only_pending_in_review_intents(self):
        intent_a = self.create_intent("意向A")
        intent_b = self.create_intent("意向B")
        self.start(intent_a)
        self.start(intent_b)
        self.decide(intent_a, ReviewRole.INVESTMENT)
        # 投资的待办只剩 B
        todo_inv = self.todo(ReviewRole.INVESTMENT)
        ids = [t["intent_id"] for t in todo_inv]
        self.assertNotIn(intent_a, ids)
        self.assertIn(intent_b, ids)
        item = next(t for t in todo_inv if t["intent_id"] == intent_b)
        self.assertEqual(item["round_no"], 1)
        self.assertEqual(item["pending_roles"], ["投资", "法务", "园区运营"])
        self.assertEqual(item["decided_roles"], [])
        self.assertEqual(item["project_name"], "测试果汁加工项目")

        # 退回后待办消失（等待提交方补件，而非角色继续审）
        self.decide(
            intent_b, ReviewRole.INVESTMENT,
            decision=ReviewDecision.RETURNED, comment="补件",
        )
        todo_legal = self.todo(ReviewRole.LEGAL)
        self.assertNotIn(intent_b, [t["intent_id"] for t in todo_legal])


class DirectStatusUpdateTests(ReviewTestBase):
    def test_cannot_bypass_review_via_generic_update(self):
        intent_id = self.create_intent()
        r = self.client.put(
            f"/api/v1/workflow/intents/{intent_id}",
            json={"status": IntentStatus.IN_DISCUSSION.value},
        )
        self.assertEqual(r.status_code, 409)
        self.assertIn("分阶段审批", r.json()["detail"])


class RecoveryAfterRestartTests(ReviewTestBase):
    def test_trace_and_todo_recover_after_engine_restart(self):
        intent_id = self.create_intent()
        self.start(intent_id)
        self.decide(
            intent_id, ReviewRole.INVESTMENT,
            comment="投资意见", attachment_summary="尽调报告12页",
            reviewer="投资老王",
        )
        self.decide(
            intent_id, ReviewRole.LEGAL,
            decision=ReviewDecision.RETURNED,
            comment="法务退件", attachment_summary="补正清单1页",
            reviewer="法务小李",
        )
        self.client.post(
            f"/api/v1/workflow/intents/{intent_id}/reviews/resubmit",
            json={"requirements": "补正完成"},
        )
        self.decide(intent_id, ReviewRole.INVESTMENT, reviewer="投资老王(二审)")

        # 模拟服务重启：销毁旧引擎，用同一数据库文件新建引擎与会话工厂
        app.dependency_overrides.clear()
        self.engine.dispose()
        self.engine = _make_engine(self.db_path)
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )

        def override_get_db():
            db = self.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

        # 完整轨迹可还原：2 轮、各角色意见/附件摘要/时间戳、当前停留在第2轮
        body = self.trace(intent_id)
        self.assertEqual(body["intent_status"], IntentStatus.REVIEWING.value)
        self.assertEqual(body["active_round_no"], 2)
        self.assertEqual(len(body["rounds"]), 2)

        r1, r2 = body["rounds"]
        self.assertEqual(r1["status"], ReviewRoundStatus.RETURNED.value)
        self.assertEqual(
            [a["role"] for a in r1["actions"]], ["投资", "法务"]
        )
        self.assertEqual(
            r1["actions"][0]["attachment_summary"], "尽调报告12页"
        )
        self.assertEqual(r1["actions"][1]["comment"], "法务退件")
        self.assertTrue(r1["closed_at"])
        self.assertEqual(
            r2["required_roles"], ["投资", "法务", "园区运营"]
        )
        self.assertEqual(r2["pending_roles"], ["法务", "园区运营"])
        self.assertEqual(r2["actions"][0]["reviewer"], "投资老王(二审)")

        # 待办也从持久化状态恢复
        todo_legal = self.todo(ReviewRole.LEGAL)
        self.assertTrue(any(t["intent_id"] == intent_id for t in todo_legal))
        todo_inv = self.todo(ReviewRole.INVESTMENT)
        self.assertFalse(any(t["intent_id"] == intent_id for t in todo_inv))

        # 重启后继续把第 2 轮审完，正常进入洽谈
        self.decide(intent_id, ReviewRole.LEGAL, reviewer="法务小李(二审)")
        r = self.decide(intent_id, ReviewRole.PARK_OPERATION, reviewer="运营阿陈")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["intent_status"], IntentStatus.IN_DISCUSSION.value)


if __name__ == "__main__":
    unittest.main()
