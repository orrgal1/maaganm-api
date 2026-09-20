#!/usr/bin/env bash
set -euo pipefail

# This script creates or updates .env.local with your credentials.
# .env.local is strictly git-ignored and will never be committed.

TARGET_FILE=".env.local"

if [ "$#" -ge 2 ]; then
  USERNAME="$1"
  PASSWORD="$2"
else
  read -r -p "Enter Maagan Michael Budget Username (employee ID): " USERNAME
  read -r -s -p "Enter Maagan Michael Budget Password: " PASSWORD
  echo ""
fi

cat << EOF > "$TARGET_FILE"
# Kibbutz Maagan Michael Budget Portal Credentials
BUDGET_USERNAME="$USERNAME"
BUDGET_PASSWORD="$PASSWORD"

# Local Server Settings
HOST="127.0.0.1"
PORT=8001
MOCK_MODE=false
EOF

chmod 600 "$TARGET_FILE"
echo "Successfully written credentials to $TARGET_FILE (mode 0600, untracked by git)."
