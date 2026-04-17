import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import Depends, HTTPException, Header, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from src.core.database import get_session, set_tenant_context
from src.models.models import TenantApiKey, AdminUser, SystemConfig, Customer
from jose import jwt, JWTError

# Security Constants
API_KEY_PREFIX = "sk_live_"
JWT_SECRET = "REPLACE_WITH_SECURE_SECRET" # Should be in env
ALGORITHM = "HS256"

security = HTTPBearer()

def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

async def get_current_tenant(
    authorization: HTTPAuthorizationCredentials = Depends(security),
    session: AsyncSession = Depends(get_session)
) -> Customer:
    """
    Authenticates a tenant using an API key. 
    Enforces strict tenant isolation via RLS after auth.
    """
    api_key = authorization.credentials
    if not api_key.startswith(API_KEY_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key format"
        )
    
    hashed = hash_api_key(api_key)
    stmt = select(TenantApiKey).where(TenantApiKey.hashed_key == hashed, TenantApiKey.is_active == True)
    result = await session.execute(stmt)
    key_record = result.scalars().first()
    
    if not key_record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API Key"
        )
    
    # Update last used
    key_record.last_used_at = datetime.utcnow()
    await session.commit()
    
    # Get the tenant (Customer)
    tenant = await session.get(Customer, key_record.tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tenant not found"
        )
        
    # Set RLS context
    await set_tenant_context(session, str(tenant.id))
    
    return tenant

async def get_current_admin(
    authorization: HTTPAuthorizationCredentials = Depends(security),
    session: AsyncSession = Depends(get_session)
) -> AdminUser:
    """
    Authenticates an admin user using JWT.
    """
    token = authorization.credentials
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
        
    stmt = select(AdminUser).where(AdminUser.email == email, AdminUser.is_active == True)
    result = await session.execute(stmt)
    user = result.scalars().first()
    
    if user is None:
        raise HTTPException(status_code=401, detail="Admin user not found")
        
    return user

def require_role(allowed_roles: List[str]):
    def role_checker(user: AdminUser = Depends(get_current_admin)):
        if user.role not in allowed_roles and user.role != "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions"
            )
        return user
    return role_checker

class KillSwitchGuard:
    """Dependency to check global system kill switches."""
    def __init__(self, switch_name: str):
        self.switch_name = switch_name
        
    async def __call__(self, session: AsyncSession = Depends(get_session)):
        stmt = select(SystemConfig).where(SystemConfig.id == 1)
        result = await session.execute(stmt)
        config = result.scalars().first()
        
        # Default to safe (not frozen) if config doesn't exist
        if not config:
            return True
            
        is_frozen = getattr(config, self.switch_name, False)
        if is_frozen:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"System component '{self.switch_name}' is currently frozen by administration."
            )
        return True

async def check_underwriting_frozen(session: AsyncSession = Depends(get_session)):
    await KillSwitchGuard("underwriting_frozen")(session)

async def check_deployment_paused(session: AsyncSession = Depends(get_session)):
    await KillSwitchGuard("fund_deployment_paused")(session)

async def check_repayments_paused(session: AsyncSession = Depends(get_session)):
    await KillSwitchGuard("repayments_paused")(session)
