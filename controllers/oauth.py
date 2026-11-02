
import secrets
import logging
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone

import requests

from odoo import http
from odoo.http import request
from odoo.exceptions import AccessDenied

_logger = logging.getLogger(__name__)

TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"


class QuickBooksOAuthController(http.Controller):

    @http.route(
        "/quickbooks/oauth/start",
        type="http",
        auth="user",
        methods=["GET"],
        csrf=False,
    )
    def oauth_start(self, **kw):
        if not request.env.user.has_group("base.group_system"):
            raise AccessDenied()

        params_store = request.env["ir.config_parameter"].sudo()

        client_id = params_store.get_param(
            "quickbooks_connector.client_id"
        )

        if not client_id:
            return request.make_response(
                "Set the QuickBooks Client ID in Odoo Settings first.",
                status=400,
            )

        base_url = params_store.get_param(
            "web.base.url", ""
        ).rstrip("/")

        redirect_uri = (
            base_url + "/quickbooks/oauth/callback"
        )

        state = secrets.token_urlsafe(32)
        params_store.set_param(
            "quickbooks_connector.oauth_state", state
        )

        query = urlencode({
            "client_id": client_id,
            "response_type": "code",
            "scope": "com.intuit.quickbooks.accounting",
            "redirect_uri": redirect_uri,
            "state": state,
        })

        authorization_url = AUTHORIZE_URL + "?" + query

        # Explicitly allow the redirect to Intuit's external domain.
        return request.redirect(
            authorization_url,
            local=False,
        )

    @http.route(
        "/quickbooks/oauth/callback",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
        save_session=True,
    )
    def oauth_callback(
        self, code=None, state=None, realmId=None, error=None, **kw
    ):
        params_store = request.env["ir.config_parameter"].sudo()

        if error:
            _logger.warning(
                "QuickBooks OAuth authorization returned an error."
            )
            return request.make_response(
                "QuickBooks authorization failed. Please reconnect "
                "and check the Intuit app configuration.",
                status=400,
            )

        expected_state = params_store.get_param(
            "quickbooks_connector.oauth_state"
        )

        if (
            not state
            or not expected_state
            or not secrets.compare_digest(state, expected_state)
        ):
            return request.make_response(
                "Invalid OAuth state. Return to Odoo and reconnect.",
                status=400,
            )

        if not code or not realmId:
            return request.make_response(
                "Missing authorization code or QuickBooks company ID.",
                status=400,
            )

        client_id = params_store.get_param(
            "quickbooks_connector.client_id"
        )
        client_secret = params_store.get_param(
            "quickbooks_connector.client_secret"
        )

        if not client_id or not client_secret:
            return request.make_response(
                "QuickBooks Client ID or Client Secret is missing "
                "in Odoo Settings.",
                status=400,
            )

        base_url = params_store.get_param(
            "web.base.url", ""
        ).rstrip("/")

        redirect_uri = (
            base_url + "/quickbooks/oauth/callback"
        )

        try:
            response = requests.post(
                TOKEN_URL,
                auth=(client_id, client_secret),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                timeout=30,
            )
        except requests.RequestException:
            _logger.exception(
                "QuickBooks OAuth token request failed."
            )
            return request.make_response(
                "Could not contact QuickBooks to exchange the "
                "authorization code. Check server connectivity.",
                status=502,
            )

        if not response.ok:
            _logger.error(
                "QuickBooks token exchange failed with HTTP %s.",
                response.status_code,
            )
            return request.make_response(
                "QuickBooks token exchange failed (HTTP %s). "
                "Check the Intuit credentials and exact redirect URI."
                % response.status_code,
                status=400,
            )

        try:
            token_data = response.json()
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_in = int(token_data.get("expires_in", 3600))

            if not access_token or not refresh_token:
                raise ValueError("Required token fields are missing")

        except (ValueError, TypeError):
            _logger.exception(
                "QuickBooks returned an invalid token response."
            )
            return request.make_response(
                "QuickBooks returned an invalid token response.",
                status=502,
            )

        params_store.set_param(
            "quickbooks_connector.realm_id", realmId
        )
        params_store.set_param(
            "quickbooks_connector.access_token", access_token
        )
        params_store.set_param(
            "quickbooks_connector.refresh_token", refresh_token
        )
        params_store.set_param(
            "quickbooks_connector.token_expiry",
            (
                datetime.now(timezone.utc)
                + timedelta(seconds=expires_in)
            ).isoformat(),
        )
        params_store.set_param(
            "quickbooks_connector.oauth_state", ""
        )

        return request.redirect("/web")
