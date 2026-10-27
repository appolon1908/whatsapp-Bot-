# Messaging integration contract

This document describes the interface implemented by the bot. Sibling services must be tested against this contract before outbound delivery is enabled. Local fake-transport tests are not evidence of provider compatibility.

## Service boundaries

Inbound path: provider adapter → Middleware V3 normalization/signing → bot inbox.

Outbound path: bot outbox → WhatsApp policy API → Middleware V3 → provider adapter. Policy decisions, provider credentials, campaign approval and provider delivery remain outside this repository. The bot never falls back to a direct provider endpoint.

Each business owns an operator-registered channel tuple `(provider, provider_account_id, phone_number_id)`. The signed tuple selects a stored tenant/channel binding. No inbound body or header may choose a tenant UUID. An inactive business, inactive channel, or channel without a verification reference cannot receive routed events.

## Authenticated inbound events

`POST /internal/v1/whatsapp/events`

Headers:

- `Content-Type: application/json`
- `X-Commerce-Timestamp`: Unix seconds in decimal ASCII.
- `X-Commerce-Signature`: `sha256=` followed by lowercase hex HMAC-SHA256.

Signed bytes are **timestamp + `.` + the exact raw request body**, using the shared ingress secret. Re-encoding JSON after signing invalidates the signature. The timestamp must be within five minutes of the bot clock. Use TLS, synchronize clocks, and keep this endpoint on a private or restricted ingress path. The shared secret must contain at least 32 UTF-8 bytes.

```json
{
  "event_id": "stable-normalized-event-id",
  "provider": "meta",
  "provider_account_id": "verified-business-account",
  "phone_number_id": "verified-number-id",
  "provider_message_id": "provider-message-id",
  "event_type": "message.received",
  "occurred_at": "2026-10-27T12:00:00Z",
  "payload_schema_version": 1,
  "trace_id": "upstream-trace-id",
  "customer_ref": "provider-recipient-reference",
  "text": "START"
}
```

`provider` is `meta` or `evolution`. Identifier fields are bounded strings; text is 1–4096 characters. The service accepts a maximum 1 MiB raw request body, rejects unknown fields, duplicate JSON keys, invalid JSON constants and an unsupported schema version. `occurred_at` must be timezone-aware; it is the authenticated upstream occurrence time, not a customer-supplied authorization claim.

Signing example for an internal integration test:

```python
import hashlib
import hmac
import json
import os
import time

body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
timestamp = str(int(time.time()))
signature = (
    "sha256="
    + hmac.new(
        os.environ["COMMERCE_INGRESS_SECRET"].encode(),
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
)
# Send exactly `body` with the timestamp and signature headers over the configured TLS path.
```

The bot commits the inbox record before returning HTTP 202. A retry with the same tenant/channel/event ID and identical raw body is acknowledged as a duplicate. Reusing the ID with different bytes returns HTTP 409 `event_collision`. The response's `event_id` is the internal inbox UUID, useful for queue administration. Persist and retry the original upstream event identity and body; generate a fresh transport signature/timestamp when retrying later.

Customer streams are processed in durable ingestion order, including before the first Conversation exists. A pending retry blocks later messages in the same customer/channel stream. Other customers can progress. Ordering by provider occurrence time is not inferred across unreceived events; Middleware should preserve its own per-customer sequencing.

## Policy submission

The bot submits to the configured HTTPS origin at:

`POST /platform/v1/whatsapp/messages`

Authorization: `Bearer <COMMERCE_POLICY_BEARER_TOKEN>`. The origin is explicit configuration; event payloads cannot override it. Redirects are not followed. The body contains:

```json
{
  "tenant_id": "business-uuid",
  "requested_by": "commerce-worker",
  "command_id": "stable-outbox-command-uuid",
  "correlation_id": "stable-correlation-id",
  "idempotency_key": "stable-message-idempotency-key",
  "recipient": "provider-recipient-reference",
  "instance_id": "verified-account-id",
  "phone_number_id": "verified-number-id",
  "provider": "meta",
  "message": {"type": "text", "text": "Order received"},
  "consent_verified": true,
  "opted_out": false,
  "campaign_id": "approved-policy-campaign",
  "business_context": {
    "conversation_id": "conversation-uuid",
    "source": "commerce",
    "consent_evidence": "recorded-evidence-reference"
  }
}
```

The policy API must independently authorize the integration and enforce its own rules; these fields are evidence, not permission to bypass policy. Before submission the bot rechecks active business/channel, campaign configuration, consent, bot mode and the conversation version while holding the relevant transaction locks.

### Required acknowledgement

HTTP 200, 201 or 202 with JSON containing both:

```json
{
  "command_id": "same-stable-outbox-command-uuid",
  "operation_id": "durable-policy-operation-id"
}
```

An optional `provider_message_id` can be returned if already known. Acknowledgement means the policy operation was accepted; it does not establish delivery to the customer. The bot uses `sent` internally for this submitted state. Delivery/read/failure are updated by later signed events.

The response is limited to 16 KiB of raw uncompressed bytes. The client uses a bounded total submission budget, including waiting for transport work. Network timeouts, ambiguous acknowledgements, 408/429 and server errors retain the **same** command and idempotency IDs for retry. Other non-success responses are treated as permanent rejections. Response bodies and bearer credentials are not copied into queue errors.

A timeout can occur after a remote service accepts a command. The policy and middleware chain must therefore deduplicate the stable ID durably. This service provides at-least-once submission with stable identifiers; it cannot guarantee exactly-once external delivery without that downstream behavior.

## Asynchronous delivery correlation

Delivery updates use the same authenticated envelope, replacing `customer_ref` and `text` with `status`:

```json
{
  "event_id": "stable-delivery-event-id",
  "provider": "meta",
  "provider_account_id": "verified-business-account",
  "phone_number_id": "verified-number-id",
  "provider_message_id": "eventual-provider-message-id",
  "event_type": "message.delivery.updated",
  "occurred_at": "2026-10-27T12:00:03Z",
  "payload_schema_version": 1,
  "trace_id": "upstream-trace-id",
  "status": "delivered",
  "command_id": "stable-outbox-command-uuid",
  "operation_id": "durable-policy-operation-id"
}
```

`status` is `sent`, `delivered`, `read` or `failed`. `command_id` and `operation_id` are optional signed fields. If the acknowledgement had no provider ID, Middleware must carry at least a valid original command or operation reference so the bot can bind the eventual provider ID to the exact sent message. If both are present they must identify the same command. Matching is restricted to the signed tenant/channel binding and refuses conflicting provider IDs or references. Unmatched or conflicting updates retain a safe retry/dead-letter error instead of silently disappearing. Successful delivery status cannot be regressed by an older weaker status.

## Human handling and consent

An explicit START/INICIAR records opt-in; STOP/PARAR records opt-out. Staff can record consent with an evidence reference. The strongest persisted consent timestamp wins, preventing an older queued START from undoing a newer staff decision.

Human takeover invalidates the conversation version and cancels queued bot replies. While human handling is active, incoming customer messages are retained but the bot does not issue replies. Authorized staff must explicitly resume. Submission holds the conversation lock through the bounded policy call, so takeover waits for a submission already in progress; an external command already accepted cannot be recalled here.

## Integration gates before any live activation

The repository review recorded these external gaps; they must be resolved and retested against exact deployed sibling-service commits:

1. WhatsApp policy command construction must preserve `campaign_id` required by Middleware's `whatsapp.message.send.v1` handler. The reviewed WhatsApp development implementation omitted it from the forwarded payload.
2. Middleware's promoted branch must contain the WhatsApp send handler. It existed on the reviewed main branch but not its development branch. Preserve the normal promotion/authority path.
3. The actual policy response must satisfy the command/operation acknowledgement above, with durable idempotency across timeout/retry/restart.
4. Middleware must authenticate normalized ingress with the exact HMAC contract and provide command/operation correlation for asynchronous delivery updates.
5. Provider credentials and number ownership must be resolved per business. A global provider-number configuration cannot establish tenant isolation by itself. Provider webhooks must fail closed when their required signing configuration is missing.
6. A controlled staging business must complete consent → catalog → cart → quote → confirm → fulfillment → delivery-status correlation, plus timeout replay, duplicate webhook and human takeover cases, through the real policy/middleware/provider path.

`COMMERCE_OUTBOUND_ENABLED` remains false until these gates and the operational gates are evidenced. Do not work around a missing handler, campaign field or acknowledgement with direct sending.
