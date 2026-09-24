"""合作意向的分阶段并行审批领域服务。

三类审阅角色（投资/法务/园区运营）在同一轮内并行给出独立意见：

- 开启一轮审批时，把当时配置的必审角色快照到轮次上，配置之后如何变化
  都不会改写进行中或已结束的轮次（已结束的意向同样不受影响）；
- 每个角色每轮只能提交一条不可变意见，重复提交、并发提交均落到 409，
  并回带当前轮次真实状态作为可解释冲突结果；
- 任一角色"补件退回"即关闭本轮（已留意见全部保留），补件后显式发起
  下一轮重审；任一角色"拒绝"则整单终止，不可重审；
- 快照内全部必审角色通过，意向才进入洽谈，项目才从招商中转洽谈中。

所有写操作运行在 BEGIN IMMEDIATE 串行化事务中（见 database.py），
读操作直接使用请求级会话。
"""

from datetime import datetime
from typing import List, Optional, Sequence, Set, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..database import run_immediate
from ..enums import (
    IntentStatus,
    ProjectStatus,
    ReviewDecision,
    ReviewRole,
    ReviewRoundStatus,
    ReviewStageStatus,
)
from .status_flow import transition_project_status

ROLE_ORDER: Tuple[ReviewRole, ...] = (
    ReviewRole.INVESTMENT,
    ReviewRole.LEGAL,
    ReviewRole.PARK_OPERATIONS,
)


class ApprovalFlowError(ValueError):
    """审批流程冲突。code 供接口层稳定识别，message 面向调用方解释。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 409,
        context: Optional[dict] = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.context = context


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def normalize_roles(roles: Sequence[ReviewRole]) -> List[ReviewRole]:
    """按固定枚举顺序去重，保证快照与配置比较稳定。"""
    seen: Set[ReviewRole] = set()
    normalized: List[ReviewRole] = []
    for role in roles:
        if role not in seen:
            seen.add(role)
            normalized.append(role)
    order = {r: i for i, r in enumerate(ROLE_ORDER)}
    normalized.sort(key=lambda r: order.get(r, len(ROLE_ORDER)))
    return normalized


def roles_to_text(roles: Sequence[ReviewRole]) -> str:
    return ",".join(r.value for r in roles)


def text_to_roles(text: Optional[str]) -> List[ReviewRole]:
    if not text:
        return []
    by_value = {r.value: r for r in ReviewRole}
    result = []
    for part in text.split(","):
        part = part.strip()
        if part and part in by_value:
            result.append(by_value[part])
    return result


# ---------------------------------------------------------------------------
# 配置：版本化，只有一条 is_active
# ---------------------------------------------------------------------------

class ConfigSnapshot:
    """事务关闭后仍可读取的配置轻量快照。"""

    def __init__(self, config: models.ApprovalConfig):
        self.id = config.id
        self.version = config.version
        self.required_roles = text_to_roles(config.required_roles)
        self.is_active = config.is_active
        self.change_remark = config.change_remark
        self.created_by = config.created_by
        self.created_at = config.created_at


def get_active_config(db: Session) -> Optional[models.ApprovalConfig]:
    return (
        db.query(models.ApprovalConfig)
        .filter(models.ApprovalConfig.is_active.is_(True))
        .order_by(models.ApprovalConfig.version.desc())
        .first()
    )


def list_config_versions(db: Session) -> List[models.ApprovalConfig]:
    return (
        db.query(models.ApprovalConfig)
        .order_by(models.ApprovalConfig.version.desc())
        .all()
    )


def create_config_version(
    roles: Sequence[ReviewRole],
    change_remark: Optional[str] = None,
    created_by: Optional[str] = None,
) -> ConfigSnapshot:
    roles = normalize_roles(roles)
    if not roles:
        raise ApprovalFlowError(
            "REVIEW_CONFIG_EMPTY", "必审角色至少需要配置一个", status_code=400
        )

    def _unit(tx: Session) -> ConfigSnapshot:
        current = get_active_config(tx)
        current_roles = text_to_roles(current.required_roles) if current else []
        if current is not None and current_roles == roles:
            raise ApprovalFlowError(
                "REVIEW_CONFIG_UNCHANGED",
                f"新版必审角色（{roles_to_text(roles)}）与当前生效的第 "
                f"{current.version} 版完全一致，配置未发生变化",
                status_code=400,
            )
        if current is not None:
            current.is_active = False
            next_version = current.version + 1
        else:
            next_version = 1
        config = models.ApprovalConfig(
            version=next_version,
            required_roles=roles_to_text(roles),
            is_active=True,
            change_remark=change_remark,
            created_by=created_by,
        )
        tx.add(config)
        tx.flush()
        return ConfigSnapshot(config)

    return run_immediate(_unit)


# ---------------------------------------------------------------------------
# 查询辅助
# ---------------------------------------------------------------------------

def _get_intent_or_404(db: Session, intent_id: int) -> models.CooperationIntent:
    intent = (
        db.query(models.CooperationIntent)
        .filter(models.CooperationIntent.id == intent_id)
        .first()
    )
    if intent is None:
        raise ApprovalFlowError(
            "REVIEW_INTENT_NOT_FOUND", "合作意向不存在", status_code=404
        )
    return intent


def get_latest_round(
    db: Session, intent_id: int
) -> Optional[models.ApprovalRound]:
    return (
        db.query(models.ApprovalRound)
        .filter(models.ApprovalRound.intent_id == intent_id)
        .order_by(models.ApprovalRound.round_no.desc())
        .first()
    )


def get_approved_round(
    db: Session, intent_id: int
) -> Optional[models.ApprovalRound]:
    return (
        db.query(models.ApprovalRound)
        .filter(
            models.ApprovalRound.intent_id == intent_id,
            models.ApprovalRound.status == ReviewRoundStatus.ALL_APPROVED,
        )
        .order_by(models.ApprovalRound.round_no.desc())
        .first()
    )


def _round_required(round_obj: models.ApprovalRound) -> List[ReviewRole]:
    return text_to_roles(round_obj.required_roles)


def _stage_status(
    round_obj: models.ApprovalRound, role: ReviewRole
) -> ReviewStageStatus:
    for opinion in round_obj.opinions:
        if opinion.role == role:
            if opinion.decision == ReviewDecision.APPROVED:
                return ReviewStageStatus.APPROVED
            if opinion.decision == ReviewDecision.REJECTED:
                return ReviewStageStatus.REJECTED
            return ReviewStageStatus.RETURNED
    return ReviewStageStatus.PENDING


def _add_event(
    db: Session,
    intent_id: int,
    event_type: str,
    round_no: Optional[int] = None,
    role: Optional[ReviewRole] = None,
    decision: Optional[ReviewDecision] = None,
    detail: Optional[str] = None,
    operator: Optional[str] = None,
) -> None:
    db.add(
        models.ApprovalEvent(
            intent_id=intent_id,
            round_no=round_no,
            event_type=event_type,
            role=role,
            decision=decision,
            detail=detail,
            operator=operator,
        )
    )


def _round_conflict_context(round_obj: models.ApprovalRound) -> dict:
    required = _round_required(round_obj)
    finished = normalize_roles([o.role for o in round_obj.opinions])
    return {
        "round_no": round_obj.round_no,
        "round_status": round_obj.status,
        "config_version": round_obj.config_version,
        "required_roles": [r.value for r in required],
        "finished_roles": [r.value for r in finished],
        "pending_roles": [r.value for r in required if r not in finished],
        "opinions": [
            {
                "role": o.role.value,
                "decision": o.decision.value,
                "reviewer": o.reviewer,
                "submitted_at": o.submitted_at.isoformat(),
            }
            for o in sorted(round_obj.opinions, key=lambda o: o.submitted_at)
        ],
    }


# ---------------------------------------------------------------------------
# 轮次开启（首轮 / 补件后重审）
# ---------------------------------------------------------------------------

def open_review_round(
    intent_id: int,
    operator: Optional[str] = None,
    resubmit_comment: Optional[str] = None,
) -> int:
    """发起首轮审批，或在补件退回后发起重审。返回轮次号。"""

    def _unit(tx: Session) -> int:
        intent = _get_intent_or_404(tx, intent_id)
        config = get_active_config(tx)
        if config is None:
            raise ApprovalFlowError(
                "REVIEW_CONFIG_MISSING",
                "尚未配置生效的必审角色，无法发起分阶段审批",
            )

        latest = get_latest_round(tx, intent_id)
        if latest is not None:
            if latest.status == ReviewRoundStatus.PENDING:
                raise ApprovalFlowError(
                    "REVIEW_ROUND_IN_PROGRESS",
                    f"第 {latest.round_no} 轮审批仍在进行中，无需重复发起",
                    context=_round_conflict_context(latest),
                )
            if latest.status == ReviewRoundStatus.RETURNED:
                if not resubmit_comment or not resubmit_comment.strip():
                    raise ApprovalFlowError(
                        "REVIEW_RESUBMIT_REMARK_REQUIRED",
                        f"第 {latest.round_no} 轮因补件退回已关闭，"
                        "发起重审必须填写补件说明，标明补充了哪些材料",
                        status_code=400,
                    )
            elif latest.status == ReviewRoundStatus.REJECTED:
                raise ApprovalFlowError(
                    "REVIEW_INTENT_REJECTED",
                    f"第 {latest.round_no} 轮已被整单拒绝，该意向不可重审；"
                    "如需合作请重新提交意向",
                    context=_round_conflict_context(latest),
                )
            elif latest.status == ReviewRoundStatus.ALL_APPROVED:
                raise ApprovalFlowError(
                    "REVIEW_ALREADY_APPROVED",
                    f"第 {latest.round_no} 轮必审角色已全部通过，意向已进入洽谈",
                    context=_round_conflict_context(latest),
                )
            round_no = latest.round_no + 1
        else:
            if intent.status not in (IntentStatus.SUBMITTED, IntentStatus.REVIEWING):
                raise ApprovalFlowError(
                    "REVIEW_INTENT_NOT_OPENABLE",
                    f"意向当前状态为「{intent.status.value}」，无法发起审批",
                )
            round_no = 1

        roles = text_to_roles(config.required_roles)
        round_obj = models.ApprovalRound(
            intent_id=intent_id,
            round_no=round_no,
            status=ReviewRoundStatus.PENDING,
            required_roles=roles_to_text(roles),
            config_version=config.version,
            opened_at=datetime.utcnow(),
        )
        tx.add(round_obj)
        tx.flush()

        if intent.status == IntentStatus.SUBMITTED:
            intent.status = IntentStatus.REVIEWING
            if intent.reviewed_at is None:
                intent.reviewed_at = datetime.utcnow()

        if round_no == 1:
            detail = (
                f"发起第 1 轮分阶段审批，必审角色：{roles_to_text(roles)}"
                f"（锁定配置第 {config.version} 版）"
            )
        else:
            detail = (
                f"补件后发起第 {round_no} 轮重审，必审角色："
                f"{roles_to_text(roles)}（锁定配置第 {config.version} 版）；"
                f"补件说明：{resubmit_comment.strip()}"
            )
        _add_event(
            tx,
            intent_id=intent_id,
            event_type="round_opened",
            round_no=round_no,
            detail=detail,
            operator=operator,
        )
        return round_no

    return run_immediate(_unit)


# ---------------------------------------------------------------------------
# 提交角色意见（核心状态机）
# ---------------------------------------------------------------------------

def submit_opinion(
    intent_id: int,
    role: ReviewRole,
    decision: ReviewDecision,
    comment: Optional[str] = None,
    attachment_summary: Optional[str] = None,
    attachments: Optional[List[dict]] = None,
    reviewer: Optional[str] = None,
) -> dict:
    """提交一个角色在当前轮的意见。

    返回 dict 含：opinion_id / round_no / round_status / intent_status /
    pending_roles / finished_roles。
    """

    def _unit(tx: Session) -> dict:
        intent = _get_intent_or_404(tx, intent_id)
        round_obj = get_latest_round(tx, intent_id)
        if round_obj is None:
            raise ApprovalFlowError(
                "REVIEW_ROUND_NOT_OPEN",
                "该意向尚未发起分阶段审批，请先发起第 1 轮审批",
                status_code=400,
            )
        required = _round_required(round_obj)

        if round_obj.status != ReviewRoundStatus.PENDING:
            closed_codes = {
                ReviewRoundStatus.RETURNED: (
                    "REVIEW_ROUND_RETURNED",
                    f"第 {round_obj.round_no} 轮审批已因补件退回关闭，"
                    "不再接收意见；请在补件后发起新一轮重审，往轮意见均已保留可查",
                ),
                ReviewRoundStatus.REJECTED: (
                    "REVIEW_ROUND_REJECTED",
                    f"第 {round_obj.round_no} 轮已整单拒绝，审批终止",
                ),
                ReviewRoundStatus.ALL_APPROVED: (
                    "REVIEW_ROUND_APPROVED",
                    f"第 {round_obj.round_no} 轮必审角色已全部通过，无需重复提交",
                ),
            }
            code, msg = closed_codes[round_obj.status]
            raise ApprovalFlowError(
                code, msg, context=_round_conflict_context(round_obj)
            )

        if role not in required:
            raise ApprovalFlowError(
                "REVIEW_ROLE_NOT_REQUIRED",
                f"角色「{role.value}」不在第 {round_obj.round_no} 轮的必审名单内"
                f"（该轮锁定配置第 {round_obj.config_version} 版："
                f"{roles_to_text(required)}）；配置调整只对之后开启的轮次生效",
                context=_round_conflict_context(round_obj),
            )

        existing = (
            tx.query(models.ApprovalOpinion)
            .filter(
                models.ApprovalOpinion.round_id == round_obj.id,
                models.ApprovalOpinion.role == role,
            )
            .first()
        )
        if existing is not None:
            raise ApprovalFlowError(
                "REVIEW_DUPLICATE",
                f"角色「{role.value}」在第 {round_obj.round_no} 轮已提交过"
                f"「{existing.decision.value}」意见"
                f"（{existing.submitted_at.strftime('%Y-%m-%d %H:%M:%S')}，"
                f"提交人：{existing.reviewer or '未署名'}），不可重复审批",
                context=_round_conflict_context(round_obj),
            )

        now = datetime.utcnow()
        opinion = models.ApprovalOpinion(
            round_id=round_obj.id,
            role=role,
            decision=decision,
            comment=comment,
            attachment_summary=attachment_summary,
            reviewer=reviewer,
            submitted_at=now,
        )
        tx.add(opinion)
        try:
            tx.flush()  # 触发 (round_id, role) 唯一约束
        except IntegrityError:
            # 串行化之外仍可能撞唯一约束（如锁等待超时后强插），
            # 统一翻译为可解释的重复审批冲突而非数据库错误。
            # 此时会话已进入待回滚状态，只能用已载入的标量字段拼最小上下文。
            raise ApprovalFlowError(
                "REVIEW_DUPLICATE",
                f"角色「{role.value}」在第 {round_obj.round_no} 轮的意见已存在，"
                "不可重复审批（并发请求仅有一条生效）",
                context={
                    "round_no": round_obj.round_no,
                    "round_status": round_obj.status,
                    "config_version": round_obj.config_version,
                    "required_roles": [r.value for r in required],
                    "finished_roles": [role.value],
                    "pending_roles": [
                        r.value for r in required if r != role
                    ],
                    "opinions": [],
                },
            )
        for item in attachments or []:
            tx.add(
                models.ApprovalAttachment(
                    opinion_id=opinion.id,
                    file_name=item.get("file_name"),
                    file_size_bytes=item.get("file_size_bytes"),
                    digest=item.get("digest"),
                    summary=item.get("summary"),
                )
            )

        _add_event(
            tx,
            intent_id=intent_id,
            event_type="opinion_submitted",
            round_no=round_obj.round_no,
            role=role,
            decision=decision,
            detail=(
                f"{role.value}审阅结论：{decision.value}。"
                f"意见：{comment or '（无）'}；"
                f"附件摘要：{attachment_summary or '（无）'}"
                + (f"；附件 {len(attachments)} 份" if attachments else "")
            ),
            operator=reviewer,
        )

        decided = {o.role: o for o in round_obj.opinions}
        finished_roles = normalize_roles(list(decided.keys()))
        pending_roles = [r for r in required if r not in decided]

        result = {
            "opinion_id": opinion.id,
            "round_no": round_obj.round_no,
            "pending_roles": pending_roles,
            "finished_roles": finished_roles,
        }

        if decision == ReviewDecision.REJECTED:
            round_obj.status = ReviewRoundStatus.REJECTED
            round_obj.closed_at = now
            round_obj.close_reason = f"{role.value}审阅拒绝整单"
            intent.status = IntentStatus.REJECTED
            if intent.reviewed_at is None:
                intent.reviewed_at = now
            _add_event(
                tx,
                intent_id=intent_id,
                event_type="round_rejected",
                round_no=round_obj.round_no,
                role=role,
                decision=decision,
                detail=(
                    f"第 {round_obj.round_no} 轮审批终止：{role.value}给出拒绝结论，"
                    "整单被拒，其余角色已提交意见全部保留"
                ),
                operator=reviewer,
            )
            result["round_status"] = ReviewRoundStatus.REJECTED
            result["intent_status"] = IntentStatus.REJECTED

        elif decision == ReviewDecision.RETURNED:
            round_obj.status = ReviewRoundStatus.RETURNED
            round_obj.closed_at = now
            round_obj.close_reason = f"{role.value}要求补件"
            if intent.status == IntentStatus.SUBMITTED:
                intent.status = IntentStatus.REVIEWING
                intent.reviewed_at = intent.reviewed_at or now
            _add_event(
                tx,
                intent_id=intent_id,
                event_type="round_returned",
                round_no=round_obj.round_no,
                role=role,
                decision=decision,
                detail=(
                    f"第 {round_obj.round_no} 轮关闭：{role.value}要求补件退回，"
                    "其余角色已提交的意见全部保留；补件完成后可发起第 "
                    f"{round_obj.round_no + 1} 轮重审"
                ),
                operator=reviewer,
            )
            result["round_status"] = ReviewRoundStatus.RETURNED
            result["intent_status"] = intent.status

        else:
            if not pending_roles and all(
                o.decision == ReviewDecision.APPROVED for o in decided.values()
            ):
                round_obj.status = ReviewRoundStatus.ALL_APPROVED
                round_obj.closed_at = now
                round_obj.close_reason = "全部必审角色通过"
                intent.status = IntentStatus.IN_DISCUSSION
                if intent.reviewed_at is None:
                    intent.reviewed_at = now
                _add_event(
                    tx,
                    intent_id=intent_id,
                    event_type="round_approved",
                    round_no=round_obj.round_no,
                    detail=(
                        f"第 {round_obj.round_no} 轮必审角色"
                        f"（{roles_to_text(required)}）全部通过，意向进入洽谈"
                    ),
                    operator=reviewer,
                )
                project = (
                    tx.query(models.Project)
                    .filter(models.Project.id == intent.project_id)
                    .first()
                )
                if (
                    project is not None
                    and project.status == ProjectStatus.ATTRACTING_INVESTMENT
                ):
                    transition_project_status(
                        tx,
                        project=project,
                        to_status=ProjectStatus.NEGOTIATING,
                        operator=reviewer,
                        reason=(
                            f"合作意向第 {round_obj.round_no} 轮分阶段审批"
                            "（投资/法务/园区运营）全部通过，转入洽谈"
                        ),
                        skip_validation=True,
                    )
                result["round_status"] = ReviewRoundStatus.ALL_APPROVED
                result["intent_status"] = IntentStatus.IN_DISCUSSION
            else:
                result["round_status"] = ReviewRoundStatus.PENDING
                result["intent_status"] = intent.status

        return result

    return run_immediate(_unit)


# ---------------------------------------------------------------------------
# 待办：按角色查询
# ---------------------------------------------------------------------------

def list_role_todo(
    db: Session,
    role: ReviewRole,
    skip: int = 0,
    limit: int = 100,
) -> List[dict]:
    """该角色在快照名单内且尚未提交意见的在途审批轮次。"""
    rounds = (
        db.query(models.ApprovalRound)
        .filter(models.ApprovalRound.status == ReviewRoundStatus.PENDING)
        .order_by(models.ApprovalRound.opened_at.asc(), models.ApprovalRound.id.asc())
        .all()
    )
    items: List[dict] = []
    for round_obj in rounds:
        required = _round_required(round_obj)
        if role not in required:
            continue
        if any(o.role == role for o in round_obj.opinions):
            continue
        intent = round_obj.intent
        finished = normalize_roles([o.role for o in round_obj.opinions])
        items.append(
            {
                "intent_id": intent.id,
                "intent_status": intent.status,
                "project_id": intent.project_id,
                "project_name": intent.project.name if intent.project else None,
                "submitter_id": intent.submitter_id,
                "submitter_name": (
                    intent.submitter.name if intent.submitter else None
                ),
                "round_no": round_obj.round_no,
                "config_version": round_obj.config_version,
                "required_roles": required,
                "finished_roles": finished,
                "pending_roles": [r for r in required if r not in finished],
                "opened_at": round_obj.opened_at,
            }
        )
    return items[skip : skip + limit]


# ---------------------------------------------------------------------------
# 轨迹还原
# ---------------------------------------------------------------------------

def build_review_trace(db: Session, intent_id: int) -> Optional[dict]:
    intent = (
        db.query(models.CooperationIntent)
        .filter(models.CooperationIntent.id == intent_id)
        .first()
    )
    if intent is None:
        return None

    rounds = (
        db.query(models.ApprovalRound)
        .filter(models.ApprovalRound.intent_id == intent_id)
        .order_by(models.ApprovalRound.round_no)
        .all()
    )
    events = (
        db.query(models.ApprovalEvent)
        .filter(models.ApprovalEvent.intent_id == intent_id)
        .order_by(models.ApprovalEvent.id)
        .all()
    )
    config = get_active_config(db)

    round_payloads = []
    for round_obj in rounds:
        required = _round_required(round_obj)
        opinions = sorted(round_obj.opinions, key=lambda o: o.submitted_at)
        round_payloads.append(
            {
                "round_no": round_obj.round_no,
                "status": round_obj.status,
                "config_version": round_obj.config_version,
                "required_roles": required,
                "opened_at": round_obj.opened_at,
                "closed_at": round_obj.closed_at,
                "close_reason": round_obj.close_reason,
                "stages": [
                    {"role": r, "status": _stage_status(round_obj, r)}
                    for r in required
                ],
                "opinions": [
                    {
                        "id": o.id,
                        "role": o.role,
                        "decision": o.decision,
                        "comment": o.comment,
                        "attachment_summary": o.attachment_summary,
                        "reviewer": o.reviewer,
                        "submitted_at": o.submitted_at,
                        "attachments": [
                            {
                                "id": a.id,
                                "opinion_id": o.id,
                                "file_name": a.file_name,
                                "file_size_bytes": a.file_size_bytes,
                                "digest": a.digest,
                                "summary": a.summary,
                            }
                            for a in sorted(o.attachments, key=lambda a: a.id)
                        ],
                    }
                    for o in opinions
                ],
            }
        )

    return {
        "intent_id": intent.id,
        "intent_status": intent.status,
        "project_id": intent.project_id,
        "current_config_version": config.version if config else None,
        "current_required_roles": (
            text_to_roles(config.required_roles) if config else []
        ),
        "rounds": round_payloads,
        "events": [
            {
                "id": e.id,
                "round_no": e.round_no,
                "event_type": e.event_type,
                "role": e.role,
                "decision": e.decision,
                "detail": e.detail,
                "operator": e.operator,
                "occurred_at": e.occurred_at,
            }
            for e in events
        ],
    }


def ensure_negotiation_allowed(db: Session, intent_id: int) -> None:
    """洽谈登记准入：必须存在全部通过的审批轮次。

    特性上线前已处于洽谈中/已采纳、且没有任何审批轮次的历史意向，
    视为既有事实予以放行；其余没有审批轨迹的意向一律拦截。
    """
    intent = _get_intent_or_404(db, intent_id)
    approved = get_approved_round(db, intent_id)
    if approved is not None:
        if intent.status not in (IntentStatus.IN_DISCUSSION, IntentStatus.ACCEPTED):
            raise ApprovalFlowError(
                "REVIEW_NEGOTIATION_BLOCKED_STATUS",
                f"意向当前状态为「{intent.status.value}」，不可登记洽谈",
            )
        return

    latest = get_latest_round(db, intent_id)
    if latest is None:
        if intent.status in (IntentStatus.IN_DISCUSSION, IntentStatus.ACCEPTED):
            return  # 上线前已进入洽谈的历史意向，无审批轨迹，放行
        raise ApprovalFlowError(
            "REVIEW_NEGOTIATION_BLOCKED_NO_REVIEW",
            "该意向尚未经过分阶段审批，必审角色全部通过前不可登记洽谈",
            status_code=409,
        )
    if latest.status == ReviewRoundStatus.REJECTED:
        raise ApprovalFlowError(
            "REVIEW_NEGOTIATION_BLOCKED_REJECTED",
            f"意向已在第 {latest.round_no} 轮被整单拒绝，不可登记洽谈",
            context=_round_conflict_context(latest),
        )
    if latest.status == ReviewRoundStatus.RETURNED:
        raise ApprovalFlowError(
            "REVIEW_NEGOTIATION_BLOCKED_PENDING_RESUBMIT",
            f"第 {latest.round_no} 轮已补件退回，需补件重审且全部通过后"
            "才可登记洽谈（往轮意见已保留）",
            status_code=409,
            context=_round_conflict_context(latest),
        )
    pending_ctx = _round_conflict_context(latest)
    pending_text = "、".join(pending_ctx["pending_roles"]) or "（无）"
    raise ApprovalFlowError(
        "REVIEW_NEGOTIATION_BLOCKED_PENDING",
        f"仍有必审角色未完成审阅：{pending_text}；全部通过前不可登记洽谈",
        status_code=409,
        context=pending_ctx,
    )
