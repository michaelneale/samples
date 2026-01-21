#!/usr/bin/env python3
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

"""Test script for MCP endpoint.

Run the server first:
  uv run server.py --products_db_path=/tmp/ucp_test/products.db \
    --transactions_db_path=/tmp/ucp_test/transactions.db --port=8182

Then run this test:
  uv run python test_mcp.py
"""

import json
import uuid

import httpx


BASE_URL = "http://localhost:8182"
MCP_ENDPOINT = f"{BASE_URL}/ucp/mcp"


def jsonrpc_request(method: str, params: dict, request_id: int = 1) -> dict:
  """Make a JSON-RPC 2.0 request."""
  payload = {
    "jsonrpc": "2.0",
    "method": method,
    "params": params,
    "id": request_id,
  }
  response = httpx.post(MCP_ENDPOINT, json=payload)
  return response.json()


def test_discovery():
  """Test that MCP is advertised in discovery."""
  print("\n=== Testing Discovery ===")
  response = httpx.get(f"{BASE_URL}/.well-known/ucp")
  profile = response.json()
  mcp_config = profile.get("ucp", {}).get("services", {}).get("dev.ucp.shopping", {}).get("mcp")
  print(f"MCP endpoint advertised: {mcp_config}")
  assert mcp_config is not None, "MCP should be advertised in discovery"
  assert "endpoint" in mcp_config, "MCP endpoint should be present"
  print("✓ Discovery test passed")


def test_create_checkout():
  """Test create_checkout MCP method."""
  print("\n=== Testing create_checkout ===")
  params = {
    "_meta": {
      "ucp": {
        "profile": "https://platform.example/profiles/v2026-01/shopping-agent.json"
      }
    },
    "idempotency_key": str(uuid.uuid4()),
    "buyer": {
      "email": "test@example.com",
      "full_name": "Test User"
    },
    "line_items": [
      {
        "item": {
          "id": "bouquet_roses",
          "title": "Red Roses"
        },
        "quantity": 1
      }
    ],
    "currency": "USD"
  }

  result = jsonrpc_request("create_checkout", params, request_id=1)
  print(f"Response: {json.dumps(result, indent=2)}")

  assert "result" in result, f"Expected result, got error: {result.get('error')}"
  checkout = result["result"]
  assert "id" in checkout, "Checkout should have an ID"
  print(f"✓ Created checkout: {checkout['id']}")
  return checkout["id"]


def test_get_checkout(checkout_id: str):
  """Test get_checkout MCP method."""
  print(f"\n=== Testing get_checkout ({checkout_id}) ===")
  params = {"id": checkout_id}

  result = jsonrpc_request("get_checkout", params, request_id=2)
  print(f"Response: {json.dumps(result, indent=2)}")

  assert "result" in result, f"Expected result, got error: {result.get('error')}"
  checkout = result["result"]
  assert checkout["id"] == checkout_id
  print(f"✓ Got checkout: {checkout['id']}, status: {checkout.get('status')}")


def test_update_checkout(checkout_id: str):
  """Test update_checkout MCP method."""
  print(f"\n=== Testing update_checkout ({checkout_id}) ===")
  params = {
    "_meta": {
      "ucp": {
        "profile": "https://platform.example/profiles/v2026-01/shopping-agent.json"
      }
    },
    "id": checkout_id,
    "idempotency_key": str(uuid.uuid4()),
    "buyer": {
      "email": "updated@example.com",
      "full_name": "Updated User"
    },
    "line_items": [
      {
        "item": {
          "id": "bouquet_roses",
          "title": "Red Roses"
        },
        "quantity": 2
      }
    ],
    "currency": "USD"
  }

  result = jsonrpc_request("update_checkout", params, request_id=3)
  print(f"Response: {json.dumps(result, indent=2)}")

  assert "result" in result, f"Expected result, got error: {result.get('error')}"
  checkout = result["result"]
  assert checkout["buyer"]["email"] == "updated@example.com"
  print(f"✓ Updated checkout, new email: {checkout['buyer']['email']}")


def test_cancel_checkout(checkout_id: str):
  """Test cancel_checkout MCP method."""
  print(f"\n=== Testing cancel_checkout ({checkout_id}) ===")
  params = {
    "id": checkout_id,
    "idempotency_key": str(uuid.uuid4()),
  }

  result = jsonrpc_request("cancel_checkout", params, request_id=4)
  print(f"Response: {json.dumps(result, indent=2)}")

  assert "result" in result, f"Expected result, got error: {result.get('error')}"
  checkout = result["result"]
  assert checkout["status"] == "canceled"
  print(f"✓ Canceled checkout, status: {checkout['status']}")


def test_method_not_found():
  """Test that unknown methods return proper error."""
  print("\n=== Testing method_not_found ===")
  result = jsonrpc_request("unknown_method", {}, request_id=99)
  print(f"Response: {json.dumps(result, indent=2)}")

  assert "error" in result, "Expected error for unknown method"
  assert result["error"]["code"] == -32601  # Method not found
  print("✓ Method not found error returned correctly")


def main():
  """Run all MCP tests."""
  print("=" * 60)
  print("UCP MCP Transport Test Suite")
  print("=" * 60)

  try:
    # Test discovery
    test_discovery()

    # Test checkout flow
    checkout_id = test_create_checkout()
    test_get_checkout(checkout_id)
    test_update_checkout(checkout_id)
    test_cancel_checkout(checkout_id)

    # Test error handling
    test_method_not_found()

    print("\n" + "=" * 60)
    print("All tests passed! ✓")
    print("=" * 60)

  except httpx.ConnectError:
    print("\n❌ Error: Could not connect to server at", BASE_URL)
    print("Make sure the server is running:")
    print("  uv run server.py --products_db_path=/tmp/ucp_test/products.db \\")
    print("    --transactions_db_path=/tmp/ucp_test/transactions.db --port=8182")
  except AssertionError as e:
    print(f"\n❌ Test failed: {e}")
    raise


if __name__ == "__main__":
  main()
