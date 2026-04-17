import logging
from uuid import UUID
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import stripe
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from src.models.models import (
    Customer, Advance, RepaymentObligation, Transaction, 
    ReconciliationException, EventLog, BaseTenantModel
)
from src.core.database import set_tenant_context
from src.core.observability import AuditLogger
import sentry_sdk

logger = logging.getLogger(__name__)

class ReconciliationService:
    """
    Continuous reconciliation system to ensure internal ledger matches real-world state.
    Detects missing events, data drift, and processing errors.
    """
    def __init__(self, session: AsyncSession):
        self.session = session

    async def run_full_reconciliation(self):
        """Main entry point for the scheduled job."""
        stmt = select(Customer)
        result = await self.session.execute(stmt)
        customers = result.scalars().all()
        
        exceptions_count = 0
        for customer in customers:
            logger.info(f"Starting reconciliation for customer {customer.id}")
            # Ensure isolation context is set
            await set_tenant_context(self.session, str(customer.id))
            
            # 1. Verify internal advance states
            exceptions_count += await self.reconcile_advances(customer.id)
            
            # 2. Verify external Stripe state (if connected)
            if customer.stripe_account_id:
                exceptions_count += await self.reconcile_stripe_state(customer)
            
            await self.session.commit()
            
        return exceptions_count

    async def reconcile_advances(self, customer_id: UUID) -> int:
        """Verifies consistency between Advance status and RepaymentObligation balances."""
        stmt = select(Advance).where(Advance.customer_id == customer_id)
        result = await self.session.execute(stmt)
        advances = result.scalars().all()
        
        exceptions_found = 0
        for advance in advances:
            # Calculate total remaining obligation
            obs_stmt = select(func.sum(RepaymentObligation.amount)).where(
                RepaymentObligation.advance_id == advance.id
            )
            obs_result = await self.session.execute(obs_stmt)
            remaining_balance = obs_result.scalar() or 0.0
            
            # Scenario A: Advance marked as repaid but has remaining balance
            if advance.status == "repaid" and remaining_balance > 0.01:
                await self._log_exception(
                    customer_id=customer_id,
                    exc_type="advance_state_drift",
                    severity="critical",
                    internal={"advance_id": str(advance.id), "status": advance.status, "remaining_balance": remaining_balance},
                    external={},
                    notes="Advance is marked repaid but has non-zero outstanding obligations."
                )
                exceptions_found += 1
            
            # Scenario B: Advance marked as active but balance is zero
            if advance.status == "active" and remaining_balance <= 0:
                await self._log_exception(
                    customer_id=customer_id,
                    exc_type="advance_state_drift",
                    severity="warning",
                    internal={"advance_id": str(advance.id), "status": advance.status, "remaining_balance": remaining_balance},
                    external={},
                    notes="Advance is active but all obligations are cleared. Should be marked as repaid."
                )
                exceptions_found += 1
                
            # Scenario C: Audit trail check
            # Sum of 'repayment_applied' events should match (Total Amount + Fee) - Remaining Balance
            event_stmt = select(func.sum(EventLog.payload["amount_applied"].as_float())).where(
                and_(
                    EventLog.customer_id == customer_id,
                    EventLog.event_type == "repayment_applied",
                    EventLog.advance_id == advance.id
                )
            )
            event_result = await self.session.execute(event_stmt)
            total_applied_events = event_result.scalar() or 0.0
            
            expected_applied = (advance.amount + advance.fee_amount) - remaining_balance
            if abs(total_applied_events - expected_applied) > 0.01:
                await self._log_exception(
                    customer_id=customer_id,
                    exc_type="repayment_ledger_mismatch",
                    severity="critical",
                    internal={
                        "advance_id": str(advance.id),
                        "total_applied_events": total_applied_events,
                        "expected_applied_by_balance": expected_applied,
                        "remaining_balance": remaining_balance
                    },
                    external={},
                    notes="Total repayments applied in audit log does not match current outstanding balance."
                )
                exceptions_found += 1
                
        return exceptions_found

    async def reconcile_stripe_state(self, customer: Customer) -> int:
        """Verifies internal Transactions against Stripe Invoice history."""
        exceptions_found = 0
        try:
            # Fetch recent paid invoices from Stripe
            # In production, we'd use a cursor-based approach or look back a few days
            lookback = int((datetime.now() - timedelta(days=3)).timestamp())
            invoices = stripe.Invoice.list(
                limit=100,
                status='paid',
                created={'gt': lookback},
                # stripe_account=customer.stripe_account_id
            )
            
            for inv in invoices.auto_paging_iter():
                inv_id = inv["id"]
                # Check if we have a transaction for this invoice
                tx_stmt = select(Transaction).where(
                    Transaction.metadata["invoice_id"].astext == inv_id
                )
                tx_result = await self.session.execute(tx_stmt)
                if not tx_result.scalars().first():
                    # Check if there's a pending event for this invoice that hasn't been processed yet
                    event_stmt = select(EventLog).where(
                        and_(
                            EventLog.customer_id == customer.id,
                            EventLog.event_type.like("%stripe_invoice.payment_succeeded%"),
                            # Assuming the event payload contains the invoice ID
                        )
                    )
                    # This is a bit simplified; real logic would deep-check event payload
                    
                    await self._log_exception(
                        customer_id=customer.id,
                        exc_type="missing_external_event",
                        severity="critical",
                        internal={"invoice_id": inv_id},
                        external={"stripe_invoice": inv.to_dict_recursive()},
                        notes="Paid Stripe invoice found with no corresponding internal Transaction record. Event likely missed or failed."
                    )
                    exceptions_found += 1
                    
        except Exception as e:
            logger.error(f"Error fetching Stripe state for {customer.id}: {e}")
            exceptions_found += 1
            
        return exceptions_found

    async def _log_exception(
        self, 
        customer_id: UUID, 
        exc_type: str, 
        severity: str, 
        internal: Dict[str, Any], 
        external: Dict[str, Any],
        notes: str
    ):
        """Writes a mismatch to the exceptions queue/table."""
        # Prevent duplicate exceptions for the same issue (optional, but good for noise reduction)
        # For now, we'll just write it.
        exc = ReconciliationException(
            customer_id=customer_id,
            exception_type=exc_type,
            severity=severity,
            internal_state=internal,
            external_state=external,
            notes=notes
        )
        self.session.add(exc)
        
        # Log to Audit Trail
        await AuditLogger.log_action(
            self.session,
            customer_id=customer_id,
            event_type="reconciliation_exception_detected",
            payload={
                "exception_id": str(exc.id),
                "type": exc_type,
                "severity": severity,
                "notes": notes
            }
        )
        
        # Capture critical issues in Sentry
        if severity == "critical":
            sentry_sdk.capture_message(
                f"Reconciliation Mismatch: {exc_type}",
                level="error",
                extra={"customer_id": str(customer_id), "notes": notes, "internal": internal}
            )

        logger.warning(f"Reconciliation Exception recorded: {exc_type} for customer {customer_id}")

