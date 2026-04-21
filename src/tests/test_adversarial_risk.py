import pytest
from datetime import datetime, timedelta
from uuid import uuid4
from src.risk_engine import RiskEngine

def test_adversarial_stale_data_killswitch():
    engine = RiskEngine()
    metrics = {
        "revenue_stability_score": 0.1,
        "concentration_risk_score": 0.1,
        "total_open_receivables": 10000.0,
        "active_advances_total": 0.0,
        "verification_status": "verified",
        "is_sanction_cleared": True
    }
    
    # Case 1: Fresh data
    fresh_sync = datetime.utcnow()
    res_fresh = engine.evaluate_customer(metrics, 180, last_synced_at=fresh_sync)
    
    # Case 2: Stale data (25 hours old)
    stale_sync = datetime.utcnow() - timedelta(hours=25)
    res_stale = engine.evaluate_customer(metrics, 180, last_synced_at=stale_sync)
    
    assert res_fresh.credit_limit > 0
    # Stale data should result in 50% limit compared to fresh
    assert res_stale.credit_limit == res_fresh.credit_limit * 0.5
    assert res_stale.metadata["stale_data_penalty_applied"] is True

def test_adversarial_bad_actor_structuring():
    engine = RiskEngine()
    metrics = {
        "revenue_stability_score": 0.1,
        "concentration_risk_score": 0.1,
        "total_open_receivables": 10000.0,
        "active_advances_total": 0.0,
        "verification_status": "verified",
        "is_sanction_cleared": True,
        "fraud_results": {
            "fraud_risk_level": "high",
            "is_structuring_detected": True
        }
    }
    
    # Even with perfect stability and history, a high fraud risk should trigger rejection
    result = engine.evaluate_customer(metrics, 365)
    
    assert result.is_eligible is False
    assert "High Fraud Risk detected" in result.rejection_reasons[0]
    assert "Structuring/Smurfing pattern detected" in result.rejection_reasons

def test_adversarial_circular_flow_rejection():
    engine = RiskEngine()
    metrics = {
        "revenue_stability_score": 0.1,
        "concentration_risk_score": 0.1,
        "total_open_receivables": 10000.0,
        "active_advances_total": 0.0,
        "verification_status": "verified",
        "is_sanction_cleared": True,
        "fraud_results": {
            "fraud_risk_level": "medium",
            "is_circular_flow_detected": True
        }
    }
    
    res_normal = engine.evaluate_customer({**metrics, "fraud_results": {}}, 365)
    res_circular = engine.evaluate_customer(metrics, 365)
    
    # Circular flow shouldn't always reject (if medium risk) but PD should be 1.5x higher
    # Higher PD = Lower Limit
    assert res_circular.probability_of_default > res_normal.probability_of_default
    assert res_circular.credit_limit < res_normal.credit_limit
    assert "Circular fund flow (money laundering risk) detected" in res_circular.rejection_reasons

def test_explainability_output():
    engine = RiskEngine()
    # Profile with high volatility and poor history
    metrics = {
        "revenue_stability_score": 0.9,
        "concentration_risk_score": 0.1,
        "total_open_receivables": 1000.0,
        "active_advances_total": 0.0,
        "verification_status": "verified",
        "is_sanction_cleared": True
    }
    
    result = engine.evaluate_customer(metrics, 30) # Only 30 days history
    
    explanation = result.metadata["rejection_explanation"]
    assert "rejection_explanation" in result.metadata
    assert "High revenue volatility" in explanation
    assert "Short operating history" in explanation
