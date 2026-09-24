"""分阶段审批接口：配置、发起/重审、角色意见、角色待办、完整轨迹。"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import schemas
from ..enums import ReviewRole
from ..errors import HTTPStatus
from ..services import approvals as svc
from ..services.approvals import ApprovalFlowError

router = APIRouter()


def _resolve_role(raw: str) -> ReviewRole:
    """路径/查询中的角色既支持中文值（投资）也支持英文名（INVESTMENT）。"""
    by_value = {r.value: r for r in ReviewRole}
    by_name = {r.name: r for r in ReviewRole}
    role = by_value.get(raw) or by_name.get(raw.upper())
    if role is None:
        allowed = "、".join(f"{r.value}（{r.name}）" for r in ReviewRole)
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail={
                "code": "REVIEW_ROLE_UNKNOWN",
                "message": f"未知审批角色「{raw}」，可选：{allowed}",
                "context": None,
            },
        )
    return role


def _raise_flow_error(e: ApprovalFlowError) -> None:
    raise HTTPException(
        status_code=e.status_code,
        detail={
            "code": e.code,
            "message": e.message,
            "context": e.context,
        },
    )


def _config_to_schema(snap: svc.ConfigSnapshot) -> schemas.ApprovalConfigOut:
    return schemas.ApprovalConfigOut(
        id=snap.id,
        version=snap.version,
        required_roles=snap.required_roles,
        is_active=snap.is_active,
        change_remark=snap.change_remark,
        created_by=snap.created_by,
        created_at=snap.created_at,
    )


# ---------------------------------------------------------------------------
# 审批配置（版本化）
# ---------------------------------------------------------------------------

@router.get(
    "/approvals/config",
    response_model=schemas.ApprovalConfigOut,
    summary="查询当前生效的必审角色配置",
)
def get_active_review_config(db: Session = Depends(get_db)):
    config = svc.get_active_config(db)
    if config is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail={
                "code": "REVIEW_CONFIG_MISSING",
                "message": "尚未配置生效的必审角色",
                "context": None,
            },
        )
    return _config_to_schema(svc.ConfigSnapshot(config))


@router.get(
    "/approvals/config/versions",
    response_model=List[schemas.ApprovalConfigOut],
    summary="查询审批配置的全部历史版本",
)
def list_review_config_versions(db: Session = Depends(get_db)):
    return [_config_to_schema(svc.ConfigSnapshot(c)) for c in svc.list_config_versions(db)]


@router.put(
    "/approvals/config",
    response_model=schemas.ApprovalConfigOut,
    status_code=HTTPStatus.CREATED,
    summary="变更必审角色配置（生成新版本，不影响在途/已结束轮次）",
)
def update_review_config(
    body: schemas.ApprovalConfigUpsert,
    db: Session = Depends(get_db),
):
    try:
        snap = svc.create_config_version(
            roles=body.required_roles,
            change_remark=body.change_remark,
            created_by=body.created_by,
        )
    except ApprovalFlowError as e:
        _raise_flow_error(e)
    return _config_to_schema(snap)


# ---------------------------------------------------------------------------
# 角色待办
# ---------------------------------------------------------------------------

@router.get(
    "/approvals/todos",
    response_model=List[schemas.ReviewTodoItem],
    summary="按角色查询待办（该角色在快照名单内且尚未提交意见）",
)
def list_role_review_todos(
    role: str = Query(..., description="审批角色：投资/法务/园区运营 或 INVESTMENT/LEGAL/PARK_OPERATIONS"),
    skip: int = 0,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    review_role = _resolve_role(role)
    return svc.list_role_todo(db, review_role, skip=skip, limit=limit)


# ---------------------------------------------------------------------------
# 发起审批 / 补件重审 / 提交意见 / 轨迹
# ---------------------------------------------------------------------------

@router.post(
    "/workflow/intents/{intent_id}/reviews",
    response_model=schemas.OpenRoundResult,
    status_code=HTTPStatus.CREATED,
    summary="发起第1轮审批，或补件后发起重审",
)
def open_review_round(
    intent_id: int,
    body: schemas.ReviewRoundOpenRequest,
    db: Session = Depends(get_db),
):
    try:
        round_no = svc.open_review_round(
            intent_id=intent_id,
            operator=body.operator,
            resubmit_comment=body.resubmit_comment,
        )
    except ApprovalFlowError as e:
        _raise_flow_error(e)
    # 以新建轮次自身的快照为准组装响应，避免与并发的配置变更混淆
    latest = svc.get_latest_round(db, intent_id)
    return schemas.OpenRoundResult(
        intent_id=intent_id,
        round_no=round_no,
        status=latest.status,
        config_version=latest.config_version,
        required_roles=svc.text_to_roles(latest.required_roles),
    )


@router.post(
    "/workflow/intents/{intent_id}/reviews/roles/{role}/opinions",
    response_model=schemas.SubmitOpinionResult,
    status_code=HTTPStatus.CREATED,
    summary="某一角色提交本轮独立意见（通过/补件退回/拒绝）",
)
def submit_role_opinion(
    intent_id: int,
    role: str,
    body: schemas.ApprovalOpinionSubmit,
    db: Session = Depends(get_db),
):
    review_role = _resolve_role(role)
    try:
        result = svc.submit_opinion(
            intent_id=intent_id,
            role=review_role,
            decision=body.decision,
            comment=body.comment,
            attachment_summary=body.attachment_summary,
            attachments=[a.model_dump() for a in body.attachments],
            reviewer=body.reviewer,
        )
    except ApprovalFlowError as e:
        _raise_flow_error(e)
    return schemas.SubmitOpinionResult.model_validate(result)


@router.get(
    "/workflow/intents/{intent_id}/reviews",
    response_model=schemas.ReviewTraceResponse,
    summary="还原完整审批轨迹（各轮快照、角色意见、附件摘要、事件流）",
)
def get_review_trace(intent_id: int, db: Session = Depends(get_db)):
    trace = svc.build_review_trace(db, intent_id)
    if trace is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail={
                "code": "REVIEW_INTENT_NOT_FOUND",
                "message": "合作意向不存在",
                "context": None,
            },
        )
    return schemas.ReviewTraceResponse.model_validate(trace)
