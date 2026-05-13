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

from typing import Optional, List
from datetime import datetime
from fastapi import Depends, HTTPException, status, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.middleware.base import BaseHTTPMiddleware
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


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for JWT authentication

    This middleware automatically verifies JWT tokens for all requests except public paths.
    """

    def __init__(self, app, secret_key: str, algorithm: str = "HS256", public_paths: List[str] = None):
        super().__init__(app)
        self.secret_key = secret_key
        self.algorithm = algorithm
        self.public_paths = public_paths or []
        self.verifier = JWTVerifier(secret_key, algorithm)

    async def dispatch(self, request: Request, call_next):
        # For public paths, try to authenticate if token is present, but don't require it
        is_public = request.url.path in self.public_paths

        # Check for Authorization header
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            if is_public:
                # Public path without token - allow through
                return await call_next(request)
            else:
                # Protected path without token - reject
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing authorization header"}
                )

        # Extract token
        try:
            scheme, token = auth_header.split()
            if scheme.lower() != "bearer":
                if is_public:
                    # Public path with invalid scheme - allow through without auth
                    return await call_next(request)
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Invalid authentication scheme"}
                )
        except ValueError:
            if is_public:
                # Public path with malformed header - allow through without auth
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid authorization header format"}
            )

        # Verify token
        try:
            payload = self.verifier.verify_token(token)
            request.state.user = payload
        except HTTPException as e:
            if is_public:
                # Public path with invalid token - allow through without auth
                return await call_next(request)
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail}
            )

        return await call_next(request)


async def get_current_user(request: Request) -> dict:
    """
    FastAPI dependency to get current authenticated user from request state
    (set by JWTAuthMiddleware)

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
    if not hasattr(request.state, "user"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return request.state.user


async def get_current_user_optional(request: Request) -> Optional[dict]:
    """
    FastAPI dependency to get current user from request state, but don't raise error if not authenticated

    Usage:
        @app.get("/api/optional-auth")
        async def optional_route(current_user: Optional[dict] = Depends(get_current_user_optional)):
            if current_user:
                return {"authenticated": True, "user": current_user}
            return {"authenticated": False}

    Returns:
        Optional[dict]: User information if authenticated, None otherwise
    """
    return getattr(request.state, "user", None)


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
