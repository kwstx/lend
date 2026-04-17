from uuid import UUID
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from fastapi import HTTPException

from src.models.models import Advance, CapitalSource, CashFlowSnapshot, Customer, CapitalReservation
from src.risk_engine import RiskEngine
from src.core.capital import CapitalManager

class AdvanceService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.risk_engine = RiskEngine()

    async def request_advance(self, customer_id: UUID, amount: float) -> Advance:
        """
        Main entry point for requesting a new advance.
        Handles risk evaluation and capital reservation.
        """
        # 1. Fetch latest customer state and metrics
        customer = await self.session.get(Customer, customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Get latest cash flow snapshot
        stmt = (
            select(CashFlowSnapshot)
            .filter(CashFlowSnapshot.customer_id == customer_id)
            .order_by(desc(CashFlowSnapshot.calculated_at))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        snapshot = result.scalar_one_or_none()
        
        if not snapshot:
            raise HTTPException(status_code=400, detail="No cash flow data available for risk assessment")

        # Calculate operating history
        operating_days = (snapshot.calculated_at - customer.created_at).days

        # 2. Evaluate Risk
        metrics = {
            "revenue_stability_score": snapshot.revenue_stability_score,
            "concentration_risk_score": snapshot.concentration_risk_score,
            "total_open_receivables": snapshot.total_open_receivables,
            "active_advances_total": snapshot.active_advances_total
        }
        
        evaluation = self.risk_engine.evaluate_customer(metrics, operating_days)
        
        if not evaluation.is_eligible:
            raise HTTPException(
                status_code=400, 
                detail={
                    "message": "Advance request denied by risk engine",
                    "reasons": evaluation.rejection_reasons
                }
            )
        
        if amount > evaluation.credit_limit:
            raise HTTPException(
                status_code=400,
                detail=f"Requested amount {amount} exceeds current credit limit of {evaluation.credit_limit}"
            )

        # 3. Capital Source Selection & Reservation
        # Load all active capital sources
        sources_stmt = select(CapitalSource).filter(CapitalSource.is_active == True)
        sources_result = await self.session.execute(sources_stmt)
        active_sources = sources_result.scalars().all()

        capital_manager = CapitalManager(list(active_sources))
        reservation = await capital_manager.find_and_reserve_capital(amount, customer_id)

        if not reservation:
            raise HTTPException(
                status_code=503,
                detail="System liquidity limit reached. No capital available to fund this request."
            )

        # 4. Create the Advance record
        # Note: In a real DB, we would decrease the source's available_amount here within the same transaction.
        advance = Advance(
            customer_id=customer_id,
            amount=amount,
            fee_amount=amount * 0.05, # Standard 5% fee for now
            status="active"
        )
        
        # Link the reservation
        reservation.advance = advance
        self.session.add(reservation)
        self.session.add(advance)

        # Finalize the capital commitment
        await capital_manager.finalize_reservation(reservation, success=True)
        
        # Update capital source balance (simplified for this exercise)
        # In production, this would be an atomic SQL update: UPDATE capital_sources SET available_amount = available_amount - :amt ...
        for source in active_sources:
            if source.id == reservation.source_id:
                source.available_amount -= amount
                break

        await self.session.commit()
        await self.session.refresh(advance)
        
        return advance

    async def cancel_advance_request(self, reservation_id: UUID):
        """Releases reserved capital if an advance is cancelled before issuance."""
        reservation = await self.session.get(CapitalReservation, reservation_id)
        if not reservation or reservation.status != "reserved":
            return

        sources_stmt = select(CapitalSource).filter(CapitalSource.id == reservation.source_id)
        sources_result = await self.session.execute(sources_stmt)
        source = sources_result.scalar_one_or_none()

        if source:
            capital_manager = CapitalManager([source])
            await capital_manager.finalize_reservation(reservation, success=False)
            source.available_amount += reservation.amount
            await self.session.commit()
