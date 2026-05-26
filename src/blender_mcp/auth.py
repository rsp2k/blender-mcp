"""OAuth2 authentication handler with JWT verification"""

import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi import HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# OAuth configuration from environment
OAUTH_SECRET_KEY = os.getenv("OAUTH_SECRET_KEY", "dev-secret-key-change-in-production")
OAUTH_ALGORITHM = os.getenv("OAUTH_ALGORITHM", "HS256")
OAUTH_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("OAUTH_ACCESS_TOKEN_EXPIRE_MINUTES", "30"))

security = HTTPBearer()


class TokenData(BaseModel):
    """JWT token payload structure"""
    user_id: str
    username: Optional[str] = None
    scopes: list[str] = []
    exp: Optional[datetime] = None


class OAuthHandler:
    """Handles OAuth2 JWT token creation and verification"""
    
    def __init__(
        self,
        secret_key: str = OAUTH_SECRET_KEY,
        algorithm: str = OAUTH_ALGORITHM,
        token_expire_minutes: int = OAUTH_ACCESS_TOKEN_EXPIRE_MINUTES
    ):
        self.secret_key = secret_key
        self.algorithm = algorithm
        self.token_expire_minutes = token_expire_minutes
        
        if self.secret_key == "dev-secret-key-change-in-production":
            logger.warning("🚨 Using default OAuth secret key - CHANGE IN PRODUCTION!")
    
    def create_access_token(
        self,
        data: Dict[str, Any],
        expires_delta: Optional[timedelta] = None
    ) -> str:
        """Create a JWT access token"""
        to_encode = data.copy()
        
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=self.token_expire_minutes)
        
        to_encode.update({"exp": expire})
        
        try:
            encoded_jwt = jwt.encode(to_encode, self.secret_key, algorithm=self.algorithm)
            return encoded_jwt
        except Exception as e:
            logger.error(f"Failed to create access token: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not create access token"
            )
    
    def verify_token(self, token: str) -> TokenData:
        """Verify and decode JWT token"""
        try:
            payload = jwt.decode(token, self.secret_key, algorithms=[self.algorithm])
            
            user_id: str = payload.get("sub")
            if user_id is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid token: missing user ID",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            
            # Extract additional claims
            username = payload.get("username")
            scopes = payload.get("scopes", [])
            exp = payload.get("exp")
            
            if exp:
                exp_datetime = datetime.fromtimestamp(exp)
                if exp_datetime < datetime.utcnow():
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Token expired",
                        headers={"WWW-Authenticate": "Bearer"},
                    )
            
            return TokenData(
                user_id=user_id,
                username=username,
                scopes=scopes,
                exp=exp_datetime if exp else None
            )
            
        except JWTError as e:
            logger.warning(f"JWT verification failed: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Unexpected error during token verification: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token verification failed",
                headers={"WWW-Authenticate": "Bearer"},
            )
    
    def create_test_token(self, user_id: str, username: str = None, scopes: list[str] = None) -> str:
        """Create a test token for development/testing"""
        data = {
            "sub": user_id,
            "username": username or f"user_{user_id}",
            "scopes": scopes or ["read", "write", "admin"]
        }
        return self.create_access_token(data)


# Global OAuth handler instance
oauth_handler = OAuthHandler()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> TokenData:
    """FastAPI dependency to get current authenticated user"""
    return oauth_handler.verify_token(credentials.credentials)


async def get_current_user_id(current_user: TokenData = Depends(get_current_user)) -> str:
    """FastAPI dependency to get current user ID"""
    return current_user.user_id


def require_scopes(required_scopes: list[str]):
    """Dependency factory for scope-based authorization"""
    def scope_checker(current_user: TokenData = Depends(get_current_user)) -> TokenData:
        if not any(scope in current_user.scopes for scope in required_scopes):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required scopes: {required_scopes}"
            )
        return current_user
    return scope_checker


# Common scope checkers
require_read = require_scopes(["read", "admin"])
require_write = require_scopes(["write", "admin"]) 
require_admin = require_scopes(["admin"])