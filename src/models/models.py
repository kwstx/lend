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
    
    # Relationships
    receivables: List["Receivable"] = Relationship(back_populates="customer")
    advances: List["Advance"] = Relationship(back_populates="customer")
    events: List["EventLog"] = Relationship(back_populates="customer")

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
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    customer: Customer = Relationship(back_populates="advances")
    repayments: List["RepaymentObligation"] = Relationship(back_populates="advance")

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
    metadata: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

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

class EventLog(BaseTenantModel, table=True):
    """Immutable log of every state change"""
    __tablename__ = "events_log"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str # receivable_created, advance_funded, repayment_processed, etc.
    payload: Dict[str, Any] = Field(sa_column=Column(JSON))
    idempotency_key: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    customer: Customer = Relationship(back_populates="events")
