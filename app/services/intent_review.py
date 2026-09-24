"""合作意向分阶段审批：投资、法务、园区运营三类角色并行审阅。

设计要点
--------
- ``IntentReviewConfig`` 保存意向当前的必审角色配置；每一轮审批在开启时把
  必审角色复制为 ``required_roles_snapshot`` 快照，因此事后修改配置不会改写
  任何已结束或进行中的轮次，只有下一轮（补件重审）才采用新配置。
- 每个角色在每一轮中只有一条 ``IntentReviewAction``，独立记录意见、附件摘要、
  评审人和时间戳。
- 退回补件（RETURNED）关闭当前轮但保留全部意见，提交方补件后开启新的一轮；
  拒绝（REJECTED）为终态，不可再推进。
- 轮次带 ``version`` 乐观锁；``(intent_id, round_no)`` 与 ``(round_id, role)``
  两个唯一约束在 SQLite 不支持行锁的情况下兜底并发写入，冲突统一抛
  :class:`ReviewConflict`，携带可解释的消息与当前状态。
"""

from datetime import datetime
from typing import Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..enums import (
    IntentStatus,
    ProjectStatus,
    ReviewDecision,
    ReviewRole,
    ReviewRoundStatus,
)
from .status_flow import transition_project_status


# 未单独配置时的默认必审角色：投资、法务、园区运营并行审阅。
DEFAULT_REQUIRED_ROLES: List[ReviewRole] = [
    ReviewRole.INVESTMENT,
    ReviewRole.LEGAL,
    ReviewRole.PARK_OPERATION,
]

# 允许提交方补件时修改的意向字段白名单。
RESUBMIT_EDITABLE_FIELDS = (
    "cooperation_mode",
    "proposed_investment_10k",
    "proposed_capacity_tonnes",
    "cooperation_content",
    "expected_timeline",
    "requirements",
    "submitter_comments",
)


class ReviewConflict(Exception):
    """审批流程冲突，携带可直接展示的原因与当前状态快照。"""

    def __init__(
        self,
        message: str,
        code: str = "review_conflict",
        state: Optional[dict] = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.state = state or {}


# ---------------------------------------------------------------------------
# 角色序列化
# ---------------------------------------------------------------------------

def _serialize_roles(roles: List[ReviewRole]) -> str:
    return ",".join(r.value for r in roles)


def _parse_roles(raw: Optional[str]) -> List[ReviewRole]:
    if not raw:
        return []
    by_value = {r.value: r for r in ReviewRole}
    return [by_value[v] for v in raw.split(",") if v in by_value]


def _normalize_roles(roles: List[ReviewRole]) -> List[ReviewRole]:
    """按枚举固定顺序去重，保证快照与配置的稳定表示。"""
    unique = {r for r in roles}
    return [r for r in ReviewRole if r in unique]


# ---------------------------------------------------------------------------
# 内部查询
# ---------------------------------------------------------------------------

def _get_intent(db: Session, intent_id: int) -> Optional[models.CooperationIntent]:
    return (
        db.query(models.CooperationIntent)
        .options(
            joinedload(models.CooperationIntent.project),
            joinedload(models.CooperationIntent.submitter),
            joinedload(models.CooperationIntent.review_rounds),
        )
        .filter(models.CooperationIntent.id == intent_id)
        .first()
    )


def _get_config(
    db: Session, intent_id: int
) -> Optional[models.IntentReviewConfig]:
    return (
        db.query(models.IntentReviewConfig)
        .filter(models.IntentReviewConfig.intent_id == intent_id)
        .first()
    )


def get_effective_roles(
    db: Session, intent_id: int
) -> List[ReviewRole]:
    """当前生效的必审角色：单独配置优先，否则用默认三角色。"""
    config = _get_config(db, intent_id)
    if config:
        return _parse_roles(config.required_roles)
    return list(DEFAULT_REQUIRED_ROLES)


def _get_round(
    db: Session, intent_id: int, round_no: int
) -> Optional[models.IntentReviewRound]:
    return (
        db.query(models.IntentReviewRound)
        .options(joinedload(models.IntentReviewRound.actions))
        .filter(
            models.IntentReviewRound.intent_id == intent_id,
            models.IntentReviewRound.round_no == round_no,
        )
        .first()
    )


def _get_active_round(
    db: Session, intent: models.CooperationIntent
) -> Optional[models.IntentReviewRound]:
    if not intent.review_stage:
        return None
    return _get_round(db, intent.id, intent.review_stage)


def _round_state(round_obj: Optional[models.IntentReviewRound]) -> dict:
    if round_obj is None:
        return {"round_no": None, "version": None, "status": None}
    return {
        "round_no": round_obj.round_no,
        "version": round_obj.version,
        "status": round_obj.status.value,
    }


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def update_config(
    db: Session,
    intent_id: int,
    required_roles: List[ReviewRole],
    expected_version: Optional[int] = None,
) -> models.IntentReviewConfig:
    """更新必审角色配置。

    已开启的轮次保留各自快照，不受影响；新版本只对下一轮审批生效。
    """
    roles = _normalize_roles(required_roles)
    if not roles:
        raise ReviewConflict("必审角色至少保留一个", code="invalid_config")

    config = _get_config(db, intent_id)
    if config is None:
        config = models.IntentReviewConfig(
            intent_id=intent_id,
            required_roles=_serialize_roles(roles),
            version=1,
        )
        db.add(config)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            config = _get_config(db, intent_id)
            if config is None:
                raise
            # 并发下已被创建：落入下方常规更新流程。
        else:
            db.refresh(config)
            return config

    if expected_version is not None and config.version != expected_version:
        raise ReviewConflict(
            f"审批配置已被他人更新（当前版本 v{config.version}，"
            f"客户端基于 v{expected_version}），请刷新后重试",
            code="config_version_conflict",
            state={"current_version": config.version},
        )

    new_value = _serialize_roles(roles)
    if config.required_roles != new_value:
        config.required_roles = new_value
        config.version += 1
        db.commit()
        db.refresh(config)
    return config


def build_config_view(
    db: Session, intent: models.CooperationIntent
) -> Dict:
    config = _get_config(db, intent.id)
    active_round = _get_active_round(db, intent)
    if config:
        return {
            "configured": True,
            "required_roles": _parse_roles(config.required_roles),
            "version": config.version,
            "updated_at": config.updated_at,
            # 进行中的轮次沿用旧快照；配置最早从下一轮开始生效。
            "applies_from_round": (
                active_round.round_no + 1 if active_round is not None else 1
            ),
        }
    return {
        "configured": False,
        "required_roles": list(DEFAULT_REQUIRED_ROLES),
        "version": None,
        "updated_at": None,
        "applies_from_round": (
            active_round.round_no + 1 if active_round is not None else 1
        ),
    }


# ---------------------------------------------------------------------------
# 审批动作
# ---------------------------------------------------------------------------

def start_review(db: Session, intent_id: int) -> Dict:
    """开启首轮审批（把意向从「已提交」置为「评审中」并快照配置）。"""
    intent = _get_intent(db, intent_id)
    if intent is None:
        raise ReviewConflict("合作意向不存在", code="intent_not_found")

    if intent.review_stage:
        round_obj = _get_round(db, intent_id, intent.review_stage)
        raise ReviewConflict(
            f"审批已开启，当前为第 {intent.review_stage} 轮，请勿重复开启",
            code="review_already_started",
            state=_round_state(round_obj),
        )
    if intent.status != IntentStatus.SUBMITTED:
        raise ReviewConflict(
            f"意向当前状态为「{intent.status.value}」，仅「已提交」的意向可开启审批",
            code="intent_status_conflict",
            state={"intent_status": intent.status.value},
        )

    roles = get_effective_roles(db, intent_id)
    round_obj = models.IntentReviewRound(
        intent_id=intent_id,
        round_no=1,
        status=ReviewRoundStatus.IN_REVIEW,
        required_roles_snapshot=_serialize_roles(roles),
        version=1,
        started_at=datetime.utcnow(),
    )
    db.add(round_obj)
    intent.review_stage = 1
    intent.status = IntentStatus.REVIEWING
    if intent.reviewed_at is None:
        intent.reviewed_at = datetime.utcnow()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _get_round(db, intent_id, 1)
        raise ReviewConflict(
            "审批已被并发开启，请刷新后重试",
            code="review_already_started",
            state=_round_state(existing),
        )
    return build_trace(db, intent_id)


def submit_decision(
    db: Session,
    intent_id: int,
    role: ReviewRole,
    decision: ReviewDecision,
    comment: Optional[str] = None,
    attachment_summary: Optional[str] = None,
    reviewer: Optional[str] = None,
    expected_round: Optional[int] = None,
    expected_version: Optional[int] = None,
) -> Dict:
    """某一角色提交本轮意见。"""
    intent = _get_intent(db, intent_id)
    if intent is None:
        raise ReviewConflict("合作意向不存在", code="intent_not_found")

    if intent.status not in (IntentStatus.REVIEWING, IntentStatus.RETURNED):
        raise ReviewConflict(
            f"意向当前状态为「{intent.status.value}」，不接受审批意见",
            code="intent_status_conflict",
            state={"intent_status": intent.status.value, **_round_state(
                _get_active_round(db, intent)
            )},
        )

    round_obj = _get_active_round(db, intent)
    if round_obj is None:
        raise ReviewConflict(
            "审批尚未开启，请先开启第 1 轮审批",
            code="review_not_started",
        )

    if expected_round is not None and expected_round != round_obj.round_no:
        raise ReviewConflict(
            f"客户端针对第 {expected_round} 轮提交，但当前已是第 "
            f"{round_obj.round_no} 轮（补件后会开启新一轮），请刷新待办后重试",
            code="round_moved",
            state=_round_state(round_obj),
        )
    if expected_version is not None and expected_version != round_obj.version:
        raise ReviewConflict(
            f"第 {round_obj.round_no} 轮已被其他角色更新（版本 v{round_obj.version}，"
            f"客户端基于 v{expected_version}），请刷新后重试",
            code="round_version_conflict",
            state=_round_state(round_obj),
        )

    if round_obj.status != ReviewRoundStatus.IN_REVIEW:
        raise ReviewConflict(
            f"第 {round_obj.round_no} 轮审批已结束（{round_obj.status.value}），"
            "不能重复提交意见",
            code="round_closed",
            state=_round_state(round_obj),
        )

    required_roles = _parse_roles(round_obj.required_roles_snapshot)
    if role not in required_roles:
        raise ReviewConflict(
            f"角色「{role.value}」不在第 {round_obj.round_no} 轮的必审角色名单"
            f"（{ '、'.join(r.value for r in required_roles) }）中，无权提交意见",
            code="role_not_required",
            state=_round_state(round_obj),
        )

    existing = (
        db.query(models.IntentReviewAction)
        .filter(
            models.IntentReviewAction.round_id == round_obj.id,
            models.IntentReviewAction.role == role,
        )
        .first()
    )
    if existing is not None:
        raise ReviewConflict(
            f"角色「{role.value}」在第 {round_obj.round_no} 轮已提交过"
            f"「{existing.decision.value}」意见（{existing.reviewer or '未署名'}，"
            f"{existing.created_at.isoformat()}），不能重复审批；"
            "如需改变结论请退回补件后在新一轮重新提交",
            code="duplicate_decision",
            state={
                **_round_state(round_obj),
                "existing_decision": existing.decision.value,
                "existing_reviewer": existing.reviewer,
                "existing_at": existing.created_at.isoformat(),
            },
        )

    action = models.IntentReviewAction(
        intent_id=intent_id,
        round_id=round_obj.id,
        role=role,
        decision=decision,
        comment=comment,
        attachment_summary=attachment_summary,
        reviewer=reviewer,
        created_at=datetime.utcnow(),
    )
    try:
        db.add(action)

        now = datetime.utcnow()
        if decision == ReviewDecision.REJECTED:
            # 任一角色拒绝 = 整单被拒，终态，保留所有已完成意见。
            round_obj.status = ReviewRoundStatus.REJECTED
            round_obj.closed_at = now
            intent.status = IntentStatus.REJECTED
        elif decision == ReviewDecision.RETURNED:
            # 退回补件：关闭本轮、阻止推进，等待提交方补件重开新一轮。
            round_obj.status = ReviewRoundStatus.RETURNED
            round_obj.closed_at = now
            intent.status = IntentStatus.RETURNED
        else:
            db.flush()
            approved_roles = {
                a.role
                for a in db.query(models.IntentReviewAction).filter(
                    models.IntentReviewAction.round_id == round_obj.id,
                    models.IntentReviewAction.decision == ReviewDecision.APPROVED,
                )
            }
            if set(required_roles).issubset(approved_roles):
                # 配置的必审角色全部通过：进入洽谈，项目同步从招商中转洽谈中。
                round_obj.status = ReviewRoundStatus.PASSED
                round_obj.closed_at = now
                intent.status = IntentStatus.IN_DISCUSSION
                project = (
                    db.query(models.Project)
                    .filter(models.Project.id == intent.project_id)
                    .first()
                )
                if project is not None and project.status == ProjectStatus.ATTRACTING_INVESTMENT:
                    transition_project_status(
                        db,
                        project=project,
                        to_status=ProjectStatus.NEGOTIATING,
                        operator=reviewer,
                        reason="合作意向分阶段审批全部通过，转入洽谈阶段",
                        skip_validation=True,
                    )

        round_obj.version += 1
        if intent.reviewed_at is None:
            intent.reviewed_at = now
        db.commit()
    except IntegrityError:
        # 并发下唯一约束兜底：同角色重复意见或轮次被并发推进。
        db.rollback()
        fresh_round = _get_round(db, intent_id, intent.review_stage)
        fresh_action = (
            db.query(models.IntentReviewAction)
            .filter(
                models.IntentReviewAction.round_id == fresh_round.id,
                models.IntentReviewAction.role == role,
            ).first()
            if fresh_round is not None
            else None
        )
        state = _round_state(fresh_round)
        if fresh_action is not None:
            state.update(
                existing_decision=fresh_action.decision.value,
                existing_reviewer=fresh_action.reviewer,
                existing_at=fresh_action.created_at.isoformat(),
            )
            message = (
                f"角色「{role.value}」的意见已被并发写入"
                f"（{fresh_action.reviewer or '未署名'}，{fresh_action.created_at.isoformat()}），"
                "本次重复提交未生效"
            )
            code = "duplicate_decision"
        else:
            message = "本轮审批状态已被并发更新，请刷新后重试"
            code = "concurrent_update"
        raise ReviewConflict(message, code=code, state=state)

    return build_trace(db, intent_id)


def resubmit(
    db: Session,
    intent_id: int,
    changes: Optional[Dict] = None,
) -> Dict:
    """提交方补件后重开新一轮审批；历史轮次与意见原样保留。"""
    intent = _get_intent(db, intent_id)
    if intent is None:
        raise ReviewConflict("合作意向不存在", code="intent_not_found")

    if intent.status == IntentStatus.REJECTED:
        raise ReviewConflict(
            "意向已被整单拒绝，不能补件重审",
            code="intent_rejected",
            state={"intent_status": intent.status.value},
        )
    if intent.status != IntentStatus.RETURNED:
        raise ReviewConflict(
            f"意向当前状态为「{intent.status.value}」，只有被退回补件的意向才能重新提交",
            code="intent_status_conflict",
            state={"intent_status": intent.status.value},
        )

    last_round = _get_active_round(db, intent)
    if last_round is None or last_round.status != ReviewRoundStatus.RETURNED:
        raise ReviewConflict(
            "不存在已退回的审批轮次，无法重开审批",
            code="round_state_conflict",
            state=_round_state(last_round),
        )

    new_round_no = last_round.round_no + 1
    roles = get_effective_roles(db, intent_id)
    new_round = models.IntentReviewRound(
        intent_id=intent_id,
        round_no=new_round_no,
        status=ReviewRoundStatus.IN_REVIEW,
        required_roles_snapshot=_serialize_roles(roles),
        version=1,
        started_at=datetime.utcnow(),
    )
    db.add(new_round)

    changes = changes or {}
    for field in RESUBMIT_EDITABLE_FIELDS:
        if field in changes and changes[field] is not None:
            setattr(intent, field, changes[field])

    intent.review_stage = new_round_no
    intent.status = IntentStatus.REVIEWING
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fresh = _get_intent(db, intent_id)
        raise ReviewConflict(
            f"第 {fresh.review_stage} 轮审批已被并发开启，本次补件提交未生效",
            code="round_exists",
            state={
                "intent_status": fresh.status.value,
                **_round_state(_get_active_round(db, fresh)),
            },
        )
    return build_trace(db, intent_id)


# ---------------------------------------------------------------------------
# 查询：待办与轨迹
# ---------------------------------------------------------------------------

def list_todo(db: Session, role: ReviewRole) -> List[Dict]:
    """按角色查询待办：意向评审中、当前轮进行中、该角色在快照名单内且尚未提交。"""
    rounds = (
        db.query(models.IntentReviewRound)
        .options(
            joinedload(models.IntentReviewRound.actions),
            joinedload(models.IntentReviewRound.intent).joinedload(
                models.CooperationIntent.project
            ),
            joinedload(models.IntentReviewRound.intent).joinedload(
                models.CooperationIntent.submitter
            ),
        )
        .filter(models.IntentReviewRound.status == ReviewRoundStatus.IN_REVIEW)
        .order_by(models.IntentReviewRound.started_at)
        .all()
    )
    todos = []
    for round_obj in rounds:
        intent = round_obj.intent
        if intent.status != IntentStatus.REVIEWING:
            continue
        required_roles = _parse_roles(round_obj.required_roles_snapshot)
        if role not in required_roles:
            continue
        decided_roles = [a.role for a in round_obj.actions]
        if role in decided_roles:
            continue
        todos.append(
            {
                "intent_id": intent.id,
                "round_no": round_obj.round_no,
                "role": role,
                "project_id": intent.project_id,
                "project_name": intent.project.name if intent.project else None,
                "submitter_id": intent.submitter_id,
                "submitter_name": intent.submitter.name if intent.submitter else None,
                "round_started_at": round_obj.started_at,
                "submitted_at": intent.submitted_at,
                "required_roles": required_roles,
                "decided_roles": decided_roles,
                "pending_roles": [
                    r for r in required_roles if r not in decided_roles
                ],
            }
        )
    return todos


def _build_round_view(round_obj: models.IntentReviewRound) -> Dict:
    required_roles = _parse_roles(round_obj.required_roles_snapshot)
    actions = sorted(round_obj.actions, key=lambda a: a.created_at)
    decided_roles = [a.role for a in actions]
    return {
        "round_no": round_obj.round_no,
        "status": round_obj.status,
        "required_roles": required_roles,
        "version": round_obj.version,
        "started_at": round_obj.started_at,
        "closed_at": round_obj.closed_at,
        "pending_roles": [r for r in required_roles if r not in decided_roles],
        "actions": [
            {
                "role": a.role,
                "decision": a.decision,
                "reviewer": a.reviewer,
                "comment": a.comment,
                "attachment_summary": a.attachment_summary,
                "created_at": a.created_at,
            }
            for a in actions
        ],
    }


def build_trace(db: Session, intent_id: int) -> Optional[Dict]:
    """还原完整审批轨迹：配置快照 + 每轮状态 + 每个角色的意见与时间戳。"""
    intent = _get_intent(db, intent_id)
    if intent is None:
        return None
    rounds = (
        db.query(models.IntentReviewRound)
        .options(joinedload(models.IntentReviewRound.actions))
        .filter(models.IntentReviewRound.intent_id == intent_id)
        .order_by(models.IntentReviewRound.round_no)
        .all()
    )
    return {
        "intent_id": intent_id,
        "intent_status": intent.status,
        "review_stage": intent.review_stage,
        "active_round_no": intent.review_stage if rounds else None,
        "config": build_config_view(db, intent),
        "rounds": [_build_round_view(r) for r in rounds],
    }
