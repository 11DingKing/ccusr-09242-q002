from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List

from ..database import get_db
from .. import crud, schemas
from ..enums import ReviewRole
from ..errors import HTTPStatus, ERROR_NOT_FOUND
from ..services import intent_review
from ..services.intent_review import ReviewConflict

router = APIRouter(prefix="/workflow/intents", tags=["分阶段审批：投资/法务/园区运营"])


def _raise_conflict(exc: ReviewConflict):
    raise HTTPException(
        status_code=HTTPStatus.CONFLICT,
        detail={
            "code": exc.code,
            "message": exc.message,
            "state": exc.state,
        },
    )


def _ensure_intent(db: Session, intent_id: int):
    intent = crud.get_intent(db, intent_id=intent_id)
    if not intent:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=ERROR_NOT_FOUND["intent"],
        )
    return intent


@router.put(
    "/{intent_id}/review-config",
    response_model=schemas.ReviewConfigView,
    summary="配置意向的必审角色（仅对之后开启的轮次生效，不改写历史）",
)
def update_review_config(
    intent_id: int,
    body: schemas.ReviewConfigUpdate,
    db: Session = Depends(get_db),
):
    _ensure_intent(db, intent_id)
    try:
        intent_review.update_config(
            db,
            intent_id=intent_id,
            required_roles=body.required_roles,
            expected_version=body.expected_version,
        )
    except ReviewConflict as exc:
        _raise_conflict(exc)
    intent = crud.get_intent(db, intent_id=intent_id)
    return intent_review.build_config_view(db, intent)


@router.get(
    "/{intent_id}/review-config",
    response_model=schemas.ReviewConfigView,
    summary="查询意向当前的必审角色配置",
)
def get_review_config(intent_id: int, db: Session = Depends(get_db)):
    intent = _ensure_intent(db, intent_id)
    return intent_review.build_config_view(db, intent)


@router.post(
    "/{intent_id}/reviews/start",
    response_model=schemas.ReviewTrace,
    summary="开启首轮分阶段审批（快照必审角色，意向进入评审中）",
)
def start_review(intent_id: int, db: Session = Depends(get_db)):
    _ensure_intent(db, intent_id)
    try:
        return intent_review.start_review(db, intent_id=intent_id)
    except ReviewConflict as exc:
        _raise_conflict(exc)


@router.post(
    "/{intent_id}/reviews/decisions",
    response_model=schemas.ReviewTrace,
    summary="提交某角色的审批意见（通过/退回补件/拒绝）",
)
def submit_decision(
    intent_id: int,
    body: schemas.ReviewDecisionCreate,
    db: Session = Depends(get_db),
):
    _ensure_intent(db, intent_id)
    try:
        return intent_review.submit_decision(
            db,
            intent_id=intent_id,
            role=body.role,
            decision=body.decision,
            comment=body.comment,
            attachment_summary=body.attachment_summary,
            reviewer=body.reviewer,
            expected_round=body.expected_round,
            expected_version=body.expected_version,
        )
    except ReviewConflict as exc:
        _raise_conflict(exc)


@router.post(
    "/{intent_id}/reviews/resubmit",
    response_model=schemas.ReviewTrace,
    summary="补件后重新提交，开启新一轮审批（历史意见保留）",
)
def resubmit(
    intent_id: int,
    body: schemas.ReviewResubmitRequest,
    db: Session = Depends(get_db),
):
    _ensure_intent(db, intent_id)
    try:
        return intent_review.resubmit(
            db,
            intent_id=intent_id,
            changes=body.model_dump(exclude_unset=True),
        )
    except ReviewConflict as exc:
        _raise_conflict(exc)


@router.get(
    "/reviews/todo",
    response_model=List[schemas.ReviewTodoItem],
    summary="按角色查询审批待办",
)
def list_review_todo(
    role: ReviewRole = Query(..., description="审批角色：投资/法务/园区运营"),
    db: Session = Depends(get_db),
):
    return intent_review.list_todo(db, role=role)


@router.get(
    "/{intent_id}/reviews",
    response_model=schemas.ReviewTrace,
    summary="还原完整审批轨迹（配置、各轮状态、角色意见与时间戳）",
)
def get_review_trace(intent_id: int, db: Session = Depends(get_db)):
    _ensure_intent(db, intent_id)
    trace = intent_review.build_trace(db, intent_id=intent_id)
    return trace
