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
    risk_score: int  # 0-1000
    probability_of_default: float
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
        
        # Load the latest policy by default
        self.load_policy("standard_v1.json")
        self.load_governor()

    def load_policy(self, filename: str):
        path = os.path.join(self.policies_dir, filename)
        if not os.path.exists(path):
            # Fallback for initialization if file doesn't exist yet in the expected location
            return
        with open(path, 'r') as f:
            data = json.load(f)
            self._current_policy = RiskPolicy(**data)

    def load_governor(self):
        if not os.path.exists(self.governor_path):
            return
        with open(self.governor_path, 'r') as f:
            data = json.load(f)
            if isinstance(data.get('last_updated'), str):
                data['last_updated'] = datetime.fromisoformat(data['last_updated'].replace('Z', '+00:00'))
            self._governor = RiskGovernor(**data)

    def _calculate_probabilistic_score(self, metrics: Dict[str, Any], history_days: int) -> int:
        """
        Calculates a score from 0 (High Risk) to 1000 (Low Risk) based on weighted metrics.
        """
        rules = self._current_policy.rules
        score = 1000
        
        # 1. Operating History (Max 200 points)
        history_points = min(200, (history_days / rules.min_operating_history_days) * 100) if rules.min_operating_history_days > 0 else 200
        
        # 2. Revenue Stability (Max 400 points)
        # Higher volatility score means lower stability. 
        # Assume 0.0 is perfect, 1.0 is highly volatile.
        stability_score = metrics.get('revenue_stability_score', 0.5)
        stability_points = max(0, 400 * (1 - (stability_score / (rules.max_revenue_volatility * 2))))
        
        # 3. Concentration (Max 400 points)
        concentration = metrics.get('concentration_risk_score', 0.5)
        concentration_points = max(0, 400 * (1 - (concentration / (rules.max_customer_concentration * 2))))
        
        final_score = int(history_points + stability_points + concentration_points)
        return min(1000, max(0, final_score))

    def _map_score_to_pd(self, score: int) -> float:
        """
        Maps a 0-1000 score to a Probability of Default (PD).
        Inverse sigmoid-like mapping.
        """
        if score >= 800: return 0.02  # 2% PD for top tier
        if score >= 600: return 0.05  # 5% PD
        if score >= 400: return 0.15  # 15% PD
        if score >= 200: return 0.40  # 40% PD
        return 0.80  # 80% PD for bottom tier

    def evaluate_customer(
        self, 
        metrics: Dict[str, Any], 
        operating_history_days: int
    ) -> RiskEvaluationResult:
        if not self._current_policy or not self._governor:
            raise ValueError("Risk engine not properly initialized")

        rules = self._current_policy.rules
        rejection_reasons = []
        
        # Global Health Check
        if self._governor.status == "suspended":
            return RiskEvaluationResult(
                is_eligible=False,
                risk_score=0,
                probability_of_default=1.0,
                credit_limit=0.0,
                policy_version=self._current_policy.version,
                rejection_reasons=["Global suspension of lending"]
            )

        # Hard Gate: Compliance & Sanctions
        if metrics.get('verification_status') != 'verified':
            rejection_reasons.append("Account must be manually verified")
        if not metrics.get('is_sanction_cleared', False):
            rejection_reasons.append("Sanction/AML screening failed")

        # Hard Gate: Absolute Minimums
        if operating_history_days < rules.min_operating_history_days:
            rejection_reasons.append(f"Insufficient history: {operating_history_days}d")

        # Calculate Probabilistic Score
        score = self._calculate_probabilistic_score(metrics, operating_history_days)
        pd = self._map_score_to_pd(score)
        
        # Fraud Deterrence Factor
        fraud_data = metrics.get('fraud_results', {})
        fraud_risk = fraud_data.get('fraud_risk_level', 'low')
        
        if fraud_risk == 'high':
            rejection_reasons.append("High Fraud Risk detected (multiple anomalies)")
        
        # Bayesian Nudge (Simulated: adjust PD based on historical repayment consistency)
        repayment_consistency = metrics.get('repayment_consistency_score', 1.0)
        pd = pd * (2.0 - repayment_consistency)
        
        # Apply Fraud Multiplier if medium risk
        if fraud_risk == 'medium':
            pd = pd * 1.5 # 50% increase in risk for suspicious activity
            
        pd = min(1.0, max(0.01, pd))

        # Check Eligibility based on score threshold
        if score < 300:
            rejection_reasons.append(f"Risk score too low: {score}")
            
        # Add specific fraud flags for transparency if any exist
        if fraud_data.get('is_structuring_detected'):
            rejection_reasons.append("Structuring/Smurfing pattern detected")
        if fraud_data.get('is_circular_flow_detected'):
            rejection_reasons.append("Circular fund flow (money laundering risk) detected")

        if rejection_reasons:
            return RiskEvaluationResult(
                is_eligible=False,
                risk_score=score,
                probability_of_default=pd,
                credit_limit=0.0,
                policy_version=self._current_policy.version,
                rejection_reasons=rejection_reasons,
                metadata={
                    "risk_score": score,
                    "applied_pd": round(pd, 4),
                    "repayment_consistency": round(repayment_consistency, 2)
                }
            )

        # Credit Limit via Expected Loss (EL) Formula
        # EL = PD * LGD * EAD
        # Here we use the formula to find EAD (Limit) such that EL is acceptable.
        # Or more simply: Limit = Base_Limit * (1 - PD) * LGD_Multiplier
        lgd_factor = 0.8 # Assume 20% recovery in default
        
        open_receivables = metrics.get('total_open_receivables', 0.0)
        active_advances = metrics.get('active_advances_total', 0.0)
        
        base_potential = (open_receivables * rules.revenue_multiplier)
        
        # Risk-adjusted limit: Limit = Potential * (1 - PD) * Recovery_Rate
        risk_adjusted_limit = base_potential * (1.0 - pd) * lgd_factor
        
        final_limit = (risk_adjusted_limit * self._governor.global_multiplier) - active_advances
        final_limit = min(max(0.0, final_limit), rules.max_exposure_cap)

        return RiskEvaluationResult(
            is_eligible=True,
            risk_score=score,
            probability_of_default=pd,
            credit_limit=final_limit,
            policy_version=self._current_policy.version,
            metadata={
                "risk_score": score,
                "score_components": {"history": "weighted", "volatility": "weighted"},
                "applied_pd": round(pd, 4),
                "recovery_factor": lgd_factor
            }
        )
