"""Controlled authentication handlers for connector profiles.

Profiles choose a handler by name. No handler evaluates profile text as code.
The FiberHome handler delegates encryption to the existing, already-audited
``gateway.fhencrypt`` implementation instead of duplicating cryptography here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from requests.auth import HTTPBasicAuth, HTTPDigestAuth

from gateway import fhencrypt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from connector_executor import ConnectorExecutor


class AuthenticationError(Exception):
    """Raised when a configured authentication flow cannot succeed."""


def _safe_field(field: str) -> str:
    return field.replace("\n", " ").replace("\r", " ")


class BaseAuthHandler:
    handler_type = "none"

    def __init__(self, executor: "ConnectorExecutor", flow: Dict[str, Any]):
        self.executor = executor
        self.flow = flow

    def authenticate(self) -> None:
        return None

    def success_payload(self) -> Optional[Dict[str, Any]]:
        endpoint_id = self.executor.flow_endpoint("login")
        if endpoint_id is None:
            return None
        result = self.executor.endpoint_results.get(endpoint_id)
        return result.payload if result else None

    def _apply_session(self) -> None:
        """Store session metadata declared by the profile, never secret values."""

        session = self.flow.get("session") or {}
        self.executor.session_spec = dict(session)
        if not session.get("store"):
            return
        field = session.get("id_field")
        if not field:
            return
        payload = self.success_payload() or {}
        value = self.executor.dig(payload, field)
        if value is not None:
            self.executor.state[field] = value

    def check_success(self) -> None:
        success = self.flow.get("success") or {}
        field = success.get("field")
        if not field:
            return
        payload = self.success_payload() or {}
        value = self.executor.dig(payload, field)
        if value is None:
            raise AuthenticationError(
                f"Authentication response did not contain {_safe_field(str(field))}."
            )
        if "allowed" in success:
            allowed = {str(item) for item in success["allowed"]}
            if str(value) not in allowed:
                raise AuthenticationError("Gateway rejected the supplied credentials.")
        if "not_equal" in success and str(value) == str(success["not_equal"]):
            raise AuthenticationError("Gateway rejected the supplied credentials.")


class NoneAuthHandler(BaseAuthHandler):
    handler_type = "none"

    def authenticate(self) -> None:
        endpoint_id = self.executor.flow_endpoint("login")
        if endpoint_id:
            self.executor.execute_endpoint(endpoint_id)
        self.check_success()


class BasicAuthHandler(BaseAuthHandler):
    handler_type = "http_basic"

    def authenticate(self) -> None:
        self.executor.http.auth = HTTPBasicAuth(
            self.executor.username, self.executor.password
        )
        self.check_success()


class DigestAuthHandler(BaseAuthHandler):
    handler_type = "http_digest"

    def authenticate(self) -> None:
        self.executor.http.auth = HTTPDigestAuth(
            self.executor.username, self.executor.password
        )
        self.check_success()


class FormAuthHandler(BaseAuthHandler):
    handler_type = "form"

    def authenticate(self) -> None:
        endpoint_id = self.executor.flow_endpoint("login")
        if endpoint_id is None:
            raise AuthenticationError("Authentication flow has no login endpoint.")
        self.executor.execute_endpoint(endpoint_id)
        self.check_success()
        self._apply_session()


class SessionAuthHandler(FormAuthHandler):
    handler_type = "session"


class TokenAuthHandler(FormAuthHandler):
    handler_type = "token"

    def authenticate(self) -> None:
        super().authenticate()
        field = self.executor.session_spec.get("id_field")
        if not field:
            return
        result = self.executor.endpoint_results.get(
            self.executor.flow_endpoint("login") or ""
        )
        token = self.executor.dig(result.payload if result else {}, field)
        if token is None:
            raise AuthenticationError("Authentication response did not contain a token.")
        self.executor.http.headers["Authorization"] = f"Bearer {token}"


class FiberHomeAesAuthHandler(BaseAuthHandler):
    """Seed/login flow using the legacy FiberHome AES routine by delegation."""

    handler_type = "fiberhome_aes"

    def authenticate(self) -> None:
        seed_id = self.executor.flow_endpoint("seed")
        login_id = self.executor.flow_endpoint("login")
        if not seed_id or not login_id:
            raise AuthenticationError(
                "FiberHome authentication profile is missing seed or login endpoint."
            )

        seed = self.executor.execute_endpoint(seed_id).payload or {}
        challenge = seed.get("acsRandom")
        session_id = seed.get("sessionid")
        if not challenge or not session_id:
            raise AuthenticationError(
                "FiberHome challenge response was missing random or session data."
            )

        # The only cryptographic operation is delegated to the existing helper.
        encrypted = fhencrypt(self.executor.password, str(challenge))
        self.executor.derived["encrypted_password"] = encrypted
        self.executor.state["sessionid"] = session_id

        self.executor.execute_endpoint(login_id)
        self.check_success()
        self._apply_session()


class HuaweiNonceFormAuthHandler(BaseAuthHandler):
    """Huawei ASP login: fetch token, POST base64 password, expect index redirect."""

    handler_type = "huawei_nonce_form"

    def authenticate(self) -> None:
        import base64

        token_id = self.executor.flow_endpoint("randcount") or self.executor.flow_endpoint("token")
        login_id = self.executor.flow_endpoint("login")
        if not token_id or not login_id:
            raise AuthenticationError(
                "Huawei authentication profile is missing randcount or login endpoint."
            )

        token_result = self.executor.execute_endpoint(token_id)
        token = (token_result.text or "").strip().lstrip("\ufeff")
        if not token:
            raise AuthenticationError("Huawei token response was empty.")

        self.executor.derived["hw_token"] = token
        self.executor.derived["encoded_password"] = base64.b64encode(
            str(self.executor.password).encode("utf-8")
        ).decode("ascii")

        login_result = self.executor.execute_endpoint(login_id)
        body = login_result.text or ""
        if "index.asp" not in body:
            raise AuthenticationError("Gateway rejected the supplied credentials.")
        self._apply_session()


HANDLERS = {
    handler.handler_type: handler
    for handler in (
        NoneAuthHandler,
        BasicAuthHandler,
        DigestAuthHandler,
        FormAuthHandler,
        SessionAuthHandler,
        TokenAuthHandler,
        FiberHomeAesAuthHandler,
        HuaweiNonceFormAuthHandler,
    )
}


def auth_handler(executor: "ConnectorExecutor", flow: Dict[str, Any]) -> BaseAuthHandler:
    handler_type = flow.get("type", "none")
    handler = HANDLERS.get(handler_type)
    if handler is None:
        raise AuthenticationError(f"Unsupported authentication handler {handler_type!r}.")
    return handler(executor, flow)
