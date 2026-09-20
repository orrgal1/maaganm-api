import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Load .env.local first (takes precedence for local overrides/secrets), then .env
load_dotenv(".env.local")
load_dotenv(".env")

# Authentication token for API access (static token fallback)
MAAGANM_API_TOKEN = os.getenv("MAAGANM_API_TOKEN", "")

# JWT Secret & Algorithm
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    secret_file = Path(".jwt_secret")
    if secret_file.exists():
        JWT_SECRET = secret_file.read_text().strip()
    else:
        JWT_SECRET = secrets.token_hex(32)
        try:
            secret_file.write_text(JWT_SECRET)
        except Exception:
            pass

JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")

# Target Service URL
BUDGET_BASE_URL = os.getenv("BUDGET_BASE_URL", "https://budget.mmm.org.il").rstrip("/")

# Optional local fallback credentials for budget.mmm.org.il
# Caller can pass them dynamically via X-Budget-Username / X-Budget-Password headers
BUDGET_USERNAME = os.getenv("BUDGET_USERNAME", "")
BUDGET_PASSWORD = os.getenv("BUDGET_PASSWORD", "")

# HTTP Server bind configuration
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8001"))

# Operating mode
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("true", "1", "yes")
MOCK_MODE = os.getenv("MOCK_MODE", "false").strip().lower() in ("true", "1", "yes")
