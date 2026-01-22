#!/bin/bash
#   Copyright 2026 UCP Authors
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

# Start the UCP Merchant Server with MCP support

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DB_DIR="${DB_DIR:-/tmp/ucp_test}"
PORT="${PORT:-8182}"

echo "=== UCP Merchant Server Setup ==="
echo ""

# Check for uv
if ! command -v uv &> /dev/null; then
  echo "Error: 'uv' is not installed."
  echo "Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi

# Install dependencies
echo "Installing dependencies..."
uv sync --quiet

# Create database directory
mkdir -p "$DB_DIR"

# Initialize database if it doesn't exist
if [ ! -f "$DB_DIR/products.db" ]; then
  echo "Initializing database with sample data..."
  uv run import_csv.py \
    --products_db_path="$DB_DIR/products.db" \
    --transactions_db_path="$DB_DIR/transactions.db" \
    --data_dir=../test_data/flower_shop
  echo "Database initialized."
else
  echo "Using existing database at $DB_DIR"
fi

echo ""
echo "=== Starting Server ==="
echo ""
echo "  REST endpoint: http://localhost:$PORT/"
echo "  MCP endpoint:  http://localhost:$PORT/ucp/mcp"
echo "  Discovery:     http://localhost:$PORT/.well-known/ucp"
echo ""
echo "  To test MCP:   uv run python test_mcp.py"
echo ""

uv run server.py \
  --products_db_path="$DB_DIR/products.db" \
  --transactions_db_path="$DB_DIR/transactions.db" \
  --port="$PORT"
