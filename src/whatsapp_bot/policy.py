"""Only outbound boundary: the policy API, never a WhatsApp provider."""

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from .http_runtime import run_http


class PolicyError(Exception):
    def __init__(self, code: str, *, transient: bool):
        super().__init__(code)
        self.code = code
        self.transient = transient


def validate_acknowledgement(result: dict, command_id: str) -> dict:
    if (
        not isinstance(result, dict)
        or str(result.get("command_id", "")) != str(command_id)
        or not isinstance(result.get("operation_id"), str)
        or not 1 <= len(result["operation_id"]) <= 200
    ):
        # Request may have succeeded: retry ONLY with the original command/idempotency IDs.
        raise PolicyError("policy_ack_invalid", transient=True)
    return result


class PolicyClient:
    def __init__(self, base_url: str, bearer_token: str, timeout=5, *, allow_local_http=False):
        parsed = urlsplit(base_url)
        local = allow_local_http and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme != "https" and not (local and parsed.scheme == "http")) or not parsed.hostname:
            raise ValueError("Policy endpoint requires HTTPS")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("Policy endpoint must be an origin")
        if not bearer_token.strip() or not 0 < timeout <= 15:
            raise ValueError("Policy credentials and bounded timeout required")
        self._url = base_url.rstrip("/") + "/platform/v1/whatsapp/messages"
        self._token = bearer_token
        self._timeout = timeout
        self._transport = None

    def close(self):
        # Each submission owns and closes its asynchronous transport before returning.
        pass

    def send(self, payload: dict) -> dict:
        try:
            return run_http(lambda: self._send_bounded(payload), timeout=self._timeout)
        except TimeoutError:
            raise PolicyError("policy_deadline", transient=True) from None

    async def _send_bounded(self, payload):
        try:
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=False, transport=self._transport
                ) as client:
                    async with client.stream(
                        "POST",
                        self._url,
                        json=payload,
                        headers={"Authorization": "Bearer " + self._token, "Accept-Encoding": "identity"},
                    ) as response:
                        if response.status_code not in {200, 201, 202}:
                            raise PolicyError(
                                "policy_unavailable" if response.status_code >= 500 else "policy_rejected",
                                transient=response.status_code >= 500 or response.status_code in {408, 429},
                            )
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise PolicyError("policy_ack_invalid", transient=True)
                        body = bytearray()
                        # Raw streaming avoids allocating/decompressing an unbounded response body.
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > 16384:
                                raise PolicyError("policy_response_too_large", transient=True)
                            body.extend(chunk)
                        try:
                            result = json.loads(body)
                        except (ValueError, UnicodeDecodeError):
                            raise PolicyError("policy_ack_invalid", transient=True) from None
                        return validate_acknowledgement(result, payload.get("command_id", ""))
        except TimeoutError:
            raise PolicyError("policy_deadline", transient=True) from None
        except httpx.RequestError:
            raise PolicyError("policy_unavailable", transient=True) from None
