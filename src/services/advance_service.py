from uuid import UUID
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from fastapi import HTTPException

from src.models.models import (
    Advance, CapitalSource, CashFlowSnapshot, Customer, 
    CapitalReservation, FinancingOffer, FundingQueue
)
from src.risk_engine import RiskEngine
from src.core.capital import CapitalManager
from datetime import datetime, timedelta

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

    async def create_financing_offer(self, customer_id: UUID, amount: float) -> FinancingOffer:
        """
        Evaluates risk engine against the latest cash-flow snapshot and generates an offer object.
        """
        # 1. Fetch latest customer and snapshot
        customer = await self.session.get(Customer, customer_id)
        if not customer:
            raise HTTPException(status_code=404, detail="Customer not found")

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

        # 2. Evaluate Risk
        operating_days = (snapshot.calculated_at - customer.created_at).days
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
                    "message": "Offer request denied by risk engine",
                    "reasons": evaluation.rejection_reasons
                }
            )
        
        if amount > evaluation.credit_limit:
            raise HTTPException(
                status_code=400,
                detail=f"Requested amount {amount} exceeds current credit limit of {evaluation.credit_limit}"
            )

        # 3. Create the Offer
        offer = FinancingOffer(
            customer_id=customer_id,
            snapshot_id=snapshot.id,
            amount=amount,
            fee_amount=amount * 0.05, # Standard fee
            status="pending",
            expires_at=datetime.utcnow() + timedelta(hours=24)
        )
        
        self.session.add(offer)
        await self.session.commit()
        await self.session.refresh(offer)
        return offer

    async def accept_financing_offer(self, customer_id: UUID, offer_id: UUID) -> FundingQueue:
        """
        Moves the request into a funding_queue, not directly into payout.
        Stages for approval and capital reservation.
        """
        # 1. Fetch the offer
        offer = await self.session.get(FinancingOffer, offer_id)
        if not offer or offer.customer_id != customer_id:
            raise HTTPException(status_code=404, detail="Financing offer not found")
        
        if offer.status != "pending":
            raise HTTPException(status_code=400, detail=f"Offer is in status '{offer.status}' and cannot be accepted")
        
        if offer.expires_at < datetime.utcnow():
            offer.status = "expired"
            await self.session.commit()
            raise HTTPException(status_code=400, detail="Offer has expired")

        # 2. Capital Reservation
        sources_stmt = select(CapitalSource).filter(CapitalSource.is_active == True)
        sources_result = await self.session.execute(sources_stmt)
        active_sources = sources_result.scalars().all()

        capital_manager = CapitalManager(list(active_sources))
        reservation = await capital_manager.find_and_reserve_capital(offer.amount, customer_id)

        if not reservation:
            raise HTTPException(
                status_code=503,
                detail="System liquidity limit reached. No capital available to fund this offer."
            )

        # 3. Create Funding Queue Entry
        queue_entry = FundingQueue(
            customer_id=customer_id,
            offer_id=offer.id,
            reservation_id=reservation.id,
            status="staged_for_approval"
        )
        
        # Update offer status
        offer.status = "accepted" # Changed from funding_queued for clarity
        
        # Note: In a real DB, we would decrease the source's available_amount here (reserved status)
        for source in active_sources:
            if source.id == reservation.source_id:
                source.available_amount -= offer.amount
                break

        self.session.add(reservation)
        self.session.add(queue_entry)
        await self.session.commit()
        await self.session.refresh(queue_entry)
        
        return queue_entry

    async def approve_funding(self, queue_id: UUID, reviewer_id: str, notes: Optional[str] = None) -> Advance:
        """
        Human-in-the-loop approval. Triggers payout and creates records.
        """
        # 1. Fetch queue entry
        queue_entry = await self.session.get(FundingQueue, queue_id)
        if not queue_entry or queue_entry.status != "staged_for_approval":
            raise HTTPException(status_code=404, detail="Funding request not found or already processed")

        # 2. Fetch related data
        offer = await self.session.get(FinancingOffer, queue_entry.offer_id)
        reservation = await self.session.get(CapitalReservation, queue_entry.reservation_id)
        
        # 3. Trigger External Payout (Mock)
        # In production: trigger_unit_payout(destination, queue_entry.amount)
        print(f"DEBUG: Triggering external capital deployment for {offer.amount} to customer {queue_entry.customer_id}")

        # 4. Create Advance
        advance = Advance(
            customer_id=queue_entry.customer_id,
            amount=offer.amount,
            fee_amount=offer.fee_amount,
            status="active",
            capital_reservation_id=reservation.id
        )
        self.session.add(advance)
        await self.session.flush() # Get advance ID

        # 5. Create Repayment Obligations (Legally traceable)
        # For simplicity, 1 obligation for total + fee due in 30 days
        obligation = RepaymentObligation(
            customer_id=queue_entry.customer_id,
            advance_id=advance.id,
            amount=offer.amount + offer.fee_amount,
            status="pending",
            due_date=datetime.utcnow() + timedelta(days=30)
        )
        self.session.add(obligation)

        # 6. Finalize Statuses
        queue_entry.status = "paid"
        queue_entry.reviewer_id = reviewer_id
        queue_entry.reviewed_at = datetime.utcnow()
        queue_entry.reviewer_notes = notes
        
        offer.status = "funded"
        reservation.status = "committed"
        reservation.advance_id = advance.id

        await self.session.commit()
        await self.session.refresh(advance)
        return advance

    async def reject_funding(self, queue_id: UUID, reviewer_id: str, reason: str) -> FundingQueue:
        """
        Human-in-the-loop rejection. Releases reserved capital.
        """
        # 1. Fetch queue entry
        queue_entry = await self.session.get(FundingQueue, queue_id)
        if not queue_entry or queue_entry.status != "staged_for_approval":
            raise HTTPException(status_code=404, detail="Funding request not found or already processed")

        # 2. Fetch related data
        offer = await self.session.get(FinancingOffer, queue_entry.offer_id)
        reservation = await self.session.get(CapitalReservation, queue_entry.reservation_id)
        
        # 3. Release Capital
        source = await self.session.get(CapitalSource, reservation.source_id)
        if source:
            source.available_amount += offer.amount
        
        reservation.status = "released"

        # 4. Finalize Statuses
        queue_entry.status = "rejected"
        queue_entry.reviewer_id = reviewer_id
        queue_entry.reviewed_at = datetime.utcnow()
        queue_entry.rejection_reason = reason
        
        offer.status = "rejected"

        await self.session.commit()
        await self.session.refresh(queue_entry)
        return queue_entry

