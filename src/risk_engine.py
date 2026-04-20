import json
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from pydantic import BaseModel
from uuid import UUID

class PolicyRules(BaseModel):
    min_operating_history_days: int
    max_revenue_volatility: float
    max_customer_concentration: float
    revenue_multiplier: float
    max_exposure_cap: float

class RiskPolicy(BaseModel):
    version: str
    name: str
    rules: PolicyRules

class RiskGovernor(BaseModel):
    global_multiplier: float
    status: str  # active, reduced, suspended
    last_updated: datetime
    reason: str

class RiskEvaluationResult(BaseModel):
    is_eligible: bool
    credit_limit: float
    policy_version: str
    rejection_reasons: list[str] = []
    metadata: Dict[str, Any] = {}

class RiskEngine:
    def __init__(self, policies_dir: str = "configs/policies", governor_path: str = "configs/risk_governor.json"):
        self.policies_dir = policies_dir
        self.governor_path = governor_path
        self._current_policy: Optional[RiskPolicy] = None
        self._governor: Optional[RiskGovernor] = None
        
        # Load the latest policy by default (for now we hardcode to standard_v1.json)
        self.load_policy("standard_v1.json")
        self.load_governor()

    def load_policy(self, filename: str):
        path = os.path.join(self.policies_dir, filename)
        with open(path, 'r') as f:
            data = json.load(f)
            self._current_policy = RiskPolicy(**data)

    def load_governor(self):
        with open(self.governor_path, 'r') as f:
            data = json.load(f)
            # Handle string datetime from JSON
            if isinstance(data.get('last_updated'), str):
                data['last_updated'] = datetime.fromisoformat(data['last_updated'].replace('Z', '+00:00'))
            self._governor = RiskGovernor(**data)

    def evaluate_customer(
        self, 
        metrics: Dict[str, Any], 
        operating_history_days: int
    ) -> RiskEvaluationResult:
        """
        Evaluates a customer's risk profile against the current policy and global governor.
        """
        if not self._current_policy or not self._governor:
            raise ValueError("Risk engine not properly initialized with policy or governor")

        rules = self._current_policy.rules
        rejection_reasons = []
        
        # Check status from governor
        if self._governor.status == "suspended":
            return RiskEvaluationResult(
                is_eligible=False,
                credit_limit=0.0,
                policy_version=self._current_policy.version,
                rejection_reasons=["Global suspension of lending"],
                metadata={"governor_reason": self._governor.reason}
            )

        # 1. Compliance Status (Manual/Free Approach)
        compliance_status = metrics.get('verification_status', 'unverified')
        is_sanction_cleared = metrics.get('is_sanction_cleared', False)
        
        if compliance_status != 'verified':
            rejection_reasons.append(f"Compliance status is {compliance_status}. Account must be manually verified by operations.")
        
        if not is_sanction_cleared:
            rejection_reasons.append("Account has not cleared sanction/AML screening.")

        # 2. Operating History
        if operating_history_days < rules.min_operating_history_days:
            rejection_reasons.append(f"Insufficient operating history: {operating_history_days} days (min {rules.min_operating_history_days})")

        # 2. Volatility
        volatility = metrics.get('revenue_stability_score', 0.0)
        if volatility > rules.max_revenue_volatility:
            rejection_reasons.append(f"Revenue volatility too high: {volatility:.2f} (max {rules.max_revenue_volatility})")

        # 3. Customer Concentration
        concentration = metrics.get('concentration_risk_score', 0.0)
        if concentration > rules.max_customer_concentration:
            rejection_reasons.append(f"Customer concentration too high: {concentration:.2f} (max {rules.max_customer_concentration})")

        # If there are rejection reasons, the customer is not eligible
        if rejection_reasons:
            return RiskEvaluationResult(
                is_eligible=False,
                credit_limit=0.0,
                policy_version=self._current_policy.version,
                rejection_reasons=rejection_reasons
            )

        # 4. Credit Limit Calculation
        # Based on open receivables and revenue multiplier
        open_receivables = metrics.get('total_open_receivables', 0.0)
        active_advances = metrics.get('active_advances_total', 0.0)
        
        base_limit = (open_receivables * rules.revenue_multiplier) - active_advances
        
        # Apply global governor multiplier
        final_limit = base_limit * self._governor.global_multiplier
        
        # Cap by maximum exposure
        final_limit = min(final_limit, rules.max_exposure_cap)
        
        # Ensure no negative limit
        final_limit = max(0.0, final_limit)

        return RiskEvaluationResult(
            is_eligible=True,
            credit_limit=final_limit,
            policy_version=self._current_policy.version,
            metadata={
                "base_limit": base_limit,
                "governor_multiplier_applied": self._governor.global_multiplier,
                "max_exposure_cap_applied": rules.max_exposure_cap
            }
        )
