"""Client for the public WSO2 Policy Hub REST API."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import httpx
import yaml

# Same default (and override env var) as the `ap` CLI: cli/src/utils/constants.go in wso2/api-platform.
DEFAULT_HUB_URL = (
    "https://db720294-98fd-40f4-85a1-cc6a3b65bc9a-dev.e1-us-east-azure.choreoapis.dev"
    "/api-platform/policy-hub-api/policy-hub-public/v1.0"
)
HUB_URL_ENV = "WSO2AP_POLICYHUB_BASE_URL"


class HubError(Exception):
    pass


class HubNotFound(HubError):
    pass


@dataclass
class PolicySummary:
    name: str
    version: str
    display_name: str = ""
    provider: str = ""
    categories: List[str] = field(default_factory=list)
    description: str = ""
    is_latest: bool = True

    @classmethod
    def from_api(cls, raw: Dict[str, Any]) -> "PolicySummary":
        return cls(
            name=str(raw.get("name", "")),
            version=str(raw.get("version", "")),
            display_name=str(raw.get("displayName") or raw.get("name") or ""),
            provider=str(raw.get("provider") or ""),
            categories=[str(c) for c in raw.get("categories") or []],
            description=str(raw.get("description") or "").strip(),
            is_latest=raw.get("isLatest") is not False,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "displayName": self.display_name,
            "provider": self.provider,
            "categories": self.categories,
            "isLatest": self.is_latest,
            "description": self.description,
        }


def resolve_hub_url(explicit: Optional[str] = None) -> str:
    return (explicit or os.environ.get(HUB_URL_ENV) or DEFAULT_HUB_URL).rstrip("/")


class PolicyHubClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base_url = resolve_hub_url(base_url)
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)
        self._versions_cache: Dict[str, List[PolicySummary]] = {}
        self._all_cache: Optional[List[PolicySummary]] = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PolicyHubClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, accept: str = "application/json") -> httpx.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self._http.get(url, params=params, headers={"Accept": accept})
        except httpx.HTTPError as exc:
            raise HubError(f"Policy Hub is unreachable ({self.base_url}): {exc}") from exc
        if resp.status_code == 404:
            raise HubNotFound(f"Not found on Policy Hub: {path}")
        if resp.status_code >= 400:
            raise HubError(f"Policy Hub request {path} failed with HTTP {resp.status_code}")
        return resp

    def categories(self) -> List[str]:
        data = self._get("/policies/categories").json()
        return [str(c) for c in data.get("data") or []]

    def list_policies(self, categories: Optional[Iterable[str]] = None, page_size: int = 100) -> List[PolicySummary]:
        """All latest policies, optionally filtered (OR) by category. Pages through the full result."""
        cats = [c for c in (categories or []) if c]
        if not cats and self._all_cache is not None:
            return list(self._all_cache)
        out: List[PolicySummary] = []
        offset = 0
        while True:
            params: Dict[str, Any] = {"offset": offset, "limit": page_size}
            if cats:
                params["categories"] = ",".join(cats)
            data = self._get("/policies", params).json()
            page = [PolicySummary.from_api(p) for p in data.get("data") or []]
            out.extend(page)
            total = (data.get("pagination") or {}).get("total", data.get("count", len(out)))
            offset += page_size
            if not page or len(out) >= int(total):
                break
        if not cats:
            self._all_cache = list(out)
        return out

    def versions(self, name: str) -> List[PolicySummary]:
        if name not in self._versions_cache:
            data = self._get(f"/policies/{name}/versions").json()
            items = data if isinstance(data, list) else data.get("data") or []
            if not items:  # the hub answers 200 with an empty list for unknown policies
                raise HubNotFound(f"Policy {name!r} not found on Policy Hub")
            self._versions_cache[name] = [PolicySummary.from_api(p) for p in items]
        return self._versions_cache[name]

    def definition(self, name: str, version: str) -> Dict[str, Any]:
        resp = self._get(f"/policies/{name}/versions/{version}/definition", accept="text/yaml, application/json")
        try:
            doc = yaml.safe_load(resp.text)
        except yaml.YAMLError as exc:
            raise HubError(f"Could not parse definition of {name} {version}: {exc}") from exc
        return doc if isinstance(doc, dict) else {}
