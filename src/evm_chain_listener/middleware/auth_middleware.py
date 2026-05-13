"""
Shared JWT Authentication Middleware for ChainDetector Services

This module provides JWT token verification middleware that can be used
across all ChainDetector backend services (MarketDataBase, EVMLogListener, AlertProcessor).

Usage:
    from auth_middleware import get_current_user, require_auth, require_admin

    @app.get("/api/protected")
    async def protected_route(current_user: dict = Depends(get_current_user)):
        return {"user": current_user}
"""

from typing import Optional
from datetime import datetime
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt
import os

# JWT Configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-super-secret-jwt-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

security = HTTPBearer()


class JWTVerifier:
    """JWT Token verification utility"""

    def __init__(self, secret_key: str = JWT_SECRET_KEY, algorithm: str = JWT_ALGORITHM):
        self.secret_key = secret_key
        self.algorithm = algorithm

    def verify_token(self, token: str) -> dict:
        """
        Verify JWT token and return payload

        Args:
            token: JWT token string

        Returns:
            dict: Token payload containing user information

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm]
            )

            # Check expiration
            exp = payload.get("exp")
            if exp and datetime.utcnow().timestamp() > exp:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token has expired",
                    headers={"WWW-Authenticate": "Bearer"},
                )

            return payload

        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {str(e)}",
                headers={"WWW-Authenticate": "Bearer"},
            )


# Global verifier instance
jwt_verifier = JWTVerifier()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """
    FastAPI dependency to get current authenticated user from JWT token

    Usage:
        @app.get("/api/protected")
        async def protected_route(current_user: dict = Depends(get_current_user)):
            return {"user": current_user}

    Returns:
        dict: User information from token payload
            {
                "user_id": int,
                "username": str,
                "email": str,
                "role": str,
                "permissions": list[str],
                "exp": int,
                "iat": int
            }
    """
    token = credentials.credentials
    return jwt_verifier.verify_token(token)


async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> Optional[dict]:
    """
    FastAPI dependency to get current user, but don't raise error if not authenticated

    Usage:
        @app.get("/api/optional-auth")
        async def optional_route(current_user: Optional[dict] = Depends(get_current_user_optional)):
            if current_user:
                return {"authenticated": True, "user": current_user}
            return {"authenticated": False}

    Returns:
        Optional[dict]: User information if authenticated, None otherwise
    """
    if not credentials:
        return None

    try:
        token = credentials.credentials
        return jwt_verifier.verify_token(token)
    except HTTPException:
        return None


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    FastAPI dependency to require admin role

    Usage:
        @app.delete("/api/admin/users/{user_id}")
        async def delete_user(user_id: int, admin: dict = Depends(require_admin)):
            # Only admins can access this endpoint
            pass

    Returns:
        dict: User information (guaranteed to be admin)

    Raises:
        HTTPException: If user is not admin
    """
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


async def require_permission(permission: str):
    """
    Factory function to create a dependency that requires specific permission

    Usage:
        require_write = require_permission("write")

        @app.post("/api/data")
        async def create_data(user: dict = Depends(require_write)):
            pass

    Args:
        permission: Required permission string

    Returns:
        Callable: Dependency function
    """
    async def check_permission(current_user: dict = Depends(get_current_user)) -> dict:
        permissions = current_user.get("permissions", [])
        if permission not in permissions and "admin" not in permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' required"
            )
        return current_user

    return check_permission


def verify_token_sync(token: str) -> dict:
    """
    Synchronous version of token verification (for non-async contexts)

    Args:
        token: JWT token string

    Returns:
        dict: Token payload

    Raises:
        Exception: If token is invalid
    """
    return jwt_verifier.verify_token(token)
