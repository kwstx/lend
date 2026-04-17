import logging
from uuid import UUID
from datetime import datetime
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from src.models.models import (
    EventLog, Transaction, Advance, RepaymentObligation, Customer, Receivable
)
from src.intelligence import CashFlowIntelligence

logger = logging.getLogger(__name__)

class RepaymentProcessor:
    """
    Controlled event processor for managing repayments.
    Processes logged events, classifies revenue, and applies repayments to outstanding obligations.
    """
    def __init__(self, session: AsyncSession):
        self.session = session
        self.eligible_categories = ["sales", "subscription"]

    async def process_pending_events(self):
        """
        Main loop to process all events marked as 'pending'.
        This ensures repayments are handled in a controlled, serializable manner.
        """
        stmt = (
            select(EventLog)
            .where(EventLog.processing_status == "pending")
            .order_by(EventLog.created_at)
        )
        result = await self.session.execute(stmt)
        events = result.scalars().all()
        
        processed_count = 0
        for event in events:
            try:
                # Use a sub-transaction or savepoint for each event if possible, 
                # but here we rely on the main session management.
                await self.process_event(event)
                event.processing_status = "processed"
                processed_count += 1
            except Exception as e:
                logger.error(f"Failed to process event {event.id}: {str(e)}")
                event.processing_status = "failed"
                event.error_message = str(e)
            
            # Commit after each event to ensure idempotency and progress preservation
            await self.session.commit()
        
        return processed_count

    async def process_event(self, event: EventLog):
        """Unified entry point for processing a single event."""
        # Route based on event type
        if "stripe_invoice.payment_succeeded" in event.event_type:
            await self._handle_stripe_invoice_success(event)
        elif "plaid_DEFAULT_UPDATE" in event.event_type:
            await self._handle_plaid_transaction_update(event)
        else:
            # If we don't know how to process it for repayments, we might still mark it as skipped
            event.processing_status = "skipped"

    async def _handle_stripe_invoice_success(self, event: EventLog):
        """Classifies Stripe invoice payment as revenue and triggers repayment logic."""
        payload = event.payload
        invoice = payload.get("data", {}).get("object", {})
        customer_id = event.customer_id
        
        if not invoice:
            raise ValueError("Invalid Stripe payload: missing invoice object")

        # 1. Auditability & Idempotency: Create Transaction record
        # Check if transaction already exists for this event
        tx_stmt = select(Transaction).where(Transaction.metadata["event_id"].astext == str(event.id))
        existing_tx = await self.session.execute(tx_stmt)
        transaction = existing_tx.scalars().first()

        if not transaction:
            transaction = Transaction(
                customer_id=customer_id,
                amount=invoice["amount_paid"] / 100.0,
                type="inflow",
                category="sales",
                timestamp=datetime.fromtimestamp(invoice.get("status_transitions", {}).get("paid_at", datetime.now().timestamp())),
                payer_id=invoice.get("customer"),
                payer_name=invoice.get("customer_name") or invoice.get("customer_email"),
                metadata={"source": "stripe", "invoice_id": invoice.get("id"), "event_id": str(event.id)}
            )
            self.session.add(transaction)
            await self.session.flush() # Ensure we have transaction.id for repayment logs
        
        # 2. Revenue Classification & Repayment
        if self._is_eligible_revenue(transaction):
            await self._apply_repayments(customer_id, transaction)
            
        # 3. Trigger Snapshot Update (ensure metrics reflect new state)
        intel = CashFlowIntelligence(self.session)
        await intel.compute_and_save_snapshot(customer_id)

    async def _handle_plaid_transaction_update(self, event: EventLog):
        """Placeholder for Plaid-driven transactions."""
        # Similar logic to Stripe but adapting to Plaid's payload structure
        # Often involves calling /transactions/get to fetch actual data
        pass

    def _is_eligible_revenue(self, transaction: Transaction) -> bool:
        """Filtering rules to classify incoming funds as eligible revenue."""
        return (
            transaction.type == "inflow" and 
            transaction.category in self.eligible_categories and
            transaction.amount > 0
        )

    async def _apply_repayments(self, customer_id: UUID, transaction: Transaction):
        """
        Applies repayments to outstanding obligations using atomic database logic.
        Repayments are capped per transaction to prevent over-collection.
        """
        # Find active advances
        stmt = (
            select(Advance)
            .where(and_(Advance.customer_id == customer_id, Advance.status == "active"))
            .order_by(Advance.created_at)
        )
        result = await self.session.execute(stmt)
        advances = result.scalars().all()
        
        if not advances:
            return

        for advance in advances:
            # CAP: Take only a percentage of the transaction amount
            # This is the 'repayment rate' (e.g. 15%)
            available_to_collect = transaction.amount * advance.repayment_rate
            
            # Find pending obligations for this advance
            obs_stmt = (
                select(RepaymentObligation)
                .where(and_(
                    RepaymentObligation.advance_id == advance.id,
                    RepaymentObligation.status == "pending"
                ))
                .order_by(RepaymentObligation.due_date)
            )
            obs_result = await self.session.execute(obs_stmt)
            obligations = obs_result.scalars().all()
            
            for obligation in obligations:
                if available_to_collect <= 0:
                    break
                    
                amount_to_apply = min(available_to_collect, obligation.amount)
                
                # Atomic deduction
                obligation.amount -= amount_to_apply
                available_to_collect -= amount_to_apply
                
                if obligation.amount <= 0:
                    obligation.status = "completed"
                    obligation.amount = 0
                
                # Audit trail: Log the repayment application
                repayment_log = EventLog(
                    customer_id=customer_id,
                    event_type="repayment_applied",
                    payload={
                        "advance_id": str(advance.id),
                        "obligation_id": str(obligation.id),
                        "amount_applied": amount_to_apply,
                        "transaction_id": str(transaction.id),
                        "remaining_obligation_balance": obligation.amount
                    },
                    idempotency_key=f"repay_{transaction.id}_{obligation.id}",
                    processing_status="processed"
                )
                self.session.add(repayment_log)
            
            # Check if all obligations for this advance are met
            remaining_total = sum([o.amount for o in obligations])
            if remaining_total <= 0:
                advance.status = "repaid"
                # Release any associated capital reservation (if not already handled)
                # In current models, reservations are already 'committed' or 'released'
