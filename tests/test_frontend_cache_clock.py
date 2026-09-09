from __future__ import annotations

import pytest

from tests.test_frontend_api_format_workbench import _run_node_script


PRELUDE = r'''
const api = await import("./static/js/api.js");
let wall = 1_800_000_000_000, mono = 0, requests = 0;
Date.now = () => wall;
Object.defineProperty(globalThis, "performance", { value: { now: () => mono }, configurable: true });
const json = value => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
const get = options => api.fetchCachedJson("/clock", options);
function assert(value, message) { if (!value) throw new Error(message); }
'''


@pytest.mark.parametrize("wall_shift", [-3_600_000, 3_600_000])
def test_watchlist_refreshes_after_elapsed_ttl_despite_wall_clock_shift(wall_shift: int) -> None:
    _run_node_script(PRELUDE + f"const shift = {wall_shift};" + r'''
        const { installAppDom } = await import("./tests/frontend_app_flow_helpers.mjs");
        const { loadWatchlist } = await import("./static/js/watchlist.js");
        const dom = installAppDom();
        let status = "watching";
        globalThis.fetch = async () => {
          requests += 1;
          return json([{symbol:"600519.SH", code:"600519", market:"SH", name:"合成股票", research_status:status, priority:"medium"}]);
        };
        const state = { watchlist: [] };
        await loadWatchlist(state);
        status = "excluded";
        mono = 16_000; wall += shift;
        await loadWatchlist(state);
        assert(requests === 2 && state.watchlist[0].research_status === "excluded", "expired visible watchlist was treated as fresh");
        assert(dom.element("watchList").innerHTML.includes("已排除"), "table did not render updated server state");
    ''')


@pytest.mark.parametrize("wall_shift", [-3_600_000, 3_600_000])
def test_wall_clock_shift_cannot_expire_an_unelapsed_cache(wall_shift: int) -> None:
    _run_node_script(PRELUDE + f"const shift = {wall_shift};" + r'''
        globalThis.fetch = async () => json({ revision: ++requests });
        const initial = await get();
        const timestamp = api.getCachedJsonSnapshot("/clock").updatedAt;
        mono = 14_999; wall += shift;
        assert(await get() === initial && requests === 1, "wall clock changed elapsed TTL");
        assert(api.getCachedJsonSnapshot("/clock").updatedAt === timestamp && timestamp === 1_800_000_000_000, "public audit timestamp became monotonic time");
        mono = 15_000;
        assert((await get()).revision === 2 && requests === 2, "exact TTL boundary was not expired");
        assert(api.getCachedJsonSnapshot("/clock").updatedAt === wall, "completed refresh omitted wall-clock timestamp");
    ''')


def test_zero_monotonic_timestamp_is_valid_and_invalidation_cannot_be_a_fresh_zero() -> None:
    _run_node_script(PRELUDE + r'''
        globalThis.fetch = async () => json({ revision: ++requests });
        const initial = await get();
        assert(await get() === initial && requests === 1, "timestamp zero was mistaken for missing state");
        assert(api.invalidateCachedJson("/clock"), "known cache did not invalidate");
        assert(api.getCachedJsonSnapshot("/clock").updatedAt === 0, "existing invalidation snapshot contract changed");
        assert((await get()).revision === 2, "invalidated cache was considered fresh at monotonic zero");
    ''')


def test_cache_ttl_starts_at_successful_completion_and_waiters_share_one_request() -> None:
    _run_node_script(PRELUDE + r'''
        let resolve;
        globalThis.fetch = () => { requests += 1; return new Promise(done => { resolve = done; }); };
        const first = get(), shared = get();
        assert(requests === 1, "same-key pending requests were duplicated");
        mono = 20_000;
        resolve(json({ revision: 1 }));
        assert(await first === await shared, "shared completion lost identity");
        mono = 34_999;
        assert((await get()).revision === 1 && requests === 1, "TTL started at request creation instead of completion");
        globalThis.fetch = async () => json({ revision: ++requests });
        mono = 35_000;
        assert((await get()).revision === 2, "completion-based TTL never expired");
    ''')


def test_force_and_stale_generation_do_not_revalidate_invalidated_data() -> None:
    _run_node_script(PRELUDE + r'''
        globalThis.fetch = async () => json({ revision: ++requests });
        await get();
        assert((await get({ force: true })).revision === 2, "force did not bypass fresh cache");
        let resolve;
        globalThis.fetch = () => { requests += 1; return new Promise(done => { resolve = done; }); };
        const pending = get({ force: true });
        api.invalidateCachedJson("/clock", { abortInflight: false });
        resolve(json({ revision: 3 }));
        assert((await pending).revision === 3, "waiter completion changed");
        assert(api.getCachedJsonSnapshot("/clock").value.revision === 2, "old generation cached its result");
        globalThis.fetch = async () => json({ revision: ++requests });
        assert((await get()).revision === 4, "old generation reset freshness after invalidation");
    ''')


def test_expired_refresh_failure_preserves_snapshot_without_extending_freshness() -> None:
    _run_node_script(PRELUDE + r'''
        globalThis.fetch = async () => json({ revision: ++requests });
        const initial = await get();
        mono = 15_000;
        globalThis.fetch = async () => { requests += 1; return new Response("unavailable", { status: 503 }); };
        let failures = 0;
        for (let count = 0; count < 2; count += 1) {
          try { await get(); } catch { failures += 1; }
        }
        const cached = api.getCachedJsonSnapshot("/clock");
        assert(failures === 2 && requests === 3, "failed refresh extended old cache TTL");
        assert(cached.found && cached.value === initial && cached.updatedAt === wall, "failed refresh destroyed last successful snapshot");
    ''')


def test_cancelled_waiter_does_not_cancel_another_waiter_or_poison_freshness() -> None:
    _run_node_script(PRELUDE + r'''
        const controller = new AbortController();
        let resolve;
        globalThis.fetch = () => { requests += 1; return new Promise(done => { resolve = done; }); };
        const cancelled = get({ signal: controller.signal }), shared = get();
        controller.abort();
        let aborted = false;
        try { await cancelled; } catch (error) { aborted = error.name === "AbortError"; }
        mono = 50;
        resolve(json({ revision: 1 }));
        await shared;
        assert(aborted && requests === 1, "waiter cancellation changed shared request ownership");
        mono = 15_049;
        assert((await get()).revision === 1 && requests === 1, "cancelled waiter changed completion freshness");
    ''')
