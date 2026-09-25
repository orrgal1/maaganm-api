import os

from dotenv import load_dotenv

# Load .env.local first (takes precedence for local overrides/secrets), then .env
load_dotenv(".env.local")
load_dotenv(".env")

# Budget service credentials and endpoint
BUDGET_BASE_URL = os.getenv("BUDGET_BASE_URL", "https://budget.mmm.org.il").rstrip("/")
BUDGET_USERNAME = os.getenv("BUDGET_USERNAME", "")
BUDGET_PASSWORD = os.getenv("BUDGET_PASSWORD", "")

# Help service member and endpoints
HELP_BASE_URL = os.getenv("HELP_BASE_URL", "https://help.mmm.org.il").rstrip("/")
HELP_SCHEDULER_BASE_URL = os.getenv(
    "HELP_SCHEDULER_BASE_URL",
    "https://hh-add.mmm.org.il",
).rstrip("/")
HELP_MEMBER_ID = os.getenv("HELP_MEMBER_ID", "")

# Gmail command-bus runtime
MAAGANM_EMAIL_HMAC_SECRET = os.getenv("MAAGANM_EMAIL_HMAC_SECRET", "")
MAAGANM_EMAIL_ALIAS = os.getenv(
    "MAAGANM_EMAIL_ALIAS",
    "orrgal+agents+maaganm@gmail.com",
)
MAAGANM_EMAIL_SENDER = os.getenv(
    "MAAGANM_EMAIL_SENDER",
    "orgal@mail.instinct.com",
)
MAAGANM_EMAIL_LABEL_ID = os.getenv("MAAGANM_EMAIL_LABEL_ID", "Label_35")
MAAGANM_EMAIL_LABEL_NAME = os.getenv("MAAGANM_EMAIL_LABEL_NAME", "Agents")
MAAGANM_EMAIL_DB_PATH = os.getenv(
    "MAAGANM_EMAIL_DB_PATH",
    "maaganm_email.sqlite3",
)
MAAGANM_EMAIL_POLL_SECONDS = float(
    os.getenv("MAAGANM_EMAIL_POLL_SECONDS", "30")
)
GAPI_BIN = os.getenv("GAPI_BIN", "gapi")
GAPI_MAX_OUTPUT_BYTES = int(os.getenv("GAPI_MAX_OUTPUT_BYTES", str(8 * 1024 * 1024)))

# Operating mode
HEADLESS = os.getenv("HEADLESS", "true").strip().lower() in ("true", "1", "yes")
MOCK_MODE = os.getenv("MOCK_MODE", "false").strip().lower() in ("true", "1", "yes")


def require_email_hmac_secret() -> str:
    """Return the command signing secret or fail worker startup."""
    if not MAAGANM_EMAIL_HMAC_SECRET:
        raise RuntimeError("MAAGANM_EMAIL_HMAC_SECRET is required")
    return MAAGANM_EMAIL_HMAC_SECRET
