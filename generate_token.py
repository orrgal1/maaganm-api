import argparse
import sys
from pathlib import Path
from security import generate_jwt_token

def main():
    parser = argparse.ArgumentParser(description="Generate JWT Bearer token for maaganm-api")
    parser.add_argument("--subject", default="budget-agent", help="Subject identifier for the token")
    parser.add_argument("--days", type=int, default=365, help="Token validity in days")
    parser.add_argument("--save", action="store_true", default=True, help="Save token to .current_jwt")
    args = parser.parse_args()

    token = generate_jwt_token(subject=args.subject, expires_days=args.days)
    if args.save:
        Path(".current_jwt").write_text(token.strip())
        print(f"Token saved to .current_jwt")

    print("\nGenerated JWT Token:")
    print(token)

if __name__ == "__main__":
    main()
