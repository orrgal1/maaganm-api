import re
import sys
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, Tuple
from fastapi import Request, status, Header
from fastapi.security.utils import get_authorization_scheme_param
import jwt

import config

logger = logging.getLogger("security")

PASSWORD_REGEX = re.compile(r'(?i)(password|secret|pass|token)[\s:=]+["\']?([^\s"\',&]+)["\']?')
JWT_REGEX = re.compile(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b')

def generate_jwt_token(
    subject: str = "budget-agent",
    expires_days: int = 365,
    extra_claims: Optional[Dict[str, Any]] = None
) -> str:
    """Generates a signed JWT Bearer token for API consumers."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=expires_days)).timestamp()),
        "iss": "maaganm-api"
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)

def sanitize_log_message(msg: str) -> str:
    """Sanitizes log messages by masking passwords, secrets, and JWT tokens."""
    cleaned = PASSWORD_REGEX.sub(r'\1="[REDACTED]"', msg)
    cleaned = JWT_REGEX.sub("[REDACTED_JWT]", cleaned)
    return cleaned

class APIException(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)

# Endpoints exempt from Bearer authentication for inspection
PUBLIC_PATHS = {"/openapi.json", "/docs", "/redoc", "/favicon.ico"}

async def verify_bearer_token(request: Request) -> Dict[str, Any]:
    """
    Verifies Bearer token.
    Accepts:
    1. A signed JWT matching config.JWT_SECRET
    2. A static token matching config.MAAGANM_API_TOKEN (if configured)
    """
    if request.url.path in PUBLIC_PATHS:
        return {"sub": "public"}

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="missing_token",
            message="Missing Authorization: Bearer <token> header"
        )

    scheme, param = get_authorization_scheme_param(auth_header)
    if scheme.lower() != "bearer" or not param:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_authorization_header",
            message="Authorization header must start with Bearer"
        )

    # Check static token fallback
    if config.MAAGANM_API_TOKEN and param == config.MAAGANM_API_TOKEN:
        return {"sub": "static-token"}

    # Attempt JWT decode
    try:
        payload = jwt.decode(
            param,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM]
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="token_expired",
            message="Bearer token has expired"
        )
    except jwt.InvalidTokenError as e:
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_token",
            message=f"Invalid Bearer token: {str(e)}"
        )

class BudgetCredentials:
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password

async def get_budget_credentials(
    request: Request,
    x_budget_username: Optional[str] = Header(None, alias="X-Budget-Username"),
    x_budget_password: Optional[str] = Header(None, alias="X-Budget-Password")
) -> BudgetCredentials:
    """
    Extracts caller credentials:
    1. Headers 'X-Budget-Username' and 'X-Budget-Password'
    2. Fallback to BUDGET_USERNAME and BUDGET_PASSWORD from .env.local
    3. If MOCK_MODE is enabled, default to mock credentials
    4. Otherwise, raises 401 credentials_missing
    """
    username = x_budget_username or config.BUDGET_USERNAME
    password = x_budget_password or config.BUDGET_PASSWORD

    if not username or not password:
        if config.MOCK_MODE:
            return BudgetCredentials(username="mock_user", password="mock_password")
        raise APIException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="credentials_missing",
            message="Budget credentials not provided. Pass 'X-Budget-Username' and 'X-Budget-Password' headers or set BUDGET_USERNAME and BUDGET_PASSWORD in .env.local"
        )

    return BudgetCredentials(username=username, password=password)
