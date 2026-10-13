"""Airdrop discovery sources.

CoinGecko's airdrop index was removed from the default source list. It answers
403 to any non-browser client, so every cycle spent a request on a source that
could only fail and pushed a permanent, meaningless entry into the cycle's
failure report.

These lock in both halves of that fix:

* the broken source is genuinely never requested, not merely silenced;
* every other source still runs, and a source that fails for a real reason is
  still reported rather than swallowed. Removing a dead source must not have
  removed the reporting that made it dead in the first place.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List

import pytest

from backend.app.airdrops import discovery
from backend.app.airdrops.discovery import (
    DEFAULT_COMMUNITY_SOURCES,
    DEFAULT_OFFICIAL_SOURCES,
    run_discovery_cycle,
)
from backend.app.tools.base import ToolResult

BROKEN_URL = "coingecko.com/en/airdrops"
KEPT_URLS = ["https://airdrops.io", "https://airdropalert.com"]


class _StubSiteSearch:
    """Stands in for the site_search tool and records what it was asked to fetch."""

    def __init__(self, *, failing: List[str] | None = None, matches: List[str] | None = None):
        self.requested: List[str] = []
        self._failing = failing or []
        self._matches = matches if matches is not None else ["Airdrop claim snapshot rewards"]

    async def execute(self, arguments: Dict[str, Any]) -> ToolResult:
        url = arguments["url"]
        self.requested.append(url)
        if url in self._failing:
            return ToolResult.failure(
                "403 Forbidden", category="HTTP_ERROR", retryable=False
            )
        return ToolResult.success({"matches": self._matches})


@pytest.fixture
def stub_search(monkeypatch):
    def install(*, failing=None, matches=None) -> _StubSiteSearch:
        tool = _StubSiteSearch(failing=failing, matches=matches)
        monkeypatch.setattr(discovery.registry, "get", lambda name: tool if name == "site_search" else None)
        return tool

    return install


class TestBrokenSourceIsGone:
    def test_coingecko_is_not_a_default_source(self):
        assert not any(BROKEN_URL in url for url in DEFAULT_COMMUNITY_SOURCES)

    def test_no_default_source_hosts_coingecko(self):
        assert all("coingecko" not in url.lower() for url in DEFAULT_COMMUNITY_SOURCES)
        assert all("coingecko" not in url.lower() for url in DEFAULT_OFFICIAL_SOURCES)

    def test_the_module_no_longer_names_the_broken_url(self):
        """A grep guard, so it cannot be quietly re-added as a literal."""
        source = inspect.getsource(discovery)
        assert BROKEN_URL not in source

    @pytest.mark.asyncio
    async def test_a_default_cycle_never_requests_the_broken_url(self, stub_search):
        """The behaviour the fix is actually for: no request is made."""
        tool = stub_search()

        await run_discovery_cycle()

        assert not any(BROKEN_URL in url for url in tool.requested)
        assert all("coingecko" not in url for url in tool.requested)

    @pytest.mark.asyncio
    async def test_a_default_cycle_reports_no_coingecko_failure(self, stub_search):
        stub_search()

        result = await run_discovery_cycle()

        failed = [f["url"] for f in result["source_failures"] if f.get("url")]
        assert not any(BROKEN_URL in url for url in failed)


class TestOtherSourcesStillWork:
    def test_the_remaining_sources_are_kept(self):
        for url in KEPT_URLS:
            assert url in DEFAULT_COMMUNITY_SOURCES, f"{url} must be preserved"

    def test_nothing_else_was_dropped(self):
        # Exactly the two known-good community sources remain.
        assert DEFAULT_COMMUNITY_SOURCES == KEPT_URLS

    @pytest.mark.asyncio
    async def test_the_remaining_sources_are_still_queried(self, stub_search):
        tool = stub_search()

        await run_discovery_cycle()

        for url in KEPT_URLS:
            assert url in tool.requested, f"{url} is no longer being fetched"

    @pytest.mark.asyncio
    async def test_campaigns_are_still_discovered_from_the_remaining_sources(self, stub_search):
        stub_search(matches=["Airdrop claim snapshot rewards available now"])

        result = await run_discovery_cycle()

        assert result["campaigns_discovered"] > 0
        assert result["results_by_source"]["community"]["count"] > 0
        assert result["results_by_source"]["community"]["sources_checked"] == len(KEPT_URLS)

    @pytest.mark.asyncio
    async def test_a_caller_supplied_source_list_is_still_honoured(self, stub_search):
        """Explicit configuration is not overridden by the defaults."""
        tool = stub_search()

        await run_discovery_cycle({"official": [], "community": ["https://example.test/a"]})

        assert tool.requested == ["https://example.test/a"]

    @pytest.mark.asyncio
    async def test_the_default_list_is_never_handed_out_by_reference(self, stub_search):
        """run_discovery_cycle copies the defaults, so it cannot mutate them.

        `results_by_source["community"]["sources_checked"]` reflects the list the
        cycle actually walked. The cycle itself builds its own dict, so this
        asserts the copy directly: the module default must still be the same
        object contents after any number of cycles.
        """
        stub_search()
        before = list(DEFAULT_COMMUNITY_SOURCES)

        for _ in range(3):
            await run_discovery_cycle()

        assert DEFAULT_COMMUNITY_SOURCES == before


class TestFallbackBehaviourIsPreserved:
    @pytest.mark.asyncio
    async def test_one_failing_source_does_not_stop_the_others(self, stub_search):
        tool = stub_search(failing=["https://airdrops.io"])

        result = await run_discovery_cycle()

        assert "https://airdropalert.com" in tool.requested
        assert result["results_by_source"]["community"]["count"] > 0

    @pytest.mark.asyncio
    async def test_a_genuinely_failing_source_is_still_reported(self, stub_search):
        """The cause was fixed by dropping a dead source, not by muting the report."""
        stub_search(failing=["https://airdrops.io"])

        result = await run_discovery_cycle()

        assert result["sources_failed"] == 1
        failure = result["source_failures"][0]
        assert failure["url"] == "https://airdrops.io"
        assert failure["error"] == "403 Forbidden"
        assert failure["retryable"] is False
        assert result["status"] == "completed_with_source_failures"

    @pytest.mark.asyncio
    async def test_a_clean_cycle_is_reported_as_complete(self, stub_search):
        stub_search()

        result = await run_discovery_cycle()

        assert result["sources_failed"] == 0
        assert result["source_failures"] == []
        assert result["status"] == "completed"