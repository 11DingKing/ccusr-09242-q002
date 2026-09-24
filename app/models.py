from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    DateTime,
    ForeignKey,
    Text,
    Date,
    Boolean,
    UniqueConstraint,
    Enum as SAEnum,
)
from sqlalchemy.orm import relationship
from datetime import datetime

from .database import Base
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
)


class Entity(Base):
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    region = Column(SAEnum(Region), nullable=False, index=True)
    country_or_province = Column(String(128), nullable=False)
    city = Column(String(128))
    contact_person = Column(String(64), nullable=False)
    contact_phone = Column(String(32), nullable=False)
    contact_email = Column(String(128))
    address = Column(String(512))
    description = Column(Text)
    registered_capital = Column(Float)
    established_year = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    capabilities = relationship(
        "EntityCapability",
        back_populates="entity",
        cascade="all, delete-orphan",
    )
    submitted_intents = relationship(
        "CooperationIntent",
        foreign_keys="CooperationIntent.submitter_id",
        back_populates="submitter",
    )
    counterparty_intents = relationship(
        "CooperationIntent",
        foreign_keys="CooperationIntent.counterparty_id",
        back_populates="counterparty",
    )


class EntityCapability(Base):
    __tablename__ = "entity_capabilities"

    id = Column(Integer, primary_key=True, index=True)
    entity_id = Column(Integer, ForeignKey("entities.id"), nullable=False)
    category = Column(SAEnum(ProcessingCategory), nullable=False, index=True)
    annual_capacity_tonnes = Column(Float, nullable=False)
    capacity_unit = Column(String(32), default="吨/年")
    production_lines = Column(Integer)
    key_products = Column(String(512))
    certifications = Column(String(512))
    created_at = Column(DateTime, default=datetime.utcnow)

    entity = relationship("Entity", back_populates="capabilities")


class IndustrialPark(Base):
    __tablename__ = "industrial_parks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    park_type = Column(SAEnum(ParkType), nullable=False, index=True)
    city = Column(String(128), nullable=False)
    district = Column(String(128))
    total_area_km2 = Column(Float)
    developed_area_km2 = Column(Float)
    pillar_industries = Column(String(512))
    preferential_policies = Column(Text)
    infrastructure = Column(Text)
    contact_person = Column(String(64))
    contact_phone = Column(String(32))
    address = Column(String(512))
    description = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    projects = relationship("Project", back_populates="park")


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(256), unique=True, nullable=False, index=True)
    project_code = Column(String(64), unique=True, index=True)
    status = Column(
        SAEnum(ProjectStatus),
        nullable=False,
        default=ProjectStatus.ATTRACTING_INVESTMENT,
        index=True,
    )
    investment_direction = Column(Text, nullable=False)
    planned_investment_10k = Column(Float, nullable=False)
    expected_annual_capacity_tonnes = Column(Float)
    planned_land_area_mu = Column(Float)
    expected_output_value_10k = Column(Float)
    expected_jobs = Column(Integer)
    construction_cycle_months = Column(Integer)
    commissioned_date = Column(Date)
    promised_monthly_capacity_tonnes = Column(Float)
    expected_local_procurement_pct = Column(Float)
    park_id = Column(Integer, ForeignKey("industrial_parks.id"), nullable=False)
    initiator_id = Column(Integer, ForeignKey("entities.id"), nullable=False)
    background = Column(Text)
    market_analysis = Column(Text)
    cooperation_modes = Column(String(256))
    support_requirements = Column(Text)
    responsible_department = Column(String(128))
    project_leader = Column(String(64))
    leader_phone = Column(String(32))
    publish_date = Column(Date)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    park = relationship("IndustrialPark", back_populates="projects")
    initiator = relationship("Entity", foreign_keys=[initiator_id])
    categories = relationship(
        "ProjectCategory",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    intents = relationship(
        "CooperationIntent",
        back_populates="project",
        cascade="all, delete-orphan",
    )
    approval = relationship(
        "ProjectApproval",
        back_populates="project",
        uselist=False,
        cascade="all, delete-orphan",
    )
    milestones = relationship(
        "ProjectMilestone",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMilestone.sequence",
    )
    status_logs = relationship(
        "ProjectStatusLog",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectStatusLog.changed_at.desc()",
    )
    capacity_reports = relationship(
        "MonthlyCapacityReport",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="MonthlyCapacityReport.report_year, MonthlyCapacityReport.report_month",
    )
    capacity_follow_ups = relationship(
        "CapacityFollowUp",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="CapacityFollowUp.created_at.desc()",
    )


class ProjectCategory(Base):
    __tablename__ = "project_categories"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    category = Column(SAEnum(ProcessingCategory), nullable=False, index=True)
    proportion = Column(Float)
    description = Column(String(256))

    project = relationship("Project", back_populates="categories")


class CooperationIntent(Base):
    __tablename__ = "cooperation_intents"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    submitter_id = Column(Integer, ForeignKey("entities.id"), nullable=False)
    counterparty_id = Column(Integer, ForeignKey("entities.id"))
    status = Column(
        SAEnum(IntentStatus),
        nullable=False,
        default=IntentStatus.SUBMITTED,
        index=True,
    )
    cooperation_mode = Column(String(128))
    proposed_investment_10k = Column(Float)
    proposed_capacity_tonnes = Column(Float)
    cooperation_content = Column(Text, nullable=False)
    expected_timeline = Column(String(256))
    requirements = Column(Text)
    submitter_comments = Column(Text)
    reviewer = Column(String(64))
    review_comments = Column(Text)
    submitted_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="intents")
    submitter = relationship(
        "Entity",
        foreign_keys=[submitter_id],
        back_populates="submitted_intents",
    )
    counterparty = relationship(
        "Entity",
        foreign_keys=[counterparty_id],
        back_populates="counterparty_intents",
    )
    negotiations = relationship(
        "NegotiationRecord",
        back_populates="intent",
        cascade="all, delete-orphan",
        order_by="NegotiationRecord.round, NegotiationRecord.held_at",
    )
    review_rounds = relationship(
        "ApprovalRound",
        back_populates="intent",
        cascade="all, delete-orphan",
        order_by="ApprovalRound.round_no",
    )
    review_events = relationship(
        "ApprovalEvent",
        back_populates="intent",
        cascade="all, delete-orphan",
        order_by="ApprovalEvent.id",
    )


class NegotiationRecord(Base):
    __tablename__ = "negotiation_records"

    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(Integer, ForeignKey("cooperation_intents.id"), nullable=False)
    round = Column(Integer, nullable=False)
    title = Column(String(256), nullable=False)
    held_at = Column(DateTime, nullable=False)
    location = Column(String(256))
    host = Column(String(128))
    participants = Column(String(512))
    key_topics = Column(Text, nullable=False)
    consensus = Column(Text)
    disagreements = Column(Text)
    next_steps = Column(Text)
    next_meeting_date = Column(Date)
    minutes_author = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)

    intent = relationship("CooperationIntent", back_populates="negotiations")


class ProjectApproval(Base):
    __tablename__ = "project_approvals"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(
        Integer,
        ForeignKey("projects.id"),
        nullable=False,
        unique=True,
    )
    approval_number = Column(String(128), unique=True, nullable=False)
    approval_date = Column(Date, nullable=False)
    approving_authority = Column(String(256), nullable=False)
    agreed_investment_10k = Column(Float, nullable=False)
    agreed_capacity_tonnes = Column(Float)
    agreed_land_area_mu = Column(Float)
    construction_start_deadline = Column(Date)
    completion_deadline = Column(Date)
    main_content = Column(Text)
    approval_conditions = Column(Text)
    approved_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)

    project = relationship("Project", back_populates="approval")


class ProjectMilestone(Base):
    __tablename__ = "project_milestones"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    sequence = Column(Integer, nullable=False)
    milestone_type = Column(SAEnum(MilestoneType), nullable=False)
    name = Column(String(256), nullable=False)
    status = Column(
        SAEnum(MilestoneStatus),
        nullable=False,
        default=MilestoneStatus.NOT_STARTED,
        index=True,
    )
    planned_date = Column(Date, nullable=False)
    actual_date = Column(Date)
    description = Column(Text)
    responsible_person = Column(String(64))
    completion_rate = Column(Float, default=0.0)
    remarks = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="milestones")


class ProjectStatusLog(Base):
    __tablename__ = "project_status_logs"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    from_status = Column(SAEnum(ProjectStatus))
    to_status = Column(SAEnum(ProjectStatus), nullable=False)
    changed_at = Column(DateTime, default=datetime.utcnow)
    operator = Column(String(64))
    reason = Column(String(512))
    remarks = Column(Text)

    project = relationship("Project", back_populates="status_logs")


class MonthlyCapacityReport(Base):
    __tablename__ = "monthly_capacity_reports"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    report_year = Column(Integer, nullable=False, index=True)
    report_month = Column(Integer, nullable=False, index=True)
    actual_output_tonnes = Column(Float, nullable=False, default=0.0)
    capacity_utilization_rate = Column(Float)
    employee_count = Column(Integer, default=0)
    local_material_procurement_10k = Column(Float, default=0.0)
    remarks = Column(Text)
    reported_by = Column(String(64))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="capacity_reports")


class CapacityFollowUp(Base):
    __tablename__ = "capacity_follow_ups"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    report_id = Column(Integer, ForeignKey("monthly_capacity_reports.id"), index=True)
    title = Column(String(256), nullable=False)
    description = Column(Text)
    status = Column(SAEnum(FollowUpStatus), nullable=False, default=FollowUpStatus.PENDING, index=True)
    priority = Column(SAEnum(FollowUpPriority), nullable=False, default=FollowUpPriority.MEDIUM)
    gap_percentage = Column(Float)
    responsible_person = Column(String(64))
    deadline = Column(Date)
    resolution = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    project = relationship("Project", back_populates="capacity_follow_ups")
    report = relationship("MonthlyCapacityReport")


class ApprovalConfig(Base):
    """分阶段审批配置。同一时刻只有一条生效配置（is_active=True）。

    配置变化通过 version 自增留痕；开启新一轮审批时把当版必审角色
    快照到 ApprovalRound，之后配置再改动不影响进行中/已结束的轮次。
    """

    __tablename__ = "approval_configs"

    id = Column(Integer, primary_key=True, index=True)
    version = Column(Integer, nullable=False, unique=True)
    required_roles = Column(String(512), nullable=False)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    change_remark = Column(String(512))
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(String(64))


class ApprovalRound(Base):
    """意向的一轮分阶段审批。required_roles 是开启轮次时的配置快照。"""

    __tablename__ = "approval_rounds"
    __table_args__ = (
        UniqueConstraint("intent_id", "round_no", name="uq_approval_round_intent_round"),
    )

    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(
        Integer, ForeignKey("cooperation_intents.id"), nullable=False, index=True
    )
    round_no = Column(Integer, nullable=False)
    status = Column(
        SAEnum(ReviewRoundStatus),
        nullable=False,
        default=ReviewRoundStatus.PENDING,
        index=True,
    )
    required_roles = Column(String(512), nullable=False)
    config_version = Column(Integer, nullable=False)
    opened_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime)
    close_reason = Column(String(512))

    intent = relationship("CooperationIntent", back_populates="review_rounds")
    opinions = relationship(
        "ApprovalOpinion",
        back_populates="round",
        cascade="all, delete-orphan",
        order_by="ApprovalOpinion.submitted_at",
    )


class ApprovalOpinion(Base):
    """某个角色在某一轮中的独立意见。提交后不可变（重复提交走并发冲突）。

    并发安全不依赖应用锁：写事务统一 BEGIN IMMEDIATE 串行化，
    (round_id, role) 唯一约束兜底，败者重试后读到胜者结果并得到
    可解释的 REVIEW_DUPLICATE 冲突（见 services/approvals.py）。
    """

    __tablename__ = "approval_opinions"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "role", name="uq_approval_opinion_round_role"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    round_id = Column(
        Integer, ForeignKey("approval_rounds.id"), nullable=False, index=True
    )
    role = Column(SAEnum(ReviewRole), nullable=False, index=True)
    decision = Column(SAEnum(ReviewDecision), nullable=False)
    comment = Column(Text)
    attachment_summary = Column(Text)
    reviewer = Column(String(64))
    submitted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    round = relationship("ApprovalRound", back_populates="opinions")
    attachments = relationship(
        "ApprovalAttachment",
        back_populates="opinion",
        cascade="all, delete-orphan",
        order_by="ApprovalAttachment.id",
    )


class ApprovalAttachment(Base):
    """意见携带的附件摘要（名称、大小、摘要说明），不保存文件本体。"""

    __tablename__ = "approval_attachments"

    id = Column(Integer, primary_key=True, index=True)
    opinion_id = Column(
        Integer, ForeignKey("approval_opinions.id"), nullable=False, index=True
    )
    file_name = Column(String(256), nullable=False)
    file_size_bytes = Column(Integer)
    digest = Column(String(128))
    summary = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    opinion = relationship("ApprovalOpinion", back_populates="attachments")


class ApprovalEvent(Base):
    """审批轨迹事件流：开轮/意见/退回/全通过/关闭均追加一条，只增不改。"""

    __tablename__ = "approval_events"

    id = Column(Integer, primary_key=True, index=True)
    intent_id = Column(
        Integer, ForeignKey("cooperation_intents.id"), nullable=False, index=True
    )
    round_no = Column(Integer)
    event_type = Column(String(32), nullable=False)
    role = Column(SAEnum(ReviewRole))
    decision = Column(SAEnum(ReviewDecision))
    detail = Column(Text)
    operator = Column(String(64))
    occurred_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    intent = relationship("CooperationIntent", back_populates="review_events")
