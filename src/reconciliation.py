import os
import stripe
from datetime import datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from src.core.database import async_session, set_tenant_context
from src.models.models import Customer, Transaction, Receivable
from src.intelligence import CashFlowIntelligence

async def reconcile_stripe_data():
    """
    Scheduled job to re-pull Stripe data for all customers to ensure consistency.
    """
    async with async_session() as session:
        result = await session.execute(select(Customer).where(Customer.stripe_account_id != None))
        customers = result.scalars().all()
        
        for customer in customers:
            print(f"Reconciling Stripe data for customer: {customer.name} ({customer.id})")
            # Set tenant context for safety (though RLS might not apply to background jobs depending on config,
            # it's good practice to set it if using the same session logic).
            await set_tenant_context(session, str(customer.id))
            
            try:
                # Fetch recent invoices from Stripe
                # In a real app, you might fetch last 24h or so
                invoices = stripe.Invoice.list(
                    limit=50,
                    status='paid',
                    created={'gt': int((datetime.now() - timedelta(hours=24)).timestamp())},
                    # stripe_account=customer.stripe_account_id # If using Connect
                )
                
                for inv in invoices.auto_paging_iter():
                    # Check if transaction already exists
                    ext_id = inv["id"]
                    tx_result = await session.execute(
                        select(Transaction).where(Transaction.metadata["invoice_id"].astext == ext_id)
                    )
                    if not tx_result.scalars().first():
                        print(f"Found missing transaction for invoice {ext_id}, creating...")
                        new_tx = Transaction(
                            customer_id=customer.id,
                            amount=inv["amount_paid"] / 100.0,
                            type="inflow",
                            category="sales",
                            timestamp=datetime.fromtimestamp(inv["status_transitions"]["paid_at"]),
                            payer_id=inv.get("customer"),
                            payer_name=inv.get("customer_name") or inv.get("customer_email"),
                            metadata={"source": "stripe_recon", "invoice_id": ext_id}
                        )
                        session.add(new_tx)
                
                # Fetch balance from Stripe
                # balance = stripe.Balance.retrieve(stripe_account=customer.stripe_account_id)
                # In a real app, you might log this or check against internal ledgers
                # print(f"Stripe balance for {customer.id}: {balance}")
                
                # Update snapshot after reconciliation
                intel = CashFlowIntelligence(session)
                await intel.compute_and_save_snapshot(customer.id)
                
                await session.commit()
            except Exception as e:
                print(f"Error reconciling Stripe for {customer.id}: {e}")
                await session.rollback()

if __name__ == "__main__":
    # For manual testing
    import asyncio
    asyncio.run(reconcile_stripe_data())
