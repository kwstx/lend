import numpy as np
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID
from sqlalchemy import text, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from src.models.models import CashFlowSnapshot, Transaction, Receivable, Advance, Customer, RepaymentObligation
from src.risk_engine import RiskEngine
from src.core.observability import AuditLogger

class CashFlowIntelligence:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.risk_engine = RiskEngine()

    async def compute_and_save_snapshot(self, customer_id: UUID) -> CashFlowSnapshot:
        """
        Computes rolling metrics for a customer using deterministic SQL queries
        and saves a versioned snapshot.
        """
        # 0. Fetch Customer Info
        customer_res = await self.session.execute(select(Customer).where(Customer.id == customer_id))
        customer = customer_res.scalar_one_or_none()
        if not customer:
            raise ValueError(f"Customer {customer_id} not found")

        # 1. Trailing Revenue (30d and 90d)
        revenue_query = text("""
            SELECT 
                COALESCE(SUM(CASE WHEN timestamp >= :d30 THEN amount ELSE 0 END), 0) as rev_30d,
                COALESCE(SUM(CASE WHEN timestamp >= :d90 THEN amount ELSE 0 END), 0) as rev_90d
            FROM transactions
            WHERE customer_id = :cid 
              AND type = 'inflow' 
              AND category IN ('sales', 'subscription')
        """)
        
        now = datetime.utcnow()
        d30 = now - timedelta(days=30)
        d90 = now - timedelta(days=90)
        
        res = await self.session.execute(revenue_query, {"cid": customer_id, "d30": d30, "d90": d90})
        rev_row = res.fetchone()
        rev_30d = rev_row.rev_30d if rev_row else 0.0
        rev_90d = rev_row.rev_90d if rev_row else 0.0

        # 2. Revenue Stability (Weighted Volatility via EWMA)
        # We'll calculate monthly sums and then compute weighted CV using NumPy
        stability_query = text("""
            WITH monthly_rev AS (
                SELECT 
                    date_trunc('month', timestamp) as month,
                    SUM(amount) as total
                FROM transactions
                WHERE customer_id = :cid 
                  AND type = 'inflow' 
                  AND category IN ('sales', 'subscription')
                  AND timestamp >= :d180
                GROUP BY 1
                ORDER BY month DESC
            )
            SELECT total FROM monthly_rev
        """)
        d180 = now - timedelta(days=180)
        res = await self.session.execute(stability_query, {"cid": customer_id, "d180": d180})
        rows = res.fetchall()
        revenue_series = [float(row.total) for row in rows] if rows else []

        if len(revenue_series) > 1:
            # Alpha for EWMA (higher alpha = more weight to recent data)
            alpha = 0.4
            weights = np.array([(1 - alpha)**i for i in range(len(revenue_series))])
            
            # Weighted Mean and Standard Deviation
            weighted_mean = np.average(revenue_series, weights=weights)
            weighted_var = np.average((np.array(revenue_series) - weighted_mean)**2, weights=weights)
            weighted_std = np.sqrt(weighted_var)
            
            stability_score = (weighted_std / weighted_mean) if weighted_mean > 0 else 1.0
        else:
            stability_score = 0.5 # Default middle ground for new customers

        # 3. Concentration Risk (Top payer % of revenue over last 90d)
        concentration_query = text("""
            WITH payer_rev AS (
                SELECT 
                    payer_id,
                    SUM(amount) as total
                FROM transactions
                WHERE customer_id = :cid 
                  AND type = 'inflow' 
                  AND category IN ('sales', 'subscription')
                  AND timestamp >= :d90
                  AND payer_id IS NOT NULL
                GROUP BY 1
            ),
            total_rev AS (
                SELECT SUM(total) as grand_total FROM payer_rev
            )
            SELECT 
                COALESCE(MAX(total) / NULLIF((SELECT grand_total FROM total_rev), 0), 0) as concentration
            FROM payer_rev
        """)
        res = await self.session.execute(concentration_query, {"cid": customer_id, "d90": d90})
        conc_row = res.fetchone()
        concentration_risk_score = float(conc_row.concentration) if conc_row else 0.0

        # ... (Inflow Classification and Core Liquidity omitted for brevity in instruction, keeping same logic)
        classification_query = text("""
            SELECT 
                COALESCE(SUM(CASE WHEN category IN ('sales', 'subscription') THEN amount ELSE 0 END), 0) as true_revenue,
                COALESCE(SUM(CASE WHEN category NOT IN ('sales', 'subscription') THEN amount ELSE 0 END), 0) as other_inflow
            FROM transactions
            WHERE customer_id = :cid 
              AND type = 'inflow' 
              AND timestamp >= :d30
        """)
        res = await self.session.execute(classification_query, {"cid": customer_id, "d30": d30})
        class_row = res.fetchone()
        true_rev_30d = class_row.true_revenue if class_row else 0.0
        other_inflow_30d = class_row.other_inflow if class_row else 0.0

        receivables_query = select(func.sum(Receivable.amount)).where(
            Receivable.customer_id == customer_id,
            Receivable.status == "pending"
        )
        advances_query = select(func.sum(Advance.amount)).where(
            Advance.customer_id == customer_id,
            Advance.status == "active"
        )
        
        recv_res = await self.session.execute(receivables_query)
        adv_res = await self.session.execute(advances_query)
        open_receivables = recv_res.scalar() or 0.0
        active_advances = adv_res.scalar() or 0.0
        
        # 4. Bayesian Nudge: Repayment Consistency
        repayment_query = text("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed
            FROM repayment_obligations
            WHERE customer_id = :cid
        """)
        res = await self.session.execute(repayment_query, {"cid": customer_id})
        rep_row = res.fetchone()
        total_repayments = rep_row.total if rep_row else 0
        completed_repayments = rep_row.completed if rep_row else 0
        
        repayment_consistency = (completed_repayments / total_repayments) if total_repayments > 0 else 1.0

        # 6. Risk Evaluation
        operating_history_days = (now - customer.created_at).days
        metrics = {
            "revenue_stability_score": stability_score,
            "concentration_risk_score": concentration_risk_score,
            "total_open_receivables": open_receivables,
            "active_advances_total": active_advances,
            "verification_status": customer.verification_status,
            "is_sanction_cleared": customer.is_sanction_cleared,
            "repayment_consistency_score": repayment_consistency
        }
        
        evaluation = self.risk_engine.evaluate_customer(metrics, operating_history_days)

        # 7. Create Snapshot
        snapshot = CashFlowSnapshot(
            customer_id=customer_id,
            calculated_at=now,
            trailing_revenue_30d=rev_30d,
            trailing_revenue_90d=rev_90d,
            revenue_stability_score=stability_score,
            concentration_risk_score=concentration_risk_score,
            true_revenue_inflow_30d=true_rev_30d,
            other_inflow_30d=other_inflow_30d,
            total_open_receivables=open_receivables,
            active_advances_total=active_advances,
            available_credit_limit=evaluation.credit_limit,
            calculation_version="v2.0",
            # Risk Evaluation Fields
            is_eligible=evaluation.is_eligible,
            rejection_reasons=evaluation.rejection_reasons,
            policy_version=evaluation.policy_version,
            risk_evaluation_metadata=evaluation.metadata
        )
        
        self.session.add(snapshot)
        
        # Log Audit Event
        await AuditLogger.log_action(
            self.session,
            customer_id=customer_id,
            event_type="cash_flow_snapshot_computed",
            payload={
                "snapshot_id": str(snapshot.id),
                "is_eligible": snapshot.is_eligible,
                "credit_limit": snapshot.available_credit_limit,
                "rejection_reasons": snapshot.rejection_reasons
            }
        )
        
        await self.session.commit()
        await self.session.refresh(snapshot)
        
        return snapshot
