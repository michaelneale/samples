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
    "name": "create_checkout",
    "description": "Create a new checkout session",
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
    "description": "Get the current state of a checkout session",
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
    "description": "Update a checkout session with new information",
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
    "description": "Complete the checkout and place the order",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {"type": "string", "description": "Checkout session ID"},
        "payment": {"type": "object", "description": "Payment instrument data"},
        "idempotency_key": {"type": "string", "description": "UUID for idempotency"},
      },
      "required": ["id", "idempotency_key"],
    },
  },
  {
    "name": "cancel_checkout",
    "description": "Cancel a checkout session",
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


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
  """Handle MCP initialize method."""
  return {
    "protocolVersion": MCP_PROTOCOL_VERSION,
    "capabilities": {
      "tools": {},
    },
    "serverInfo": {
      "name": SERVER_NAME,
      "version": SERVER_VERSION,
    },
  }


def handle_initialized() -> dict[str, Any]:
  """Handle MCP initialized notification."""
  return {}


def handle_tools_list() -> dict[str, Any]:
  """Handle tools/list method."""
  return {"tools": TOOLS}


async def handle_tools_call(
  params: dict[str, Any],
  checkout_service: CheckoutService,
) -> dict[str, Any]:
  """Handle tools/call method - routes to UCP checkout tools."""
  tool_name = params.get("name")
  arguments = params.get("arguments", {})

  if tool_name == "create_checkout":
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

  return {
    "content": [
      {
        "type": "text",
        "text": str(result),
      }
    ],
    "isError": False,
  }


async def process_mcp_request(
  body: dict[str, Any],
  checkout_service: CheckoutService,
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
      result = await handle_tools_call(params, checkout_service)
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


@router.post("/ucp/mcp")
async def mcp_endpoint(
  request: Request,
  checkout_service: Annotated[
    CheckoutService, Depends(dependencies.get_checkout_service)
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

  result = await process_mcp_request(body, checkout_service)

  # If result is None, it was a notification - return 202 Accepted
  if result is None:
    return JSONResponse(content={}, status_code=202)

  if use_sse:
    async def generate():
      yield {"event": "message", "data": json.dumps(result)}

    return EventSourceResponse(generate())
  else:
    return JSONResponse(content=result)


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
