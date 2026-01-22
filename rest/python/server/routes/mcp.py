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

"""MCP (Model Context Protocol) transport binding for UCP Shopping Service.

This module implements the full MCP protocol (JSON-RPC 2.0 over HTTP) for the
UCP checkout capability, following both:
- MCP spec: https://modelcontextprotocol.io/
- UCP MCP binding: https://ucp.dev/specification/checkout-mcp

MCP Protocol Methods:
  - initialize
  - initialized  
  - tools/list
  - tools/call

UCP Tools (via tools/call):
  - create_checkout
  - get_checkout
  - update_checkout
  - complete_checkout
  - cancel_checkout
"""

import json
import logging
import uuid
from typing import Annotated, Any

import dependencies
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
import models
from pydantic import BaseModel
import db
from services.checkout_service import CheckoutService
from ucp_sdk.models.schemas.shopping.order import PlatformConfig
from ucp_sdk.models.schemas.shopping.payment_create_req import (
  PaymentCreateRequest,
)
from ucp_sdk.models.schemas.shopping.types.payment_instrument import (
  PaymentInstrument,
)

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "ucp-shopping-mcp"
SERVER_VERSION = "0.1.0"

# Tool definitions for tools/list
TOOLS = [
  {
    "name": "get_merchant_info",
    "description": """Get merchant information and capabilities (UCP discovery).

Returns:
- UCP version and supported capabilities (checkout, fulfillment, discount, etc.)
- Available payment handlers (google_pay, shop_pay, mock_payment_handler)
- Service endpoints

This is the MCP equivalent of fetching /.well-known/ucp""",
    "inputSchema": {
      "type": "object",
      "properties": {},
    },
  },
  {
    "name": "list_products",
    "description": """List available products in the store catalog.

Returns products with their IDs, titles, and prices. Use the product 'id' field
when creating checkouts (in line_items.item.id).""",
    "inputSchema": {
      "type": "object",
      "properties": {},
    },
  },
  {
    "name": "create_checkout",
    "description": """Create a new checkout session for purchasing items.

Returns a checkout object with:
- id: Use this for subsequent update/complete calls
- status: 'incomplete' (needs more info) or 'ready_for_complete' (ready for payment)
- continue_url: URL where the user can complete payment in a secure UI
- totals: Price breakdown

IMPORTANT: When status is 'ready_for_complete', present the continue_url to the user 
so they can complete payment securely. Do NOT attempt to collect payment details directly.""",
    "inputSchema": {
      "type": "object",
      "properties": {
        "buyer": {
          "type": "object",
          "description": "Buyer information",
          "properties": {
            "email": {"type": "string"},
            "full_name": {"type": "string"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
          },
        },
        "line_items": {
          "type": "array",
          "description": "Items to purchase",
          "items": {
            "type": "object",
            "properties": {
              "item": {
                "type": "object",
                "properties": {
                  "id": {"type": "string"},
                  "title": {"type": "string"},
                },
                "required": ["id"],
              },
              "quantity": {"type": "integer"},
            },
            "required": ["item", "quantity"],
          },
        },
        "currency": {"type": "string", "description": "ISO 4217 currency code"},
        "idempotency_key": {"type": "string", "description": "UUID for idempotency"},
      },
      "required": ["line_items", "currency"],
    },
  },
  {
    "name": "get_checkout",
    "description": """Get the current state of a checkout session.

Check the 'status' field:
- 'incomplete': More information needed (check 'messages' for what's missing)
- 'ready_for_complete': Ready for payment - present continue_url to user
- 'completed': Order placed successfully
- 'canceled': Session was canceled""",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {"type": "string", "description": "Checkout session ID"},
      },
      "required": ["id"],
    },
  },
  {
    "name": "update_checkout",
    "description": """Update a checkout session with buyer info, shipping, or discounts.

Use this to:
- Add/update buyer email and name
- Set shipping address (in fulfillment.methods)
- Apply discount codes (in discounts.codes)
- Modify line items

After updating, check if status becomes 'ready_for_complete'. If so, present 
the continue_url to the user for secure payment completion.""",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {"type": "string", "description": "Checkout session ID"},
        "buyer": {"type": "object", "description": "Updated buyer information"},
        "line_items": {"type": "array", "description": "Updated line items"},
        "fulfillment": {"type": "object", "description": "Fulfillment/shipping details"},
        "discounts": {"type": "object", "description": "Discount codes to apply"},
        "idempotency_key": {"type": "string"},
      },
      "required": ["id"],
    },
  },
  {
    "name": "complete_checkout",
    "description": """Complete the checkout and place the order. 

WARNING: This requires payment credentials. In most cases, you should NOT call this 
directly. Instead, present the continue_url to the user so they can complete payment 
securely through the merchant's payment UI (Google Pay, credit card form, etc).

Only use this if you have a pre-authorized payment token (e.g., from AP2 mandate).""",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {"type": "string", "description": "Checkout session ID"},
        "payment": {"type": "object", "description": "Payment instrument with pre-authorized token"},
        "idempotency_key": {"type": "string", "description": "UUID for idempotency"},
      },
      "required": ["id", "idempotency_key"],
    },
  },
  {
    "name": "cancel_checkout",
    "description": "Cancel a checkout session. Use if the user wants to abandon the purchase.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {"type": "string", "description": "Checkout session ID"},
        "idempotency_key": {"type": "string", "description": "UUID for idempotency"},
      },
      "required": ["id", "idempotency_key"],
    },
  },
]

logger = logging.getLogger(__name__)

router = APIRouter()


class JsonRpcRequest(BaseModel):
  """JSON-RPC 2.0 request structure."""

  jsonrpc: str = "2.0"
  method: str
  params: dict[str, Any] = {}
  id: int | str | None = None


class JsonRpcError(BaseModel):
  """JSON-RPC 2.0 error structure."""

  code: int
  message: str
  data: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
  """JSON-RPC 2.0 response structure."""

  jsonrpc: str = "2.0"
  result: dict[str, Any] | None = None
  error: JsonRpcError | None = None
  id: int | str | None = None


# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def make_error_response(
  code: int,
  message: str,
  request_id: int | str | None = None,
  data: dict[str, Any] | None = None,
) -> JsonRpcResponse:
  """Create a JSON-RPC error response."""
  return JsonRpcResponse(
    id=request_id,
    error=JsonRpcError(code=code, message=message, data=data),
  )


def make_success_response(
  result: dict[str, Any],
  request_id: int | str | None = None,
) -> JsonRpcResponse:
  """Create a JSON-RPC success response."""
  return JsonRpcResponse(id=request_id, result=result)


def extract_platform_profile(params: dict[str, Any]) -> PlatformConfig | None:
  """Extract platform config from _meta.ucp.profile in params."""
  meta = params.get("_meta", {})
  ucp_meta = meta.get("ucp", {})
  profile_url = ucp_meta.get("profile")
  if profile_url:
    return PlatformConfig(webhook_url=profile_url)
  return None


def get_ucp_discovery() -> dict[str, Any]:
  """Get full UCP discovery profile."""
  import pathlib
  import re
  profile_path = pathlib.Path(__file__).parent / "discovery_profile.json"
  with profile_path.open() as f:
    content = f.read()
    content = re.sub(r'\{\{ENDPOINT\}\}', 'http://localhost:8182', content)
    content = re.sub(r'\{\{SHOP_ID\}\}', 'test-shop-id', content)
    return json.loads(content)


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
  """Handle MCP initialize method."""
  discovery = get_ucp_discovery()
  
  return {
    "protocolVersion": MCP_PROTOCOL_VERSION,
    "capabilities": {
      "tools": {},
    },
    "serverInfo": {
      "name": SERVER_NAME,
      "version": SERVER_VERSION,
      "ucp": {
        "version": discovery.get("ucp", {}).get("version"),
        "capabilities": [c["name"] for c in discovery.get("ucp", {}).get("capabilities", [])],
        "paymentHandlers": [h["id"] for h in discovery.get("payment", {}).get("handlers", [])],
      },
    },
  }


def handle_initialized() -> dict[str, Any]:
  """Handle MCP initialized notification."""
  return {}


def handle_tools_list() -> dict[str, Any]:
  """Handle tools/list method."""
  return {"tools": TOOLS}


def format_checkout_response(result: dict[str, Any]) -> list[dict[str, Any]]:
  """Format checkout result with helpful instructions based on status."""
  status = result.get("status")
  continue_url = result.get("continue_url")
  
  content = [
    {
      "type": "text", 
      "text": json.dumps(result, indent=2),
    }
  ]
  
  # Add clear instructions based on checkout status
  if status == "ready_for_complete" and continue_url:
    content.append({
      "type": "text",
      "text": f"\n✅ CHECKOUT READY FOR PAYMENT\n\nThe checkout is ready. Present this link to the user to complete payment securely:\n{continue_url}\n\nDo NOT attempt to collect payment details directly.",
    })
  elif status == "incomplete":
    messages = result.get("messages", [])
    if messages:
      msg_text = "\n".join(f"- {m.get('content', m.get('message', str(m)))}" for m in messages)
      content.append({
        "type": "text",
        "text": f"\n⚠️ CHECKOUT INCOMPLETE\n\nMissing information:\n{msg_text}\n\nUse update_checkout to provide the missing details.",
      })
  elif status == "completed":
    order = result.get("order", {})
    content.append({
      "type": "text",
      "text": f"\n🎉 ORDER PLACED\n\nOrder ID: {order.get('id')}\nOrder URL: {order.get('permalink_url')}",
    })
  
  return content


async def handle_tools_call(
  params: dict[str, Any],
  checkout_service: CheckoutService,
  products_session,
) -> dict[str, Any]:
  """Handle tools/call method - routes to UCP checkout tools."""
  tool_name = params.get("name")
  arguments = params.get("arguments", {})

  if tool_name == "get_merchant_info":
    result = get_ucp_discovery()
  elif tool_name == "list_products":
    result = await handle_list_products(products_session)
  elif tool_name == "create_checkout":
    result = await handle_create_checkout(arguments, checkout_service)
  elif tool_name == "get_checkout":
    result = await handle_get_checkout(arguments, checkout_service)
  elif tool_name == "update_checkout":
    result = await handle_update_checkout(arguments, checkout_service)
  elif tool_name == "complete_checkout":
    result = await handle_complete_checkout(arguments, checkout_service)
  elif tool_name == "cancel_checkout":
    result = await handle_cancel_checkout(arguments, checkout_service)
  else:
    raise ValueError(f"Unknown tool: {tool_name}")

  # Format response with helpful instructions for checkout-related tools
  if tool_name in ("create_checkout", "get_checkout", "update_checkout", "complete_checkout"):
    content = format_checkout_response(result)
  else:
    content = [{"type": "text", "text": json.dumps(result, indent=2)}]

  return {
    "content": content,
    "isError": False,
  }


async def process_mcp_request(
  body: dict[str, Any],
  checkout_service: CheckoutService,
  products_session,
) -> dict[str, Any] | None:
  """Process a single MCP JSON-RPC request and return response dict.
  
  Returns None for notifications (no id).
  """
  try:
    rpc_request = JsonRpcRequest(**body)
  except Exception as e:
    return make_error_response(INVALID_REQUEST, f"Invalid request: {e}").model_dump(exclude_none=True)

  method = rpc_request.method
  params = rpc_request.params
  request_id = rpc_request.id

  # Notifications have no id and expect no response
  is_notification = request_id is None

  try:
    # MCP protocol methods
    if method == "initialize":
      result = handle_initialize(params)
    elif method == "initialized":
      # This is always a notification, no response needed
      return None
    elif method == "notifications/initialized":
      # Alternative form
      return None
    elif method == "tools/list":
      result = handle_tools_list()
    elif method == "tools/call":
      result = await handle_tools_call(params, checkout_service, products_session)
    # Direct UCP methods (for backwards compatibility / direct JSON-RPC calls)
    elif method == "create_checkout":
      result = await handle_create_checkout(params, checkout_service)
    elif method == "get_checkout":
      result = await handle_get_checkout(params, checkout_service)
    elif method == "update_checkout":
      result = await handle_update_checkout(params, checkout_service)
    elif method == "complete_checkout":
      result = await handle_complete_checkout(params, checkout_service)
    elif method == "cancel_checkout":
      result = await handle_cancel_checkout(params, checkout_service)
    else:
      if is_notification:
        return None
      return make_error_response(
        METHOD_NOT_FOUND,
        f"Method not found: {method}",
        request_id,
      ).model_dump(exclude_none=True)

    if is_notification:
      return None

    return make_success_response(result, request_id).model_dump(exclude_none=True)

  except ValueError as e:
    if is_notification:
      return None
    return make_error_response(
      INVALID_PARAMS,
      str(e),
      request_id,
      data={"status": "error", "errors": [{"code": "INVALID_PARAMS", "message": str(e)}]},
    ).model_dump(exclude_none=True)
  except Exception as e:
    logger.exception("MCP method %s failed", method)
    if is_notification:
      return None
    return make_error_response(
      INTERNAL_ERROR,
      str(e),
      request_id,
      data={"status": "error", "errors": [{"code": "INTERNAL_ERROR", "message": str(e)}]},
    ).model_dump(exclude_none=True)


async def handle_list_products(
  products_session,
) -> dict[str, Any]:
  """Handle list_products MCP method."""
  products = await db.get_all_products(products_session)
  return {
    "products": [
      {
        "id": p.id,
        "title": p.title,
        "price": p.price,
        "price_formatted": f"${p.price / 100:.2f}",
        "image_url": p.image_url,
      }
      for p in products
    ]
  }


@router.post("/ucp/mcp")
async def mcp_endpoint(
  request: Request,
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
  ],
  products_session: Annotated[
    Any, Depends(dependencies.get_products_db)
  ],
):
  """MCP JSON-RPC 2.0 endpoint for UCP Shopping Service.

  Supports both regular JSON responses and SSE streaming based on Accept header.

  Implements MCP protocol methods:
    - initialize
    - initialized (notification)
    - tools/list
    - tools/call

  UCP checkout tools are exposed via tools/call.
  """
  accept = request.headers.get("accept", "")
  use_sse = "text/event-stream" in accept

  try:
    body = await request.json()
  except Exception:
    resp = make_error_response(PARSE_ERROR, "Parse error")
    return JSONResponse(content=resp.model_dump(exclude_none=True))

  result = await process_mcp_request(body, checkout_service, products_session)

  # If result is None, it was a notification - return 202 Accepted
  if result is None:
    return JSONResponse(content={}, status_code=202)

  if use_sse:
    async def generate():
      yield {"event": "message", "data": json.dumps(result)}

    return EventSourceResponse(generate())
  else:
    return JSONResponse(content=result)


def get_default_payment_handlers() -> list[dict[str, Any]]:
  """Get payment handlers from discovery profile."""
  import pathlib
  import re
  profile_path = pathlib.Path(__file__).parent / "discovery_profile.json"
  with profile_path.open() as f:
    content = f.read()
    # Replace template placeholders with valid JSON strings
    content = re.sub(r'\{\{ENDPOINT\}\}', 'http://localhost:8182', content)
    content = re.sub(r'\{\{SHOP_ID\}\}', 'test-shop-id', content)
    profile = json.loads(content)
  return profile.get("payment", {}).get("handlers", [])


async def handle_create_checkout(
  params: dict[str, Any],
  checkout_service: CheckoutService,
) -> dict[str, Any]:
  """Handle create_checkout MCP method."""
  platform_config = extract_platform_profile(params)

  # Remove _meta before passing to checkout service
  checkout_params = {k: v for k, v in params.items() if k != "_meta"}

  # Get or generate idempotency key
  idempotency_key = checkout_params.pop("idempotency_key", str(uuid.uuid4()))

  # Provide default payment with handlers from discovery profile
  if "payment" not in checkout_params:
    checkout_params["payment"] = {
      "instruments": [],
      "handlers": get_default_payment_handlers(),
    }
  elif "handlers" not in checkout_params.get("payment", {}):
    checkout_params["payment"]["handlers"] = get_default_payment_handlers()
    checkout_params["payment"].setdefault("instruments", [])

  # Build checkout request
  checkout_req = models.UnifiedCheckoutCreateRequest(**checkout_params)

  result = await checkout_service.create_checkout(
    checkout_req,
    idempotency_key,
    platform_config,
  )
  return result.model_dump(mode="json", by_alias=True)


async def handle_get_checkout(
  params: dict[str, Any],
  checkout_service: CheckoutService,
) -> dict[str, Any]:
  """Handle get_checkout MCP method."""
  checkout_id = params.get("id")
  if not checkout_id:
    raise ValueError("Missing required parameter: id")

  result = await checkout_service.get_checkout(checkout_id)
  return result.model_dump(mode="json", by_alias=True)


async def handle_update_checkout(
  params: dict[str, Any],
  checkout_service: CheckoutService,
) -> dict[str, Any]:
  """Handle update_checkout MCP method."""
  checkout_id = params.get("id")
  if not checkout_id:
    raise ValueError("Missing required parameter: id")

  platform_config = extract_platform_profile(params)

  # Get checkout data - either from 'checkout' param or directly in params
  checkout_params = params.get("checkout", {})
  if not checkout_params:
    # Params might be flat (checkout fields directly in params)
    checkout_params = {k: v for k, v in params.items() if k not in ("id", "_meta", "idempotency_key")}

  idempotency_key = params.get("idempotency_key", str(uuid.uuid4()))

  # Provide default payment with handlers from discovery profile
  if "payment" not in checkout_params:
    checkout_params["payment"] = {
      "instruments": [],
      "handlers": get_default_payment_handlers(),
    }
  elif "handlers" not in checkout_params.get("payment", {}):
    checkout_params["payment"]["handlers"] = get_default_payment_handlers()
    checkout_params["payment"].setdefault("instruments", [])

  checkout_req = models.UnifiedCheckoutUpdateRequest(**checkout_params)

  result = await checkout_service.update_checkout(
    checkout_id,
    checkout_req,
    idempotency_key,
    platform_config,
  )
  return result.model_dump(mode="json", by_alias=True)


async def handle_complete_checkout(
  params: dict[str, Any],
  checkout_service: CheckoutService,
) -> dict[str, Any]:
  """Handle complete_checkout MCP method."""
  checkout_id = params.get("id")
  if not checkout_id:
    raise ValueError("Missing required parameter: id")

  idempotency_key = params.get("idempotency_key")
  if not idempotency_key:
    raise ValueError("Missing required parameter: idempotency_key")

  # Payment data from params
  payment_data = params.get("payment", {})

  # Build payment request - service requires this
  instruments = payment_data.get("instruments", [])
  if instruments:
    payment_instruments = [PaymentInstrument(root=inst) for inst in instruments]
    payment_req = PaymentCreateRequest(
      selected_instrument_id=payment_data.get("selected_instrument_id"),
      instruments=payment_instruments,
    )
  elif payment_data.get("id"):
    # Single instrument passed directly
    payment_instruments = [PaymentInstrument(root=payment_data)]
    payment_req = PaymentCreateRequest(
      selected_instrument_id=payment_data.get("id"),
      instruments=payment_instruments,
    )
  else:
    # Default empty payment request
    payment_req = PaymentCreateRequest(
      selected_instrument_id=None,
      instruments=[],
    )

  result = await checkout_service.complete_checkout(
    checkout_id,
    payment_req,
    {},  # risk_signals
    idempotency_key,
  )
  return result.model_dump(mode="json", by_alias=True)


async def handle_cancel_checkout(
  params: dict[str, Any],
  checkout_service: CheckoutService,
) -> dict[str, Any]:
  """Handle cancel_checkout MCP method."""
  checkout_id = params.get("id")
  if not checkout_id:
    raise ValueError("Missing required parameter: id")

  idempotency_key = params.get("idempotency_key")
  if not idempotency_key:
    raise ValueError("Missing required parameter: idempotency_key")

  result = await checkout_service.cancel_checkout(checkout_id, idempotency_key)
  return result.model_dump(mode="json", by_alias=True)
