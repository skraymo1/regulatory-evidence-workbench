from __future__ import annotations

import base64
import binascii
import json
import logging


def authorize_principal(headers, allowed_oid: str, tenant_id: str) -> bool:
    """Trust only EasyAuth headers: deployed ingress MUST enforce platform authentication."""
    if not allowed_oid or not tenant_id:
        return False
    headers = {key.lower(): value for key, value in headers.items()}
    encoded = headers.get("x-ms-client-principal", "")
    try:
        principal = json.loads(base64.b64decode(encoded, validate=True))
        claims = {item["typ"]: item["val"] for item in principal["claims"]}
        oid = claims.get("oid") or claims.get(
            "http://schemas.microsoft.com/identity/claims/objectidentifier"
        )
        tid = claims.get("tid") or claims.get(
            "http://schemas.microsoft.com/identity/claims/tenantid"
        )
        # auth_typ can name a federation mechanism rather than the identity provider.
        provider = headers.get("x-ms-client-principal-idp", principal.get("auth_typ"))
        permitted = provider == "aad" and oid == allowed_oid and tid == tenant_id
        if not permitted:
            logging.getLogger(__name__).warning(
                "Authorization denied: provider_match=%s oid_match=%s tenant_match=%s",
                provider == "aad", oid == allowed_oid, tid == tenant_id,
            )
        return permitted
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        logging.getLogger(__name__).warning("Principal schema failure: %s", type(exc).__name__)
        return False
