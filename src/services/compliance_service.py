from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.models import Customer, EventLog
from src.core.observability import AuditLogger

class ComplianceService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def update_verification_status(
        self, 
        customer_id: UUID, 
        status: str, 
        reviewer_id: str,
        reason: Optional[str] = None,
        notes: Optional[str] = None
    ) -> Customer:
        """
        Manually updates a customer's verification status after external review.
        Following the 'Free/Manual' compliance approach.
        """
        customer = await self.session.get(Customer, customer_id)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found")

        old_status = customer.verification_status
        customer.verification_status = status
        
        # If we transition to verified, we assume sanction clearing has been done manually
        if status == "verified":
            customer.is_sanction_cleared = True
            
        customer.last_compliance_check_at = datetime.utcnow()
        
        metadata = customer.verification_metadata or {}
        metadata["last_review"] = {
            "reviever_id": reviewer_id,
            "reviewed_at": datetime.utcnow().isoformat(),
            "previous_status": old_status,
            "reason": reason,
            "notes": notes
        }
        customer.verification_metadata = metadata
        
        self.session.add(customer)
        
        # Audit Log
        await AuditLogger.log_action(
            self.session,
            customer_id=customer_id,
            event_type="compliance_status_updated",
            payload={
                "old_status": old_status,
                "new_status": status,
                "reviewer_id": reviewer_id,
                "reason": reason
            }
        )
        
        await self.session.commit()
        await self.session.refresh(customer)
        return customer

    async def record_document_intake(
        self,
        customer_id: UUID,
        doc_type: str, # kyc_id, kyb_registration, tax_doc
        storage_path: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Customer:
        """Records the intake of a manually uploaded document."""
        customer = await self.session.get(Customer, customer_id)
        if not customer:
            raise ValueError(f"Customer {customer_id} not found")
            
        v_metadata = customer.verification_metadata or {}
        if "documents" not in v_metadata:
            v_metadata["documents"] = []
            
        v_metadata["documents"].append({
            "type": doc_type,
            "path": storage_path,
            "uploaded_at": datetime.utcnow().isoformat(),
            "metadata": metadata
        })
        
        customer.verification_metadata = v_metadata
        # Automatically move to 'pending' if it was 'unverified'
        if customer.verification_status == "unverified":
            customer.verification_status = "pending"
            
        self.session.add(customer)
        await self.session.commit()
        await self.session.refresh(customer)
        return customer
