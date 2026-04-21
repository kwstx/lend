import json
import os
from datetime import datetime
from uuid import UUID
from typing import Dict, Any, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException

from src.models.models import AdminUser, PolicyChangeProposal

class GovernanceService:
    def __init__(self, session: AsyncSession, policies_dir: str = "configs/policies"):
        self.session = session
        self.policies_dir = policies_dir

    async def propose_policy_update(
        self, 
        proposer_id: UUID, 
        target_policy: str, 
        new_content: Dict[str, Any]
    ) -> PolicyChangeProposal:
        """
        Creates a proposal to update a risk policy.
        Requires approval from a DIFFERENT admin to take effect.
        """
        # Validate target_policy exists (or is a valid name)
        if not target_policy.endswith(".json"):
            raise HTTPException(status_code=400, detail="Target policy must be a .json file")

        proposal = PolicyChangeProposal(
            target_policy=target_policy,
            proposed_content=new_content,
            proposer_id=proposer_id,
            status="pending"
        )
        
        self.session.add(proposal)
        await self.session.commit()
        await self.session.refresh(proposal)
        return proposal

    async def approve_policy_update(
        self, 
        approver_id: UUID, 
        proposal_id: UUID
    ) -> PolicyChangeProposal:
        """
        Approves a proposal and writes the changes to disk.
        MANDATORY: Proposer and Approver must be different.
        """
        proposal = await self.session.get(PolicyChangeProposal, proposal_id)
        if not proposal:
            raise HTTPException(status_code=404, detail="Proposal not found")
        
        if proposal.status != "pending":
            raise HTTPException(status_code=400, detail=f"Proposal is already {proposal.status}")

        if proposal.proposer_id == approver_id:
            raise HTTPException(
                status_code=403, 
                detail="Security Violation: The proposer and approver must be different administrators."
            )

        # 1. Update the proposal record
        proposal.approver_id = approver_id
        proposal.status = "approved"
        proposal.finalized_at = datetime.utcnow()

        # 2. Persist the change to the filesystem (The 'Executive' action)
        path = os.path.join(self.policies_dir, proposal.target_policy)
        
        # Ensure directory exists
        os.makedirs(self.policies_dir, exist_ok=True)
        
        with open(path, 'w') as f:
            json.dump(proposal.proposed_content, f, indent=4)

        await self.session.commit()
        return proposal

    async def list_pending_proposals(self) -> List[PolicyChangeProposal]:
        stmt = select(PolicyChangeProposal).where(PolicyChangeProposal.status == "pending")
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
