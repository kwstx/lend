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
    category: str # sales, subscription, manual
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

class CashFlowSnapshot(BaseTenantModel, table=True):
    """Aggregated state for credit limit calculations"""
    __tablename__ = "cash_flow_snapshots"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    calculated_at: datetime = Field(default_factory=datetime.utcnow)
    total_open_receivables: float
    active_advances_total: float
    available_credit_limit: float
    confidence_score: float

class EventLog(BaseTenantModel, table=True):
    """Immutable log of every state change"""
    __tablename__ = "events_log"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    event_type: str # receivable_created, advance_funded, repayment_processed, etc.
    payload: Dict[str, Any] = Field(sa_column=Column(JSON))
    idempotency_key: str = Field(unique=True, index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    
    customer: Customer = Relationship(back_populates="events")
