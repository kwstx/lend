import numpy as np
import networkx as nx
from datetime import datetime, timedelta
from typing import Dict, Any, List
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

class FraudDetector:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def detect_structuring(self, customer_id: UUID, threshold: float = 10000.0) -> Dict[str, Any]:
        """
        Detects 'structuring' or 'smurfing' where multiple transactions are 
        just below a reporting threshold (e.g., $10,000).
        """
        # Search for transactions between 90% and 100% of the threshold
        lower_bound = threshold * 0.9
        query = text("""
            SELECT COUNT(*) as count, SUM(amount) as total
            FROM transactions
            WHERE customer_id = :cid
              AND amount >= :lb AND amount < :threshold
              AND type = 'inflow'
              AND timestamp >= (NOW() - INTERVAL '30 days')
        """)
        res = await self.session.execute(query, {"cid": customer_id, "lb": lower_bound, "threshold": threshold})
        row = res.fetchone()
        
        count = row.count if row else 0
        is_suspicious = count >= 3 # Arbitrary flag: 3+ transactions just under the limit
        
        return {
            "is_structuring_detected": is_suspicious,
            "structuring_count": count,
            "structuring_threshold": threshold
        }

    async def detect_round_numbers(self, customer_id: UUID) -> Dict[str, Any]:
        """
        Flags merchants with an unusually high percentage of perfectly round transactions.
        Natural revenue (like $12.43) is rarely perfectly round ($1,000.00).
        """
        query = text("""
            SELECT 
                COUNT(*) as total_count,
                SUM(CASE WHEN amount = FLOOR(amount) THEN 1 ELSE 0 END) as round_count
            FROM transactions
            WHERE customer_id = :cid
              AND type = 'inflow'
              AND timestamp >= (NOW() - INTERVAL '90 days')
        """)
        res = await self.session.execute(query, {"cid": customer_id})
        row = res.fetchone()
        
        total = row.total_count if row and row.total_count else 0
        rounds = row.round_count if row and row.round_count else 0
        
        ratio = (rounds / total) if total > 0 else 0
        # If > 50% of transactions are perfectly round, it's a red flag for synthetic data
        is_suspicious = ratio > 0.5 and total > 5
        
        return {
            "round_number_ratio": round(ratio, 2),
            "is_excessive_round_numbers": is_suspicious
        }

    async def check_benfords_law(self, customer_id: UUID) -> Dict[str, Any]:
        """
        Checks if the leading digits of inflows follow Benford's Law.
        Significant deviation suggests manual data entry or fraud.
        """
        query = text("""
            SELECT LEFT(CAST(amount AS TEXT), 1) as first_digit, COUNT(*) as count
            FROM transactions
            WHERE customer_id = :cid
              AND type = 'inflow'
              AND amount >= 1
            GROUP BY 1
        """)
        res = await self.session.execute(query, {"cid": customer_id})
        rows = res.fetchall()
        
        counts = {str(i): 0 for i in range(1, 10)}
        total = 0
        for row in rows:
            if row.first_digit in counts:
                counts[row.first_digit] = row.count
                total += row.count
                
        if total < 20: # Not enough data for statistical significance
            return {"benfords_law_anomaly_detected": False, "confidence": "low"}
            
        # Expected Benford frequencies: 1: 30.1%, 2: 17.6%, 3: 12.5%, etc.
        expected = {
            '1': 0.301, '2': 0.176, '3': 0.125, 
            '4': 0.097, '5': 0.079, '6': 0.067, 
            '7': 0.058, '8': 0.051, '9': 0.046
        }
        
        # Calculate Chi-Square like statistic or simple MAD (Mean Absolute Deviation)
        mad = sum(abs((counts[d]/total) - expected[d]) for d in expected) / 9
        
        # Threshold for MAD: > 0.05 is often suspicious
        return {
            "benfords_mad": round(mad, 4),
            "benfords_law_anomaly_detected": mad > 0.06,
            "observation_count": total
        }

    async def detect_circular_flows(self, customer_id: UUID) -> Dict[str, Any]:
        """
        Uses NetworkX to detect circular paths between the merchant and their payers/payees.
        Merchant A -> Payee B -> Merchant A (Red Flag)
        """
        # Fetch recent inflows and outflows
        query = text("""
            SELECT type, amount, payer_id, context_data->>'recipient_id' as recipient_id
            FROM transactions
            WHERE customer_id = :cid
              AND timestamp >= (NOW() - INTERVAL '60 days')
        """)
        res = await self.session.execute(query, {"cid": customer_id})
        rows = res.fetchall()
        
        G = nx.DiGraph()
        merchant_node = str(customer_id)
        
        for row in rows:
            if row.type == 'inflow' and row.payer_id:
                # Payer -> Merchant
                G.add_edge(str(row.payer_id), merchant_node, weight=float(row.amount))
            elif row.type == 'outflow' and row.recipient_id:
                # Merchant -> Recipient
                G.add_edge(merchant_node, str(row.recipient_id), weight=float(row.amount))
        
        # Check for simple cycles containing the merchant
        try:
            cycles = list(nx.simple_cycles(G))
            # Filter for cycles involving the merchant
            merchant_cycles = [c for c in cycles if merchant_node in c]
            
            return {
                "is_circular_flow_detected": len(merchant_cycles) > 0,
                "cycle_count": len(merchant_cycles),
                "suspect_nodes": list(set([node for c in merchant_cycles for node in c if node != merchant_node]))
            }
        except Exception:
            return {"is_circular_flow_detected": False, "error": "Network analysis failed"}

    async def run_all(self, customer_id: UUID) -> Dict[str, Any]:
        """Orchestrates all fraud detection signals."""
        results = {}
        results.update(await self.detect_structuring(customer_id))
        results.update(await self.detect_round_numbers(customer_id))
        results.update(await self.check_benfords_law(customer_id))
        results.update(await self.detect_circular_flows(customer_id))
        
        # Aggregate Risk Score for Fraud
        fraud_flags = [
            results["is_structuring_detected"],
            results["is_excessive_round_numbers"],
            results["benfords_law_anomaly_detected"],
            results["is_circular_flow_detected"]
        ]
        results["fraud_risk_level"] = "high" if sum(fraud_flags) >= 2 else "medium" if sum(fraud_flags) == 1 else "low"
        
        return results
