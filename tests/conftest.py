from __future__ import annotations

from typing import Dict, List

import pytest

from aigw_builder.hub import HubNotFound, PolicySummary
from aigw_builder.resolver import KindDetector, Resolver, TagIndex

TAG_LINES = [
    "abc\trefs/tags/policies/semantic-cache/v1.1.0",
    "abc\trefs/tags/policies/semantic-cache/v1.2.0",
    "abc\trefs/tags/policies/semantic-cache/v1.2.0^{}",
    "abc\trefs/tags/policies/semantic-cache/v1.10.0",
    "abc\trefs/tags/policies/jwt-auth/v1.0.2",
    "abc\trefs/tags/policies/jwt-auth/v1.3.0",
    "abc\trefs/tags/policies/jwt-auth/v1.3.1",
    "abc\trefs/tags/policies/granite-guardian-prompt-injection/v0.9.0",
    "abc\trefs/tags/policies/cors/v1.0.1",
    "abc\trefs/tags/policies/set-headers/v1.0.1",
    "abc\trefs/tags/policies/major-pol/v1.0.0",
    "abc\trefs/tags/policies/major-pol/v1.0.1",
    "abc\trefs/tags/policies/major-pol/v1.1.0",
    "abc\trefs/tags/policies/major-pol/v1.2.0",
    "abc\trefs/tags/policies/major-pol/v2.0.0",
]
PYTHON_POLICIES = {"granite-guardian-prompt-injection"}


class FakeHub:
    def __init__(self) -> None:
        self.data: Dict[str, List[str]] = {
            "semantic-cache": ["1.10", "1.2", "1.1"],
            "jwt-auth": ["1.3", "1.0"],
            "granite-guardian-prompt-injection": ["0.9"],
            "cors": ["1.0"],
            "major-pol": ["2.0", "1.1", "1.0"],  # v1.2.0 is tagged but not published on the hub
        }

    def versions(self, name: str) -> List[PolicySummary]:
        if name not in self.data:
            raise HubNotFound(name)
        return [PolicySummary(name=name, version=v, is_latest=i == 0) for i, v in enumerate(self.data[name])]

    def list_policies(self, categories=None) -> List[PolicySummary]:
        return [self.versions(n)[0] for n in self.data]


@pytest.fixture
def resolver() -> Resolver:
    kinds = KindDetector(probe=lambda name, tag: name not in PYTHON_POLICIES)
    return Resolver(FakeHub(), tags=TagIndex(TAG_LINES), kinds=kinds)
