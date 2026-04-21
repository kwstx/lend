from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID, uuid4
from sqlmodel import SQLModel, Field, Relationship, JSON, Column

class BaseTenantModel(SQLModel):
    customer_id: UUID = Field(index=True)

class Customer(SQLModel, table=True):
    __tablename__ = "customers"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    email: str = Field(unique=True, index=True)
    stripe_account_id: Optional[str] = Field(default=None, index=True)
    plaid_item_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

    # Compliance & Verification (Free/Manual Approach)
    verification_status: str = Field(default="unverified") # unverified, pending, verified, rejected
    is_sanction_cleared: bool = Field(default=False)
    tax_id: Optional[str] = Field(default=None)
    business_registration_number: Optional[str] = Field(default=None)
    verification_metadata: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_compliance_check_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = Field(default_factory=datetime.utcnow) # Added for stale-data killswitch

    # Relationships
    receivables: List["Receivable"] = Relationship(back_populates="customer")
    advances: List["Advance"] = Relationship(back_populates="customer")
    events: List["EventLog"] = Relationship(back_populates="customer")
    beneficial_owners: List["BeneficialOwner"] = Relationship(back_populates="customer")

class BeneficialOwner(BaseTenantModel, table=True):
    """Manual tracking of business owners for KYC/AML."""
    __tablename__ = "beneficial_owners"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    full_name: str
    email: str
    verification_status: str = Field(default="unverified") 
    is_sanction_cleared: bool = Field(default=False)
    ownership_percentage: float
    created_at: datetime = Field(default_factory=datetime.utcnow)

    customer: Customer = Relationship(back_populates="beneficial_owners")

class Receivable(BaseTenantModel, table=True):
    __tablename__ = "receivables"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    external_id: str = Field(index=True)  # ID from source system (e.g. Stripe, Quickbooks)
    amount: float
    currency: str = Field(default="USD")
    due_date: datetime
    status: str = Field(default="pending") # pending, paid, cancelled
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    customer: Customer = Relationship(back_populates="receivables")

class Advance(BaseTenantModel, table=True):
    __tablename__ = "advances"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    amount: float
    fee_amount: float
    status: str = Field(default="active") # active, repaid, defaulted
    repayment_rate: float = Field(default=0.15) # 15% of eligible revenue
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    customer: Customer = Relationship(back_populates="advances")
    repayments: List["RepaymentObligation"] = Relationship(back_populates="advance")
    
    # Capital Source Link
    capital_reservation_id: Optional[UUID] = Field(default=None, foreign_key="capital_reservations.id")
    capital_reservation: Optional["CapitalReservation"] = Relationship(back_populates="advance")

class RepaymentObligation(BaseTenantModel, table=True):
    __tablename__ = "repayment_obligations"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    advance_id: UUID = Field(foreign_key="advances.id")
    amount: float
    status: str = Field(default="pending") # pending, completed
    due_date: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    advance: Advance = Relationship(back_populates="repayments")

class Transaction(BaseTenantModel, table=True):
    """Real-time cash flow data (inflows/outflows)"""
    __tablename__ = "transactions"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    amount: float
    type: str # inflow, outflow
    category: str # sales, subscription, manual, transfer, refund
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    payer_id: Optional[str] = Field(default=None, index=True) # External payer ID (e.g. Stripe Customer ID)
    payer_name: Optional[str] = Field(default=None, index=True)
    context_data: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class CashFlowSnapshot(BaseTenantModel, table=True):
    """Aggregated state for credit limit calculations (versioned)"""
    __tablename__ = "cash_flow_snapshots"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    calculated_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    
    # Trailing Revenue
    trailing_revenue_30d: float = Field(default=0.0)
    trailing_revenue_90d: float = Field(default=0.0)
    
    # Revenue Stability (Coefficient of Variation: std_dev / mean)
    # 0.0 is perfect stability, higher is more volatile
    revenue_stability_score: float = Field(default=0.0)
    
    # Concentration Risk (Percentage of revenue from top payer in last 90d)
    concentration_risk_score: float = Field(default=0.0)
    
    # Inflow Classification (Last 30d)
    true_revenue_inflow_30d: float = Field(default=0.0)
    other_inflow_30d: float = Field(default=0.0) # transfers, refunds, etc.
    
    # Core Liquidity
    total_open_receivables: float = Field(default=0.0)
    active_advances_total: float = Field(default=0.0)
    available_credit_limit: float = Field(default=0.0)
    
    # Metadata for reconstruction
    calculation_version: str = Field(default="v1")
    confidence_score: float = Field(default=1.0)
    
    # Risk Evaluation Results
    is_eligible: bool = Field(default=True)
    rejection_reasons: Optional[List[str]] = Field(default=None, sa_column=Column(JSON))
    policy_version: Optional[str] = Field(default=None)
    risk_evaluation_metadata: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))

class EventLog(BaseTenantModel, table=True):
    """Immutable log of every state change"""
    __tablename__ = "events_log"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    advance_id: Optional[UUID] = Field(default=None, index=True)
    event_type: str # receivable_created, advance_funded, repayment_processed, etc.
    payload: Dict[str, Any] = Field(sa_column=Column(JSON))
    idempotency_key: str = Field(unique=True, index=True)
    processing_status: str = Field(default="pending", index=True) # pending, processed, failed, skipped
    error_message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    customer: Customer = Relationship(back_populates="events")

class CapitalSource(SQLModel, table=True):
    __tablename__ = "capital_sources"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str # Partner Lender, Treasury, Internal Balance Sheet
    type: str # partner_api, treasury, internal_pool
    available_amount: float = Field(default=0.0)
    total_capacity: float = Field(default=0.0)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    # Relationships
    reservations: List["CapitalReservation"] = Relationship(back_populates="source")

class CapitalReservation(BaseTenantModel, table=True):
    __tablename__ = "capital_reservations"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    source_id: UUID = Field(foreign_key="capital_sources.id")
    advance_id: Optional[UUID] = Field(default=None) # Set after advance is created
    amount: float
    status: str = Field(default="reserved") # reserved, committed, released
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    source: CapitalSource = Relationship(back_populates="reservations")
    advance: Optional["Advance"] = Relationship(back_populates="capital_reservation")

class FinancingOffer(BaseTenantModel, table=True):
    __tablename__ = "financing_offers"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    snapshot_id: UUID = Field(foreign_key="cash_flow_snapshots.id")
    amount: float
    fee_amount: float
    status: str = Field(default="pending") # pending, accepted, rejected, expired, funding_queued
    expires_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)

class FundingQueue(BaseTenantModel, table=True):
    """Everything staged for approval and capital reservation before payout."""
    __tablename__ = "funding_queue"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    offer_id: UUID = Field(foreign_key="financing_offers.id")
    reservation_id: UUID = Field(foreign_key="capital_reservations.id")
    status: str = Field(default="staged_for_approval") # staged_for_approval, approved, rejected, paid
    
    # HITL Approval tracking
    reviewer_id: Optional[str] = Field(default=None)
    reviewed_at: Optional[datetime] = Field(default=None)
    rejection_reason: Optional[str] = Field(default=None)
    reviewer_notes: Optional[str] = Field(default=None)

    created_at: datetime = Field(default_factory=datetime.utcnow)

class ReconciliationException(BaseTenantModel, table=True):
    """Logs mismatches between internal ledger and external provider state for manual review."""
    __tablename__ = "reconciliation_exceptions"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    exception_type: str # balance_mismatch, missing_repayment, external_record_mismatch
    severity: str = Field(default="critical") # critical, warning
    
    internal_state: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    external_state: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    
    resolution_status: str = Field(default="unresolved") # unresolved, resolved, ignored
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    notes: Optional[str] = None
    
    created_at: datetime = Field(default_factory=datetime.utcnow)

class SystemConfig(SQLModel, table=True):
    """Global system state and kill switches."""
    __tablename__ = "system_configs"
    id: int = Field(default=1, primary_key=True)
    
    # Financial Kill Switches
    underwriting_frozen: bool = Field(default=False)
    fund_deployment_paused: bool = Field(default=False)
    repayments_paused: bool = Field(default=False)
    
    # Simulation Mode
    simulation_mode: bool = Field(default=True) # Defaults to True for safety (sandbox first)
    
    # Global Limits
    max_global_daily_deployment: float = Field(default=1000000.0)
    current_daily_deployment: float = Field(default=0.0)
    
    # Pilot Mode Caps (Strict exposure caps per customer and per day)
    per_customer_exposure_cap: float = Field(default=5000.0) # $5k limit for pilot customers
    daily_exposure_cap: float = Field(default=50000.0) # $50k global daily limit in pilot mode
    
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    updated_by: Optional[str] = None

class TenantApiKey(SQLModel, table=True):
    """API Keys for tenant authentication."""
    __tablename__ = "tenant_api_keys"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="customers.id", index=True)
    hashed_key: str = Field(index=True)
    prefix: str # First 8 chars for identification
    name: str # e.g. "Production Key"
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: Optional[datetime] = None

class AdminUser(SQLModel, table=True):
    """Internal users with RBAC."""
    __tablename__ = "admin_users"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    email: str = Field(unique=True, index=True)
    hashed_password: str
    role: str = Field(default="viewer") # viewer, operations, admin, super_admin
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)

