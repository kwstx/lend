from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Type
from uuid import UUID
import logging

from src.models.models import CapitalSource, CapitalReservation

logger = logging.getLogger(__name__)

class CapitalSourceProvider(ABC):
    """
    Abstract interface for different capital sources (Partner API, Treasury, Internal).
    """
    def __init__(self, source_record: CapitalSource):
        self.source_record = source_record

    @abstractmethod
    async def check_liquidity(self, amount: float) -> bool:
        """Check if the source has enough liquidity for the requested amount."""
        pass

    @abstractmethod
    async def reserve_funds(self, amount: float, customer_id: UUID) -> Optional[CapitalReservation]:
        """Temporarily reserve funds from this source."""
        pass

    @abstractmethod
    async def commit_funds(self, reservation: CapitalReservation) -> bool:
        """Finalize the transfer of funds (called when advance is issued)."""
        pass

    @abstractmethod
    async def release_funds(self, reservation: CapitalReservation) -> bool:
        """Release previously reserved funds (called if advance is cancelled or expires)."""
        pass

class PartnerLenderProvider(CapitalSourceProvider):
    async def check_liquidity(self, amount: float) -> bool:
        # Mock: External API call to partner
        logger.info(f"Checking Partner API liquidity for {amount}")
        return self.source_record.available_amount >= amount and self.source_record.is_active

    async def reserve_funds(self, amount: float, customer_id: UUID) -> Optional[CapitalReservation]:
        if await self.check_liquidity(amount):
            return CapitalReservation(
                customer_id=customer_id,
                source_id=self.source_record.id,
                amount=amount,
                status="reserved",
                expires_at=datetime.utcnow() + timedelta(hours=12)
            )
        return None

    async def commit_funds(self, reservation: CapitalReservation) -> bool:
        logger.info(f"Committed {reservation.amount} from Partner API")
        reservation.status = "committed"
        return True

    async def release_funds(self, reservation: CapitalReservation) -> bool:
        logger.info(f"Released {reservation.amount} back to Partner API")
        reservation.status = "released"
        return True

class TreasuryProvider(CapitalSourceProvider):
    async def check_liquidity(self, amount: float) -> bool:
        logger.info(f"Checking Treasury liquidity for {amount}")
        return self.source_record.available_amount >= amount and self.source_record.is_active

    async def reserve_funds(self, amount: float, customer_id: UUID) -> Optional[CapitalReservation]:
        if await self.check_liquidity(amount):
            return CapitalReservation(
                customer_id=customer_id,
                source_id=self.source_record.id,
                amount=amount,
                status="reserved",
                expires_at=datetime.utcnow() + timedelta(hours=24)
            )
        return None

    async def commit_funds(self, reservation: CapitalReservation) -> bool:
        # Treasury funds are usually pre-funded, so we just update internal state
        reservation.status = "committed"
        return True

    async def release_funds(self, reservation: CapitalReservation) -> bool:
        reservation.status = "released"
        return True

class InternalBalanceSheetProvider(CapitalSourceProvider):
    async def check_liquidity(self, amount: float) -> bool:
        logger.info(f"Checking Internal Pool liquidity for {amount}")
        return self.source_record.available_amount >= amount and self.source_record.is_active

    async def reserve_funds(self, amount: float, customer_id: UUID) -> Optional[CapitalReservation]:
        if await self.check_liquidity(amount):
            return CapitalReservation(
                customer_id=customer_id,
                source_id=self.source_record.id,
                amount=amount,
                status="reserved",
                expires_at=datetime.utcnow() + timedelta(hours=48)
            )
        return None

    async def commit_funds(self, reservation: CapitalReservation) -> bool:
        reservation.status = "committed"
        return True

    async def release_funds(self, reservation: CapitalReservation) -> bool:
        reservation.status = "released"
        return True

class CapitalManager:
    """
    Orchestrates capital source selection and reservation management.
    """
    PROVIDERS: Dict[str, Type[CapitalSourceProvider]] = {
        "partner_api": PartnerLenderProvider,
        "treasury": TreasuryProvider,
        "internal_pool": InternalBalanceSheetProvider
    }

    def __init__(self, sources: List[CapitalSource]):
        self.providers = [
            self.PROVIDERS[s.type](s) 
            for s in sources 
            if s.type in self.PROVIDERS and s.is_active
        ]

    async def find_and_reserve_capital(self, amount: float, customer_id: UUID) -> Optional[CapitalReservation]:
        """
        Tries to reserve the requested amount from available sources.
        Strategy: Waterfall (Internal -> Treasury -> Partner)
        """
        # Sort by strategy: we prefer using internal funds first
        # (Assuming internal_pool > treasury > partner_api preference for now)
        order = {"internal_pool": 0, "treasury": 1, "partner_api": 2}
        sorted_providers = sorted(self.providers, key=lambda p: order.get(p.source_record.type, 99))

        for provider in sorted_providers:
            reservation = await provider.reserve_funds(amount, customer_id)
            if reservation:
                logger.info(f"Reserved {amount} from {provider.source_record.name}")
                return reservation
        
        logger.warning(f"Insufficient capital across all sources for amount {amount}")
        return None

    async def finalize_reservation(self, reservation: CapitalReservation, success: bool):
        """Commits or releases a reservation based on the outcome of the advance issuance."""
        # Find the provider for this reservation
        source_id = reservation.source_id
        # This approach is a bit simplified; in practice we'd load the source from DB
        # but for this abstraction layer, we'll assume the manager has the relevant provider.
        for provider in self.providers:
            if provider.source_record.id == source_id:
                if success:
                    await provider.commit_funds(reservation)
                else:
                    await provider.release_funds(reservation)
                return
        
        raise ValueError(f"No provider found for source_id {source_id}")
