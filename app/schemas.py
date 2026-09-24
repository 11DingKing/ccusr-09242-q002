from pydantic import BaseModel, Field, ConfigDict, BeforeValidator
from datetime import datetime, date
from typing import Optional, List, Annotated

from .enums import (
    Region,
    ProcessingCategory,
    ProjectStatus,
    ParkType,
    IntentStatus,
    MilestoneStatus,
    MilestoneType,
    FollowUpStatus,
    FollowUpPriority,
    ReviewRole,
    ReviewDecision,
    ReviewRoundStatus,
    ReviewStageStatus,
)


def _enum_by_name_or_value(enum_cls):
    """入站枚举宽松解析：同时接受枚举中文名（value）与英文常量名（name）。"""

    def _parse(value):
        if isinstance(value, enum_cls):
            return value
        if isinstance(value, str):
            found = next((m for m in enum_cls if m.value == value), None)
            if found is not None:
                return found
            key = value.strip().upper()
            if key in enum_cls.__members__:
                return enum_cls[key]
        return value

    return BeforeValidator(_parse)


RoleInput = Annotated[ReviewRole, _enum_by_name_or_value(ReviewRole)]
DecisionInput = Annotated[ReviewDecision, _enum_by_name_or_value(ReviewDecision)]


class EntityCapabilityBase(BaseModel):
    category: ProcessingCategory
    annual_capacity_tonnes: float
    capacity_unit: Optional[str] = "吨/年"
    production_lines: Optional[int] = None
    key_products: Optional[str] = None
    certifications: Optional[str] = None


class EntityCapabilityCreate(EntityCapabilityBase):
    pass


class EntityCapability(EntityCapabilityBase):
    id: int
    entity_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EntityBase(BaseModel):
    name: str
    region: Region
    country_or_province: str
    city: Optional[str] = None
    contact_person: str
    contact_phone: str
    contact_email: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    registered_capital: Optional[float] = None
    established_year: Optional[int] = None


class EntityCreate(EntityBase):
    capabilities: List[EntityCapabilityCreate] = Field(default_factory=list)


class EntityUpdate(BaseModel):
    name: Optional[str] = None
    region: Optional[Region] = None
    country_or_province: Optional[str] = None
    city: Optional[str] = None
    contact_person: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_email: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None
    registered_capital: Optional[float] = None
    established_year: Optional[int] = None


class Entity(EntityBase):
    id: int
    created_at: datetime
    updated_at: datetime
    capabilities: List[EntityCapability] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class EntityListItem(BaseModel):
    id: int
    name: str
    region: Region
    country_or_province: str
    city: Optional[str] = None
    contact_person: str
    registered_capital: Optional[float] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class IndustrialParkBase(BaseModel):
    name: str
    park_type: ParkType
    city: str
    district: Optional[str] = None
    total_area_km2: Optional[float] = None
    developed_area_km2: Optional[float] = None
    pillar_industries: Optional[str] = None
    preferential_policies: Optional[str] = None
    infrastructure: Optional[str] = None
    contact_person: Optional[str] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None


class IndustrialParkCreate(IndustrialParkBase):
    pass


class IndustrialParkUpdate(BaseModel):
    name: Optional[str] = None
    park_type: Optional[ParkType] = None
    city: Optional[str] = None
    district: Optional[str] = None
    total_area_km2: Optional[float] = None
    developed_area_km2: Optional[float] = None
    pillar_industries: Optional[str] = None
    preferential_policies: Optional[str] = None
    infrastructure: Optional[str] = None
    contact_person: Optional[str] = None
    contact_phone: Optional[str] = None
    address: Optional[str] = None
    description: Optional[str] = None


class IndustrialPark(IndustrialParkBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectCategoryBase(BaseModel):
    category: ProcessingCategory
    proportion: Optional[float] = None
    description: Optional[str] = None


class ProjectCategoryCreate(ProjectCategoryBase):
    pass


class ProjectCategory(ProjectCategoryBase):
    id: int
    project_id: int

    model_config = ConfigDict(from_attributes=True)


class ProjectBase(BaseModel):
    name: str
    project_code: Optional[str] = None
    status: ProjectStatus = ProjectStatus.ATTRACTING_INVESTMENT
    investment_direction: str
    planned_investment_10k: float
    expected_annual_capacity_tonnes: Optional[float] = None
    planned_land_area_mu: Optional[float] = None
    expected_output_value_10k: Optional[float] = None
    expected_jobs: Optional[int] = None
    construction_cycle_months: Optional[int] = None
    commissioned_date: Optional[date] = None
    promised_monthly_capacity_tonnes: Optional[float] = None
    expected_local_procurement_pct: Optional[float] = None
    park_id: int
    initiator_id: int
    background: Optional[str] = None
    market_analysis: Optional[str] = None
    cooperation_modes: Optional[str] = None
    support_requirements: Optional[str] = None
    responsible_department: Optional[str] = None
    project_leader: Optional[str] = None
    leader_phone: Optional[str] = None
    publish_date: Optional[date] = None


class ProjectCreate(ProjectBase):
    categories: List[ProjectCategoryCreate] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    project_code: Optional[str] = None
    investment_direction: Optional[str] = None
    planned_investment_10k: Optional[float] = None
    expected_annual_capacity_tonnes: Optional[float] = None
    planned_land_area_mu: Optional[float] = None
    expected_output_value_10k: Optional[float] = None
    expected_jobs: Optional[int] = None
    construction_cycle_months: Optional[int] = None
    commissioned_date: Optional[date] = None
    promised_monthly_capacity_tonnes: Optional[float] = None
    expected_local_procurement_pct: Optional[float] = None
    park_id: Optional[int] = None
    background: Optional[str] = None
    market_analysis: Optional[str] = None
    cooperation_modes: Optional[str] = None
    support_requirements: Optional[str] = None
    responsible_department: Optional[str] = None
    project_leader: Optional[str] = None
    leader_phone: Optional[str] = None
    publish_date: Optional[date] = None


class ProjectListItem(BaseModel):
    id: int
    name: str
    project_code: Optional[str] = None
    status: ProjectStatus
    planned_investment_10k: float
    expected_annual_capacity_tonnes: Optional[float] = None
    park_id: int
    park_name: Optional[str] = None
    initiator_name: Optional[str] = None
    publish_date: Optional[date] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class Project(ProjectBase):
    id: int
    created_at: datetime
    updated_at: datetime
    categories: List[ProjectCategory] = Field(default_factory=list)
    park: Optional[IndustrialPark] = None
    initiator: Optional[EntityListItem] = None
    capacity_reports: List["MonthlyCapacityReport"] = Field(default_factory=list)
    capacity_follow_ups: List["CapacityFollowUp"] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class StatusChangeRequest(BaseModel):
    to_status: ProjectStatus
    operator: Optional[str] = None
    reason: str = Field(..., min_length=1, description="状态变更原因，退回时必填")
    remarks: Optional[str] = None


class ProjectStatusLogBase(BaseModel):
    from_status: Optional[ProjectStatus] = None
    to_status: ProjectStatus
    operator: Optional[str] = None
    reason: Optional[str] = None
    remarks: Optional[str] = None


class ProjectStatusLog(ProjectStatusLogBase):
    id: int
    project_id: int
    changed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NegotiationRecordBase(BaseModel):
    round: int
    title: str
    held_at: datetime
    location: Optional[str] = None
    host: Optional[str] = None
    participants: Optional[str] = None
    key_topics: str
    consensus: Optional[str] = None
    disagreements: Optional[str] = None
    next_steps: Optional[str] = None
    next_meeting_date: Optional[date] = None
    minutes_author: Optional[str] = None


class NegotiationRecordCreate(NegotiationRecordBase):
    pass


class NegotiationRecord(NegotiationRecordBase):
    id: int
    intent_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CooperationIntentBase(BaseModel):
    project_id: int
    submitter_id: int
    counterparty_id: Optional[int] = None
    cooperation_mode: Optional[str] = None
    proposed_investment_10k: Optional[float] = None
    proposed_capacity_tonnes: Optional[float] = None
    cooperation_content: str
    expected_timeline: Optional[str] = None
    requirements: Optional[str] = None
    submitter_comments: Optional[str] = None


class CooperationIntentCreate(CooperationIntentBase):
    pass


class CooperationIntentUpdate(BaseModel):
    status: Optional[IntentStatus] = None
    cooperation_mode: Optional[str] = None
    proposed_investment_10k: Optional[float] = None
    proposed_capacity_tonnes: Optional[float] = None
    cooperation_content: Optional[str] = None
    expected_timeline: Optional[str] = None
    requirements: Optional[str] = None
    submitter_comments: Optional[str] = None
    reviewer: Optional[str] = None
    review_comments: Optional[str] = None


class CooperationIntentListItem(BaseModel):
    id: int
    project_id: int
    project_name: Optional[str] = None
    submitter_id: int
    submitter_name: Optional[str] = None
    counterparty_id: Optional[int] = None
    counterparty_name: Optional[str] = None
    status: IntentStatus
    cooperation_mode: Optional[str] = None
    proposed_investment_10k: Optional[float] = None
    submitted_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CooperationIntent(CooperationIntentBase):
    id: int
    status: IntentStatus
    reviewer: Optional[str] = None
    review_comments: Optional[str] = None
    submitted_at: datetime
    reviewed_at: Optional[datetime] = None
    updated_at: datetime
    project: Optional[ProjectListItem] = None
    submitter: Optional[EntityListItem] = None
    counterparty: Optional[EntityListItem] = None
    negotiations: List[NegotiationRecord] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ProjectApprovalBase(BaseModel):
    approval_number: str
    approval_date: date
    approving_authority: str
    agreed_investment_10k: float
    agreed_capacity_tonnes: Optional[float] = None
    agreed_land_area_mu: Optional[float] = None
    construction_start_deadline: Optional[date] = None
    completion_deadline: Optional[date] = None
    main_content: Optional[str] = None
    approval_conditions: Optional[str] = None
    approved_by: Optional[str] = None


class ProjectApprovalCreate(ProjectApprovalBase):
    project_id: int


class ProjectApproval(ProjectApprovalBase):
    id: int
    project_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ProjectApprovalRequest(BaseModel):
    approval_number: str
    approval_date: date
    approving_authority: str
    agreed_investment_10k: float
    agreed_capacity_tonnes: Optional[float] = None
    agreed_land_area_mu: Optional[float] = None
    construction_start_deadline: Optional[date] = None
    completion_deadline: Optional[date] = None
    main_content: Optional[str] = None
    approval_conditions: Optional[str] = None
    approved_by: Optional[str] = None
    operator: Optional[str] = None
    milestones: List["MilestoneCreate"] = Field(default_factory=list)


class ProjectMilestoneBase(BaseModel):
    sequence: int
    milestone_type: MilestoneType
    name: str
    status: MilestoneStatus = MilestoneStatus.NOT_STARTED
    planned_date: date
    actual_date: Optional[date] = None
    description: Optional[str] = None
    responsible_person: Optional[str] = None
    completion_rate: float = 0.0
    remarks: Optional[str] = None


class MilestoneCreate(ProjectMilestoneBase):
    pass


class ProjectMilestoneCreate(ProjectMilestoneBase):
    project_id: int


class ProjectMilestoneUpdate(BaseModel):
    status: Optional[MilestoneStatus] = None
    actual_date: Optional[date] = None
    completion_rate: Optional[float] = None
    description: Optional[str] = None
    responsible_person: Optional[str] = None
    remarks: Optional[str] = None


class ProjectMilestone(ProjectMilestoneBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ParkStatistics(BaseModel):
    park_id: int
    park_name: str
    park_type: ParkType
    total_projects: int
    established_projects: int
    under_construction_projects: int
    commissioned_projects: int
    attracting_projects: int
    negotiating_projects: int
    total_agreed_investment_10k: float
    total_planned_investment_10k: float
    commission_rate: float


class CategoryStatistics(BaseModel):
    category: ProcessingCategory
    project_count: int
    total_investment_10k: float
    total_capacity_tonnes: float


class OverallStatistics(BaseModel):
    total_projects: int
    attracting_investment_count: int
    negotiating_count: int
    established_count: int
    under_construction_count: int
    commissioned_count: int
    total_planned_investment_10k: float
    total_agreed_investment_10k: float
    total_expected_output_value_10k: float
    total_expected_jobs: int
    commission_rate: float
    parks: List[ParkStatistics]
    categories: List[CategoryStatistics]


class MonthlyCapacityReportBase(BaseModel):
    report_year: int
    report_month: int
    actual_output_tonnes: float = 0.0
    capacity_utilization_rate: Optional[float] = None
    employee_count: Optional[int] = 0
    local_material_procurement_10k: Optional[float] = 0.0
    remarks: Optional[str] = None
    reported_by: Optional[str] = None


class MonthlyCapacityReportCreate(MonthlyCapacityReportBase):
    project_id: int


class MonthlyCapacityReportUpdate(BaseModel):
    actual_output_tonnes: Optional[float] = None
    capacity_utilization_rate: Optional[float] = None
    employee_count: Optional[int] = None
    local_material_procurement_10k: Optional[float] = None
    remarks: Optional[str] = None
    reported_by: Optional[str] = None


class MonthlyCapacityReport(MonthlyCapacityReportBase):
    id: int
    project_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CapacityFollowUpBase(BaseModel):
    title: str
    description: Optional[str] = None
    status: FollowUpStatus = FollowUpStatus.PENDING
    priority: FollowUpPriority = FollowUpPriority.MEDIUM
    gap_percentage: Optional[float] = None
    responsible_person: Optional[str] = None
    deadline: Optional[date] = None
    resolution: Optional[str] = None


class CapacityFollowUpCreate(CapacityFollowUpBase):
    project_id: int
    report_id: Optional[int] = None


class CapacityFollowUpUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[FollowUpStatus] = None
    priority: Optional[FollowUpPriority] = None
    gap_percentage: Optional[float] = None
    responsible_person: Optional[str] = None
    deadline: Optional[date] = None
    resolution: Optional[str] = None


class CapacityFollowUp(CapacityFollowUpBase):
    id: int
    project_id: int
    report_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CapacityCurvePoint(BaseModel):
    period: str
    year: int
    month: int
    promised_capacity_tonnes: float
    actual_output_tonnes: float
    utilization_rate: float


class CapacityCurveResponse(BaseModel):
    project_id: int
    project_name: str
    promised_monthly_capacity_tonnes: Optional[float]
    curve: List[CapacityCurvePoint]


class ParkCapacityStatistics(BaseModel):
    park_id: int
    park_name: str
    park_type: ParkType
    commissioned_projects: int
    reporting_projects: int
    total_promised_capacity_tonnes: float
    total_actual_output_tonnes: float
    average_utilization_rate: float
    total_local_procurement_10k: float


class CategoryCapacityStatistics(BaseModel):
    category: ProcessingCategory
    project_count: int
    total_promised_capacity_tonnes: float
    total_actual_output_tonnes: float
    average_utilization_rate: float
    total_local_procurement_10k: float


class CapacityOverviewStatistics(BaseModel):
    total_commissioned_projects: int
    total_reporting_projects: int
    total_promised_capacity_tonnes: float
    total_actual_output_tonnes: float
    overall_utilization_rate: float
    total_local_procurement_10k: float
    parks: List[ParkCapacityStatistics]
    categories: List[CategoryCapacityStatistics]


Project.model_rebuild()


# ---------------------------------------------------------------------------
# 分阶段审批
# ---------------------------------------------------------------------------

class ApprovalAttachmentBase(BaseModel):
    file_name: str = Field(..., min_length=1, description="附件名称")
    file_size_bytes: Optional[int] = Field(None, ge=0)
    digest: Optional[str] = Field(None, max_length=128, description="附件摘要指纹")
    summary: Optional[str] = Field(None, description="附件内容摘要说明")


class ApprovalAttachmentCreate(ApprovalAttachmentBase):
    pass


class ApprovalAttachment(ApprovalAttachmentBase):
    id: int
    opinion_id: int

    model_config = ConfigDict(from_attributes=True)


class ApprovalOpinionSubmit(BaseModel):
    decision: DecisionInput = Field(..., description="通过 / 补件退回 / 拒绝")
    comment: Optional[str] = Field(None, description="角色独立意见")
    attachment_summary: Optional[str] = Field(
        None, description="附件总体摘要（文本，区别于每份附件的 summary）"
    )
    attachments: List[ApprovalAttachmentCreate] = Field(default_factory=list)
    reviewer: Optional[str] = Field(None, max_length=64)


class ReviewRoundOpenRequest(BaseModel):
    resubmit_comment: Optional[str] = Field(
        None,
        description="非首轮时必填：本次补件补充了哪些材料/说明",
    )
    operator: Optional[str] = Field(None, max_length=64)


class ApprovalConfigUpsert(BaseModel):
    required_roles: List[RoleInput] = Field(
        ...,
        min_length=1,
        description="必审角色列表，保存时按固定顺序去重",
    )
    change_remark: Optional[str] = Field(None, description="本次配置变更说明")
    created_by: Optional[str] = Field(None, max_length=64)


class ApprovalConfigOut(BaseModel):
    id: int
    version: int
    required_roles: List[ReviewRole]
    is_active: bool
    change_remark: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReviewStageOut(BaseModel):
    role: ReviewRole
    status: ReviewStageStatus


class ApprovalOpinionOut(BaseModel):
    id: int
    role: ReviewRole
    decision: ReviewDecision
    comment: Optional[str] = None
    attachment_summary: Optional[str] = None
    reviewer: Optional[str] = None
    submitted_at: datetime
    attachments: List[ApprovalAttachment] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class ReviewRoundOut(BaseModel):
    round_no: int
    status: ReviewRoundStatus
    config_version: int
    required_roles: List[ReviewRole]
    opened_at: datetime
    closed_at: Optional[datetime] = None
    close_reason: Optional[str] = None
    stages: List[ReviewStageOut] = Field(default_factory=list)
    opinions: List[ApprovalOpinionOut] = Field(default_factory=list)


class ReviewTodoItem(BaseModel):
    intent_id: int
    intent_status: IntentStatus
    project_id: int
    project_name: Optional[str] = None
    submitter_id: int
    submitter_name: Optional[str] = None
    round_no: int
    config_version: int
    required_roles: List[ReviewRole]
    finished_roles: List[ReviewRole] = Field(default_factory=list)
    pending_roles: List[ReviewRole] = Field(default_factory=list)
    opened_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApprovalEventOut(BaseModel):
    id: int
    round_no: Optional[int] = None
    event_type: str
    role: Optional[ReviewRole] = None
    decision: Optional[ReviewDecision] = None
    detail: Optional[str] = None
    operator: Optional[str] = None
    occurred_at: datetime


class ReviewTraceResponse(BaseModel):
    intent_id: int
    intent_status: IntentStatus
    project_id: int
    current_config_version: Optional[int] = None
    current_required_roles: List[ReviewRole] = Field(default_factory=list)
    rounds: List[ReviewRoundOut] = Field(default_factory=list)
    events: List[ApprovalEventOut] = Field(default_factory=list)


class SubmitOpinionResult(BaseModel):
    opinion_id: int
    round_no: int
    round_status: ReviewRoundStatus
    intent_status: IntentStatus
    pending_roles: List[ReviewRole] = Field(default_factory=list)
    finished_roles: List[ReviewRole] = Field(default_factory=list)


class OpenRoundResult(BaseModel):
    intent_id: int
    round_no: int
    status: ReviewRoundStatus = ReviewRoundStatus.PENDING
    config_version: int
    required_roles: List[ReviewRole] = Field(default_factory=list)
