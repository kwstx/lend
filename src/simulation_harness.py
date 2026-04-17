import asyncio
import uuid
import random
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.database import async_session, init_db
from src.models.models import (
    Customer, SystemConfig, CapitalSource, EventLog, 
    Transaction, CashFlowSnapshot, Advance, RepaymentObligation,
    FundingQueue, FinancingOffer
)
from src.services.advance_service import AdvanceService
from src.services.repayment_processor import RepaymentProcessor
from src.services.reconciliation_service import ReconciliationService
from src.intelligence import CashFlowIntelligence
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("simulation_harness")

async def run_simulation():
    async with async_session() as session:
        logger.info("--- STARTING END-TO-END FUNDING CYCLE SIMULATION ---")
        
        # 1. Setup System Config for Simulation
        config = await session.get(SystemConfig, 1)
        if not config:
            config = SystemConfig(id=1, simulation_mode=True)
            session.add(config)
        else:
            config.simulation_mode = True
        
        from sqlalchemy import select
        
        source_result = await session.execute(select(CapitalSource).where(CapitalSource.name == "Simulation Pool"))
        source = source_result.scalars().first()
        if not source:
            source = CapitalSource(
                name="Simulation Pool",
                type="internal_pool",
                available_amount=100000.0,
                total_capacity=100000.0,
                is_active=True
            )
            session.add(source)
        
        await session.commit()
        
        # 2. Setup Test Customer
        customer_id = uuid.uuid4()
        customer = Customer(
            id=customer_id,
            name="Simulated Biz Inc.",
            email=f"test_{customer_id.hex[:8]}@example.com",
            stripe_account_id=f"acct_sim_{uuid.uuid4().hex[:10]}",
            plaid_item_id=f"item_sim_{uuid.uuid4().hex[:10]}"
        )
        session.add(customer)
        await session.commit()
        logger.info(f"Created Test Customer: {customer.name} ({customer.id})")

        # 3. Simulate Historical Cash Flow (Inflows)
        # Create 10 transactions over the last 30 days
        for i in range(10):
            tx = Transaction(
                customer_id=customer_id,
                amount=random.uniform(1000, 5000),
                type="inflow",
                category="sales",
                timestamp=datetime.utcnow() - timedelta(days=random.randint(1, 45)),
                context_data={"source": "stripe_sim"}
            )
            session.add(tx)
        
        await session.commit()
        logger.info("Simulated historical transactions.")

        # 4. Compute Initial Snapshot
        intel = CashFlowIntelligence(session)
        await intel.compute_and_save_snapshot(customer_id)
        logger.info("Computed initial cash flow snapshot.")

        # 5. Request Financing
        advance_service = AdvanceService(session)
        offer = await advance_service.create_financing_offer(customer_id, 2000.0)
        logger.info(f"Generated Financing Offer: ${offer.amount} (ID: {offer.id})")

        # 6. Accept Financing
        queue_entry = await advance_service.accept_financing_offer(customer_id, offer.id)
        logger.info(f"Offer Accepted. Staged in Funding Queue (ID: {queue_entry.id})")

        # 7. Approve Funding (Operations Approval)
        advance = await advance_service.approve_funding(
            queue_entry.id, 
            reviewer_id="sim_bot@lend.ai", 
            notes="Automated simulation approval."
        )
        logger.info(f"Funding Approved. Advance Active: ID {advance.id}, Amount ${advance.amount}")

        # 8. Simulate Revenue & Repayment Cycle
        # Log a Stripe payment event
        event_id = f"evt_sim_{uuid.uuid4().hex[:10]}"
        stripe_event = {
            "id": event_id,
            "type": "invoice.payment_succeeded",
            "data": {
                "object": {
                    "id": f"in_sim_{uuid.uuid4().hex[:10]}",
                    "amount_paid": 500000, # $5,000.00
                    "customer": "cus_sim_123",
                    "customer_name": "Test Payer",
                    "status_transitions": {"paid_at": datetime.now().timestamp()}
                }
            }
        }
        
        event_log = EventLog(
            customer_id=customer_id,
            event_type="stripe_invoice.payment_succeeded",
            payload=stripe_event,
            idempotency_key=f"stripe_{event_id}",
            processing_status="pending"
        )
        session.add(event_log)
        await session.commit()
        logger.info(f"Logged simulated Stripe repayment event: {event_id}")

        # 9. Run Repayment Processor
        processor = RepaymentProcessor(session)
        processed = await processor.process_pending_events()
        logger.info(f"Repayment Processor: Handled {processed} events.")

        # 10. Run Reconciliation
        recon_service = ReconciliationService(session)
        exceptions = await recon_service.run_full_reconciliation()
        logger.info(f"Reconciliation: Found {exceptions} exceptions.")

        # 11. Final State Summary
        await session.refresh(advance)
        # Fetch obligations
        obs_stmt = select(RepaymentObligation).where(RepaymentObligation.advance_id == advance.id)
        obs_result = await session.execute(obs_stmt)
        obligations = obs_result.scalars().all()
        remaining = sum(o.amount for o in obligations)
        
        logger.info("--- SIMULATION COMPLETE ---")
        logger.info(f"Advance Status: {advance.status}")
        logger.info(f"Outstanding Balance: ${remaining:,.2f}")
        logger.info(f"System Mode: {'SIMULATION' if config.simulation_mode else 'PILOT'}")

if __name__ == "__main__":
    asyncio.run(run_simulation())
