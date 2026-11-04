"""Thin client for Odoo 19's JSON-2 API: POST /json/2/<model>/<method> with bearer key."""
from typing import Any

import httpx

from steelquote.config import Settings, get_settings


class OdooError(RuntimeError):
    pass


class Odoo:
    def __init__(self, settings: Settings | None = None):
        s = settings or get_settings()
        self._http = httpx.Client(
            base_url=f"{s.odoo_url}/json/2",
            headers={"Authorization": f"bearer {s.odoo_api_key}", "X-Odoo-Database": s.odoo_db},
            timeout=60,
        )

    def call(self, model: str, method: str, **kwargs: Any) -> Any:
        r = self._http.post(f"/{model}/{method}", json=kwargs)
        if r.is_error:
            raise OdooError(f"{model}.{method} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def search_read(self, model: str, domain: list | None = None, fields: list[str] | None = None,
                    limit: int | None = None, order: str | None = None) -> list[dict]:
        kw: dict[str, Any] = {"domain": domain or [], "fields": fields or []}
        if limit:
            kw["limit"] = limit
        if order:
            kw["order"] = order
        return self.call(model, "search_read", **kw)

    def create(self, model: str, vals_list: list[dict]) -> list[int]:
        return self.call(model, "create", vals_list=vals_list)

    def write(self, model: str, ids: list[int], vals: dict) -> bool:
        return self.call(model, "write", ids=ids, vals=vals)

    def message_post(self, model: str, record_id: int, body: str) -> Any:
        """body is HTML. Over JSON-2 a plain str is escaped, and Markup is not sendable, so flag it explicitly."""
        return self.call(model, "message_post", ids=[record_id], body=body, body_is_html=True)
