"""
Pins for the Dante DDM plugin: what it does today, in the order it does it.

These hold behaviour the feature tests in test_dante_plugin.py leave open: the
order of every side effect an operation causes (the state writes, the events,
the log lines, the GraphQL requests and their bodies, the project save, the
tasks started), the connection's bring-up and its clean-up on each failure,
the reconnect back-off, the poll's tolerance of failures, the action names and
the two forms a route request comes in, the preset store's refusals and its
recall plan, and reading a routing-matrix row or column back to a channel.

Some of them pin something that only looks deliberate, such as a poll that
gives up handing the reconnect the project's config as saved rather than the
settings read out of their groups. They record the behaviour; they do not
endorse it.

Run from the openavc-plugins root: pytest tests/test_dante_pins.py -v
"""

import copy
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

_PLUGINS_ROOT = Path(__file__).resolve().parents[1]
if str(_PLUGINS_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_ROOT))

from integrations.dante import dante_plugin  # noqa: E402
from integrations.dante.dante_plugin import DanteDDMPlugin  # noqa: E402

pytestmark = pytest.mark.asyncio


# ──── A recorder of every side effect, in order ────

_OPEN_TASKS = []


class _Task:
    """What the fake create_task hands back: the coroutine, not yet run."""

    def __init__(self, coro):
        self.coro = coro
        self.finished = False
        _OPEN_TASKS.append(self)

    def done(self):
        return self.finished

    async def run(self):
        try:
            return await self.coro
        finally:
            self.finished = True


@pytest.fixture(autouse=True)
def _close_unrun_tasks():
    yield
    while _OPEN_TASKS:
        _OPEN_TASKS.pop().coro.close()


class _Api:
    """The plugin API, recording each call into one ordered list."""

    def __init__(self, config=None):
        self._config = dict(config or {})
        self.calls = []
        self.tasks = []

    @property
    def config(self):
        return dict(self._config)

    async def state_set(self, key, value):
        self.calls.append(("state", key, value))

    async def event_emit(self, name, payload=None):
        self.calls.append(("event", name, copy.deepcopy(payload)))

    async def event_subscribe(self, pattern, handler):
        self.calls.append(("subscribe", pattern, handler.__name__))
        return "sub-1"

    def log(self, message, level="info"):
        self.calls.append(("log", level, message))

    async def save_config(self, config):
        self.calls.append(("save_config", copy.deepcopy(config)))
        self._config = dict(config)

    def create_task(self, coro, name=None):
        self.calls.append(("task", name))
        task = _Task(coro)
        self.tasks.append(task)
        return task


class _Status(Exception):
    """An HTTP status error raise_for_status throws."""


class _Answer:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        if isinstance(self.body, _Status):
            raise self.body

    def json(self):
        return copy.deepcopy(self.body)


class _Client:
    """httpx.AsyncClient's two calls, answering from a script.

    Each answer is a JSON body, an exception post raises, or a _Status that
    raise_for_status raises. The last answer repeats."""

    def __init__(self, calls, answers, close_raises=None):
        self.calls = calls
        self.answers = list(answers)
        self.close_raises = close_raises

    async def post(self, path, json=None):
        self.calls.append(("post", path, copy.deepcopy(json)))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException) and not isinstance(answer, _Status):
            raise answer
        return _Answer(answer)

    async def aclose(self):
        self.calls.append(("aclose",))
        if self.close_raises:
            raise self.close_raises


def _plugin(config=None, answers=None):
    plugin = DanteDDMPlugin()
    api = _Api(config)
    plugin.api = api
    if answers is not None:
        plugin._client = _Client(api.calls, answers)
    return plugin, api


def _httpx(api, answers, close_raises=None):
    """A stand-in httpx module whose AsyncClient records how it was built."""
    module = types.ModuleType("httpx")

    def async_client(**kwargs):
        api.calls.append(("client", kwargs))
        return _Client(api.calls, answers, close_raises)

    module.AsyncClient = async_client
    return module


class _Sleeper:
    """asyncio as the plugin sees it: sleep records its delay, and can stop
    the plugin's loop after a number of sleeps or run a step on each."""

    def __init__(self, plugin, calls, stop_after=None, on_sleep=None):
        self.plugin = plugin
        self.calls = calls
        self.stop_after = stop_after
        self.on_sleep = on_sleep
        self.count = 0

    async def sleep(self, delay):
        self.calls.append(("sleep", delay))
        self.count += 1
        if self.on_sleep:
            self.on_sleep(self.count)
        if self.stop_after is not None and self.count >= self.stop_after:
            self.plugin._shutting_down = True


def _domains(*pairs):
    return {"data": {"domains": [{"id": i, "name": n} for i, n in pairs]}}


def _domain_body():
    return {
        "id": "d1",
        "name": "Main AV",
        "devices": [
            {
                "id": "dev-001",
                "name": "MIC-01",
                "txChannels": [
                    {"id": "t1", "index": 1, "name": "01"},
                    {"id": "t2", "index": 2, "name": "02"},
                ],
                "rxChannels": [],
            },
            {
                "id": "dev-002",
                "name": "AMP 01.a",
                "txChannels": [],
                "rxChannels": [
                    {"id": "r1", "index": 1, "name": "L", "subscribedDevice": "MIC-01",
                     "subscribedChannel": "01", "summary": "CONNECTED"},
                    {"id": "r2", "index": 2, "name": "R", "subscribedDevice": "",
                     "subscribedChannel": "", "summary": None},
                    {"id": "r3", "index": 3, "name": "C", "subscribedDevice": "MIC-01",
                     "subscribedChannel": None, "summary": "IN_PROGRESS"},
                ],
            },
            {"id": "dev-003", "name": "Stage", "txChannels": None, "rxChannels": None},
        ],
    }


def _domain_answer(domain=None):
    return {"data": {"domain": domain if domain is not None else _domain_body()}}


_OK = {"data": {"DeviceRxChannelsSubscriptionSet": {"ok": True}}}
_NOT_OK = {"data": {"DeviceRxChannelsSubscriptionSet": {"ok": False}}}


def _devices():
    return {d["id"]: d for d in _domain_body()["devices"]}


def _mutation(device_id, subs, allow):
    return (
        "post",
        "/graphql",
        {
            "query": dante_plugin._MUTATION_SET_SUBSCRIPTIONS,
            "variables": {
                "input": {
                    "deviceId": device_id,
                    "subscriptions": subs,
                    "allowSubscriptionToNonExistentDevice": allow,
                    "allowSubscriptionToNonExistentChannel": allow,
                }
            },
        },
    )


def _sub(index, device, channel):
    return {"rxChannelIndex": index, "subscribedDevice": device, "subscribedChannel": channel}


_DOMAINS_POST = ("post", "/graphql", {"query": dante_plugin._QUERY_DOMAINS})


def _domain_post(domain_id="d1"):
    return (
        "post",
        "/graphql",
        {"query": dante_plugin._QUERY_DOMAIN, "variables": {"domainIDInput": domain_id}},
    )


_START_STATE = [
    ("state", "connected", False),
    ("state", "domain_name", ""),
    ("state", "device_count", 0),
    ("state", "subscription_count", 0),
    ("state", "active_preset", ""),
    ("state", "preset_dirty", False),
    ("subscribe", "plugin.dante.action.*", "_on_action"),
]


# ──── start, stop, health ────


async def test_start_with_grouped_settings_connects_and_polls_in_this_order():
    config = {
        "connection": {"ddm_url": " https://ddm.local/api/graphql/ ", "api_key": " key ",
                       "verify_tls": False},
        "polling": {"domain_id": " d2 ", "poll_interval": 7},
    }
    plugin = DanteDDMPlugin()
    api = _Api(config)
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_domains(("d1", "Main"), ("d2", "Overflow"))])}):
        await plugin.start(api)
    assert api.calls == _START_STATE + [
        ("client", {
            "base_url": "https://ddm.local/api",
            "headers": {"Authorization": "key", "Content-Type": "application/json"},
            "verify": False,
            "timeout": 10.0,
        }),
        _DOMAINS_POST,
        ("state", "connected", True),
        ("state", "domain_name", "Overflow"),
        ("event", "connected", None),
        ("task", "dante_poll"),
        ("log", "info", "Connected to DDM, domain: Overflow"),
        ("log", "info", "Dante DDM plugin started"),
    ]
    assert plugin._connected is True
    assert (plugin._domain_id, plugin._domain_name) == ("d2", "Overflow")
    assert api.tasks[0].coro.cr_frame.f_locals["interval"] == 7


async def test_start_that_cannot_connect_schedules_a_reconnect_with_the_settings_read_flat():
    config = {"connection": {"ddm_url": "https://ddm/graphql", "api_key": "k"}}
    plugin = DanteDDMPlugin()
    api = _Api(config)
    with patch.dict(sys.modules, {"httpx": _httpx(api, [ConnectionError("refused")])}):
        await plugin.start(api)
    assert api.calls[len(_START_STATE):] == [
        ("client", {
            "base_url": "https://ddm",
            "headers": {"Authorization": "k", "Content-Type": "application/json"},
            "verify": True,
            "timeout": 10.0,
        }),
        _DOMAINS_POST,
        ("log", "error", "Failed to connect to DDM: refused"),
        ("state", "connected", False),
        ("aclose",),
        ("task", "dante_reconnect"),
        ("log", "info", "Dante DDM plugin started"),
    ]
    assert plugin._client is None
    seen = []

    async def connect(cfg):
        seen.append(cfg)
        return True

    plugin._connect = connect
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls)):
        await api.tasks[0].run()
    assert seen == [{"ddm_url": "https://ddm/graphql", "api_key": "k"}]


async def test_stop_closes_the_client_swallowing_its_error_and_forgets_the_domain():
    plugin, api = _plugin(answers=[{}])
    plugin._client.close_raises = RuntimeError("closing")
    plugin._connected = True
    plugin._domain_id, plugin._domain_name = "d1", "Main"
    plugin._devices = _devices()
    plugin._state_keys = {"tx.a.1"}
    await plugin.stop()
    assert api.calls == [("aclose",), ("log", "info", "Dante DDM plugin stopped")]
    assert plugin._shutting_down is True
    assert (plugin._client, plugin._connected, plugin._devices, plugin._state_keys,
            plugin._domain_id, plugin._domain_name) == (None, False, {}, set(), None, None)


async def test_stop_before_start_logs_nothing():
    plugin = DanteDDMPlugin()
    await plugin.stop()
    assert plugin._shutting_down is True


async def test_health_says_which_domain_and_how_many_devices():
    plugin = DanteDDMPlugin()
    plugin._connected = True
    assert await plugin.health_check() == {"status": "ok", "message": "Connected to DDM (0 devices)"}
    plugin._domain_name = "Main"
    plugin._devices = _devices()
    assert await plugin.health_check() == {"status": "ok", "message": "Connected to Main (3 devices)"}
    plugin._connected = False
    assert await plugin.health_check() == {"status": "error", "message": "Disconnected from DDM"}


# ──── _connect ────


async def test_connect_without_httpx_logs_and_fails():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": None}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k"}) is False
    assert api.calls == [("log", "error", "httpx not installed. Install with: pip install httpx")]


@pytest.mark.parametrize("cfg", [
    {},
    {"ddm_url": "https://x/graphql"},
    {"api_key": "k"},
    {"ddm_url": "  ", "api_key": "k"},
    {"ddm_url": "https://x", "api_key": "   "},
])
async def test_connect_without_a_url_or_key_logs_and_fails(cfg):
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [{}])}):
        assert await plugin._connect(cfg) is False
    assert api.calls == [
        ("log", "error", "DDM URL and API key are required. Configure in plugin settings.")
    ]


async def test_connect_with_a_url_that_is_not_text_raises():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [{}])}):
        with pytest.raises(AttributeError):
            await plugin._connect({"ddm_url": None, "api_key": "k"})


@pytest.mark.parametrize("url, base", [
    ("https://ddm/graphql", "https://ddm"),
    ("https://ddm/graphql/", "https://ddm"),
    ("https://ddm/x/graphql", "https://ddm/x"),
    ("https://ddm", "https://ddm"),
    ("https://ddm/graphql/graphql", "https://ddm/graphql"),
])
async def test_connect_builds_the_base_url_without_the_graphql_path(url, base):
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_domains(("d1", "Main"))])}):
        assert await plugin._connect({"ddm_url": url, "api_key": "k"}) is True
    assert api.calls[0][1]["base_url"] == base


async def test_connect_to_a_domain_not_in_the_list_names_it_by_its_id():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_domains(("d1", "Main"))])}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k", "domain_id": "d9"}) is True
    assert (plugin._domain_id, plugin._domain_name) == ("d9", "d9")
    assert ("state", "domain_name", "d9") in api.calls


async def test_connect_with_no_domain_set_picks_the_first_and_says_so():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_domains(("d1", "Main"), ("d2", "B"))])}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k", "domain_id": "  "}) is True
    assert api.calls[1:] == [
        _DOMAINS_POST,
        ("log", "info", "Auto-selected domain: Main (d1)"),
        ("state", "connected", True),
        ("state", "domain_name", "Main"),
        ("event", "connected", None),
        ("task", "dante_poll"),
        ("log", "info", "Connected to DDM, domain: Main"),
    ]
    assert api.tasks[0].coro.cr_frame.f_locals["interval"] == 5


async def test_connect_to_a_ddm_with_no_domains_closes_the_client_and_fails_quietly():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_domains()])}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k"}) is False
    assert api.calls[1:] == [
        _DOMAINS_POST,
        ("log", "error", "No Dante domains found"),
        ("aclose",),
    ]
    assert plugin._client is None and plugin._connected is False


async def test_connect_whose_close_fails_after_finding_no_domains_reports_that_as_the_failure():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_domains()], RuntimeError("stuck"))}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k"}) is False
    assert api.calls[1:] == [
        _DOMAINS_POST,
        ("log", "error", "No Dante domains found"),
        ("aclose",),
        ("log", "error", "Failed to connect to DDM: stuck"),
        ("state", "connected", False),
        ("aclose",),
    ]
    assert plugin._client is None


async def test_connect_refused_by_graphql_says_so_and_cleans_up():
    plugin, api = _plugin()
    answer = {"errors": [{"message": "Unauthorized"}]}
    with patch.dict(sys.modules, {"httpx": _httpx(api, [answer], RuntimeError("x"))}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k"}) is False
    assert api.calls[1:] == [
        _DOMAINS_POST,
        ("log", "error", "GraphQL error: Unauthorized"),
        ("log", "error", "Failed to connect to DDM: Failed to fetch domains"),
        ("state", "connected", False),
        ("aclose",),
    ]
    assert plugin._client is None


async def test_connect_whose_client_cannot_be_built_fails_with_nothing_to_close():
    plugin, api = _plugin()
    module = types.ModuleType("httpx")

    def refuse(**kwargs):
        raise ValueError("bad url")

    module.AsyncClient = refuse
    with patch.dict(sys.modules, {"httpx": module}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k"}) is False
    assert api.calls == [
        ("log", "error", "Failed to connect to DDM: bad url"),
        ("state", "connected", False),
    ]


async def test_connect_answered_with_an_http_error_fails():
    plugin, api = _plugin()
    with patch.dict(sys.modules, {"httpx": _httpx(api, [_Status("401")])}):
        assert await plugin._connect({"ddm_url": "u", "api_key": "k"}) is False
    assert api.calls[-3:] == [
        ("log", "error", "Failed to connect to DDM: 401"),
        ("state", "connected", False),
        ("aclose",),
    ]


# ──── reconnecting ────


async def test_no_reconnect_is_scheduled_while_shutting_down():
    plugin, api = _plugin()
    plugin._shutting_down = True
    plugin._schedule_reconnect({})
    assert api.calls == [] and plugin._reconnect_task is None


async def test_a_reconnect_already_running_is_not_started_twice_but_a_finished_one_is_replaced():
    plugin, api = _plugin()
    plugin._schedule_reconnect({})
    first = plugin._reconnect_task
    plugin._schedule_reconnect({})
    assert plugin._reconnect_task is first
    first.finished = True
    plugin._schedule_reconnect({})
    assert plugin._reconnect_task is not first
    assert api.calls == [("task", "dante_reconnect"), ("task", "dante_reconnect")]


async def test_reconnect_backs_off_doubling_to_a_minute_and_survives_a_raising_connect():
    plugin, api = _plugin()
    results = [False, RuntimeError("boom"), False, False, False, False, True]

    async def connect(cfg):
        api.calls.append(("connect", cfg))
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    plugin._connect = connect
    plugin._schedule_reconnect({"ddm_url": "u"})
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls)):
        await plugin._reconnect_task.run()
    attempt = [("log", "debug", "Reconnecting to DDM..."), ("connect", {"ddm_url": "u"})]
    assert api.calls == [("task", "dante_reconnect")] + (
        [("sleep", 5)] + attempt
        + [("sleep", 10)] + attempt + [("log", "warning", "Reconnect failed: boom")]
        + [("sleep", 20)] + attempt
        + [("sleep", 40)] + attempt
        + [("sleep", 60)] + attempt
        + [("sleep", 60)] + attempt
        + [("sleep", 60)] + attempt
    )
    assert plugin._reconnect_task is None


async def test_reconnect_stops_once_connected_elsewhere_or_shutting_down():
    plugin, api = _plugin()
    plugin._schedule_reconnect({})
    sleeper = _Sleeper(plugin, api.calls, on_sleep=lambda n: setattr(plugin, "_connected", True))
    with patch.object(dante_plugin, "asyncio", sleeper):
        await plugin._reconnect_task.run()
    assert api.calls == [("task", "dante_reconnect"), ("sleep", 5)]
    assert plugin._reconnect_task is None

    plugin, api = _plugin()
    plugin._schedule_reconnect({})
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls, stop_after=1)):
        await plugin._reconnect_task.run()
    assert api.calls == [("task", "dante_reconnect"), ("sleep", 5)]
    assert plugin._reconnect_task is None


async def test_a_reconnect_that_fails_as_the_plugin_stops_ends_without_sleeping_again():
    plugin, api = _plugin()

    async def connect(cfg):
        api.calls.append(("connect", cfg))
        plugin._shutting_down = True
        return False

    plugin._connect = connect
    plugin._schedule_reconnect({})
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls)):
        await plugin._reconnect_task.run()
    assert api.calls == [
        ("task", "dante_reconnect"),
        ("sleep", 5),
        ("log", "debug", "Reconnecting to DDM..."),
        ("connect", {}),
    ]
    assert plugin._reconnect_task is None


# ──── polling ────


async def test_three_failed_polls_in_a_row_disconnect_and_reconnect_with_the_config_as_saved():
    config = {"connection": {"ddm_url": "https://ddm/graphql", "api_key": "k"}}
    plugin, api = _plugin(config, [_domain_answer(), ConnectionError("a"), ConnectionError("b"),
                                   ConnectionError("c"), ConnectionError("d")])
    plugin._connected = True
    plugin._domain_id = "d1"
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls)):
        await plugin._poll_loop(4)
    tail = api.calls[api.calls.index(("event", "devices.updated", {"device_count": 3})) + 1:]
    assert tail == [
        ("sleep", 4),
        _domain_post(),
        ("log", "warning", "Poll error (1/3): a"),
        ("sleep", 4),
        _domain_post(),
        ("log", "warning", "Poll error (2/3): b"),
        ("sleep", 4),
        _domain_post(),
        ("log", "error", "Poll error (3/3): c"),
        ("state", "connected", False),
        ("event", "disconnected", None),
        ("task", "dante_reconnect"),
    ]
    assert plugin._connected is False
    # The reconnect reads the config as saved, its settings still in their
    # groups, so a Dante set up through the settings form finds no URL here.
    plugin._shutting_down = False
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls, stop_after=2)):
        await api.tasks[0].run()
    assert api.calls[-3:] == [
        ("log", "debug", "Reconnecting to DDM..."),
        ("log", "error", "DDM URL and API key are required. Configure in plugin settings."),
        ("sleep", 10),
    ]


async def test_a_successful_poll_resets_the_failure_count():
    plugin, api = _plugin(answers=[ConnectionError("a"), ConnectionError("b"), _domain_answer(),
                                   ConnectionError("c")])
    plugin._connected = True
    plugin._domain_id = "d1"
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls, stop_after=4)):
        await plugin._poll_loop(3)
    logs = [c for c in api.calls if c[0] == "log"]
    assert logs == [
        ("log", "warning", "Poll error (1/3): a"),
        ("log", "warning", "Poll error (2/3): b"),
        ("log", "warning", "Poll error (1/3): c"),
    ]
    assert plugin._connected is True


async def test_polls_that_fail_while_already_disconnected_keep_polling():
    plugin, api = _plugin(answers=[ConnectionError("x")])
    plugin._domain_id = "d1"
    with patch.object(dante_plugin, "asyncio", _Sleeper(plugin, api.calls, stop_after=4)):
        await plugin._poll_loop(3)
    assert [c for c in api.calls if c[0] != "post"] == [
        ("log", "warning", "Poll error (1/3): x"), ("sleep", 3),
        ("log", "warning", "Poll error (2/3): x"), ("sleep", 3),
        ("log", "error", "Poll error (3/3): x"), ("sleep", 3),
        ("log", "error", "Poll error (4/3): x"), ("sleep", 3),
    ]


# ──── refreshing the state from the domain ────


async def test_refresh_writes_each_channel_then_the_counts_then_the_event():
    plugin, api = _plugin(answers=[_domain_answer()])
    plugin._domain_id = "d1"
    await plugin._refresh_state()
    assert api.calls == [
        _domain_post(),
        ("state", "tx.mic_01.1", "MIC-01 / 01"),
        ("state", "tx.mic_01.2", "MIC-01 / 02"),
        ("state", "rx.amp_01_a.1", "AMP 01.a / L"),
        ("state", "route.amp_01_a.1", "connected"),
        ("state", "route_info.amp_01_a.1", "MIC-01 / 01"),
        ("state", "rx.amp_01_a.2", "AMP 01.a / R"),
        ("state", "route.amp_01_a.2", "none"),
        ("state", "route_info.amp_01_a.2", ""),
        ("state", "rx.amp_01_a.3", "AMP 01.a / C"),
        ("state", "route.amp_01_a.3", "in_progress"),
        ("state", "route_info.amp_01_a.3", "MIC-01 / "),
        ("state", "device_count", 3),
        ("state", "subscription_count", 2),
        ("event", "devices.updated", {"device_count": 3}),
    ]
    assert plugin._devices == _devices()
    assert plugin._state_keys == {
        "tx.mic_01.1", "tx.mic_01.2",
        "rx.amp_01_a.1", "route.amp_01_a.1", "route_info.amp_01_a.1",
        "rx.amp_01_a.2", "route.amp_01_a.2", "route_info.amp_01_a.2",
        "rx.amp_01_a.3", "route.amp_01_a.3", "route_info.amp_01_a.3",
    }


async def test_refresh_with_the_same_devices_clears_the_keys_gone_and_sends_no_event():
    smaller = _domain_body()
    smaller["devices"][0]["txChannels"] = [{"id": "t1", "index": 1, "name": "01"}]
    smaller["devices"][1]["rxChannels"] = smaller["devices"][1]["rxChannels"][:1]
    plugin, api = _plugin(answers=[_domain_answer(), _domain_answer(smaller)])
    plugin._domain_id = "d1"
    await plugin._refresh_state()
    api.calls.clear()
    await plugin._refresh_state()
    assert api.calls[:5] == [
        _domain_post(),
        ("state", "tx.mic_01.1", "MIC-01 / 01"),
        ("state", "rx.amp_01_a.1", "AMP 01.a / L"),
        ("state", "route.amp_01_a.1", "connected"),
        ("state", "route_info.amp_01_a.1", "MIC-01 / 01"),
    ]
    assert sorted(api.calls[5:12]) == [
        ("state", key, None) for key in sorted([
            "tx.mic_01.2", "rx.amp_01_a.2", "route.amp_01_a.2", "route_info.amp_01_a.2",
            "rx.amp_01_a.3", "route.amp_01_a.3", "route_info.amp_01_a.3",
        ])
    ]
    assert api.calls[12:] == [
        ("state", "device_count", 3),
        ("state", "subscription_count", 1),
    ]


async def test_refresh_of_a_domain_with_no_device_list_counts_nothing_and_announces_the_change():
    plugin, api = _plugin(answers=[_domain_answer({"id": "d1", "devices": None})])
    plugin._domain_id = "d1"
    plugin._devices = {"old": {"id": "old", "name": "Old"}}
    plugin._state_keys = {"tx.old.1"}
    await plugin._refresh_state()
    assert api.calls == [
        _domain_post(),
        ("state", "tx.old.1", None),
        ("state", "device_count", 0),
        ("state", "subscription_count", 0),
        ("event", "devices.updated", {"device_count": 0}),
    ]
    assert plugin._devices == {} and plugin._state_keys == set()


async def test_refresh_refused_by_graphql_or_without_a_domain_changes_nothing():
    plugin, api = _plugin(answers=[{"errors": [{"message": "busy"}]}])
    plugin._domain_id = "d1"
    plugin._devices = _devices()
    await plugin._refresh_state()
    assert api.calls == [_domain_post(), ("log", "error", "GraphQL error: busy")]
    assert plugin._devices == _devices()

    plugin, api = _plugin(answers=[_domain_answer()])
    await plugin._refresh_state()
    assert api.calls == []

    plugin, api = _plugin(answers=[{"data": {}}])
    plugin._domain_id = "d1"
    await plugin._refresh_state()
    assert api.calls == [_domain_post()]


# ──── the GraphQL request ────


async def test_graphql_without_a_client_answers_none():
    plugin, api = _plugin()
    assert await plugin._graphql("q", {"a": 1}) is None
    assert api.calls == []


async def test_graphql_sends_variables_only_when_there_are_some():
    plugin, api = _plugin(answers=[{"data": {"x": 1}}])
    assert await plugin._graphql("q", {}) == {"x": 1}
    assert await plugin._graphql("q", {"a": 1}) == {"x": 1}
    assert api.calls == [
        ("post", "/graphql", {"query": "q"}),
        ("post", "/graphql", {"query": "q", "variables": {"a": 1}}),
    ]


async def test_graphql_errors_are_logged_by_their_first_message_and_answer_none():
    plugin, api = _plugin(answers=[{"errors": []}, {"errors": [{"code": 7}, {"message": "2nd"}]},
                                   {"errors": [{"message": "first"}], "data": {"x": 1}}])
    assert await plugin._graphql("q") is None
    assert await plugin._graphql("q") is None
    assert await plugin._graphql("q") is None
    assert [c for c in api.calls if c[0] == "log"] == [
        ("log", "error", "GraphQL error: Unknown"),
        ("log", "error", "GraphQL error: {'code': 7}"),
        ("log", "error", "GraphQL error: first"),
    ]


async def test_graphql_without_data_answers_none_and_an_http_error_raises():
    plugin, api = _plugin(answers=[{}, _Status("500")])
    assert await plugin._graphql("q") is None
    with pytest.raises(_Status):
        await plugin._graphql("q")


async def test_fetch_domains_reads_an_absent_list_as_empty():
    plugin, api = _plugin(answers=[{"data": {}}])
    assert await plugin._fetch_domains() == []


# ──── subscription mutations ────


async def test_one_subscription_refuses_unknown_devices_and_reads_ok():
    plugin, api = _plugin(answers=[_OK])
    assert await plugin._set_subscription("dev-002", 2, "MIC-01", "01") is True
    assert api.calls == [_mutation("dev-002", [_sub(2, "MIC-01", "01")], False)]


@pytest.mark.parametrize("answer", [_NOT_OK, {"data": {}}, {"data": {"DeviceRxChannelsSubscriptionSet": {}}},
                                    {"errors": [{"message": "no"}]}])
async def test_one_subscription_not_confirmed_is_false(answer):
    plugin, api = _plugin(answers=[answer])
    assert await plugin._set_subscription("dev-002", 2, "", "") is False


async def test_one_subscription_that_cannot_be_sent_logs_and_is_false():
    plugin, api = _plugin(answers=[ConnectionError("gone")])
    assert await plugin._set_subscription("dev-002", 2, "", "") is False
    assert api.calls[-1] == ("log", "error", "Subscription request failed: gone")


async def test_a_batch_of_subscriptions_allows_unknown_devices_and_reads_ok():
    plugin, api = _plugin(answers=[_OK])
    subs = [_sub(1, "A", "1"), _sub(2, "", "")]
    assert await plugin._set_subscriptions_batch("dev-9", subs) is True
    assert api.calls == [_mutation("dev-9", subs, True)]

    plugin, api = _plugin(answers=[_NOT_OK])
    assert await plugin._set_subscriptions_batch("dev-9", subs) is False
    plugin, api = _plugin(answers=[{"errors": [{"message": "no"}]}])
    assert await plugin._set_subscriptions_batch("dev-9", subs) is False
    plugin, api = _plugin(answers=[ConnectionError("gone")])
    assert await plugin._set_subscriptions_batch("dev-9", subs) is False
    assert api.calls[-1] == ("log", "error", "Batch subscription request failed: gone")


# ──── route and unroute ────


async def test_route_sends_then_announces_logs_and_marks_the_preset_changed():
    plugin, api = _plugin(answers=[_OK])
    plugin._devices = _devices()
    assert await plugin.route("AMP 01.a", 2, "MIC-01", "02") is True
    assert api.calls == [
        _mutation("dev-002", [_sub(2, "MIC-01", "02")], False),
        ("event", "route.changed", {"rx_device": "AMP 01.a", "rx_channel_index": 2,
                                    "tx_device": "MIC-01", "tx_channel": "02", "active": True}),
        ("log", "info", "Routed MIC-01/02 -> AMP 01.a/2"),
        ("state", "preset_dirty", True),
    ]


async def test_route_the_ddm_refuses_announces_the_failure():
    plugin, api = _plugin(answers=[_NOT_OK])
    plugin._devices = _devices()
    assert await plugin.route("AMP 01.a", 2, "MIC-01", "02") is False
    assert api.calls[1:] == [
        ("event", "route.failed", {"rx_device": "AMP 01.a", "rx_channel_index": 2,
                                   "tx_device": "MIC-01", "tx_channel": "02",
                                   "reason": "mutation_failed"}),
        ("log", "error", "Failed to route MIC-01/02 -> AMP 01.a/2"),
    ]


async def test_route_to_an_unknown_device_announces_why():
    plugin, api = _plugin(answers=[_OK])
    plugin._devices = _devices()
    assert await plugin.route("amp 01.a", 2, "MIC-01", "02") is False
    assert api.calls == [
        ("log", "error", "Rx device not found: amp 01.a"),
        ("event", "route.failed", {"rx_device": "amp 01.a", "rx_channel_index": 2,
                                   "reason": "rx_device_not_found"}),
    ]


async def test_unroute_sends_blanks_then_announces_logs_and_marks_the_preset_changed():
    plugin, api = _plugin(answers=[_OK])
    plugin._devices = _devices()
    assert await plugin.unroute("AMP 01.a", 1) is True
    assert api.calls == [
        _mutation("dev-002", [_sub(1, "", "")], False),
        ("event", "route.changed", {"rx_device": "AMP 01.a", "rx_channel_index": 1,
                                    "tx_device": "", "tx_channel": "", "active": False}),
        ("log", "info", "Unrouted AMP 01.a/1"),
        ("state", "preset_dirty", True),
    ]


async def test_unroute_refused_is_silent_and_an_unknown_device_is_only_logged():
    plugin, api = _plugin(answers=[_NOT_OK])
    plugin._devices = _devices()
    assert await plugin.unroute("AMP 01.a", 1) is False
    assert api.calls == [_mutation("dev-002", [_sub(1, "", "")], False)]
    api.calls.clear()
    assert await plugin.unroute("Nope", 1) is False
    assert api.calls == [("log", "error", "Rx device not found: Nope")]


# ──── presets ────


_MEETING = [
    {"tx_device": "MIC-01", "tx_channel": "02", "rx_device": "AMP 01.a", "rx_channel_index": "3"},
    {"tx_device": "MIC-01", "rx_device": "Stage", "rx_channel_index": 0},
    {"rx_device": "AMP 01.a", "rx_channel_index": None},
    {"tx_device": "X", "tx_channel": "9", "rx_device": "AMP 01.a", "rx_channel_index": 4},
]


async def test_recall_clears_first_then_sets_each_receivers_routes_in_one_request():
    plugin, api = _plugin({"_presets": {"Meeting": _MEETING}}, answers=[_OK])
    plugin._devices = _devices()
    assert await plugin.recall_preset("Meeting") is True
    assert api.calls == [
        _mutation("dev-002", [_sub(1, "", ""), _sub(3, "", "")], True),
        ("state", "active_preset", ""),
        ("state", "preset_dirty", False),
        ("log", "info", "Cleared all subscriptions"),
        _mutation("dev-002", [_sub(3, "MIC-01", "02"), _sub(0, "", ""), _sub(4, "X", "9")], True),
        _mutation("dev-003", [_sub(0, "MIC-01", "")], True),
        ("event", "preset.recalled", {"preset_name": "Meeting"}),
        ("state", "active_preset", "Meeting"),
        ("state", "preset_dirty", False),
        ("log", "info", "Recalled preset: Meeting"),
    ]


async def test_recall_of_an_unknown_preset_changes_nothing():
    plugin, api = _plugin({"_presets": {"Meeting": []}}, answers=[_OK])
    plugin._devices = _devices()
    assert await plugin.recall_preset("meeting") is False
    assert api.calls == [("log", "error", "Preset not found: meeting")]


async def test_recall_of_an_empty_preset_only_clears_and_leaves_no_preset_shown():
    plugin, api = _plugin({"_presets": {"Off": []}}, answers=[_OK])
    plugin._devices = _devices()
    assert await plugin.recall_preset("Off") is True
    assert api.calls[1:] == [
        ("state", "active_preset", ""),
        ("state", "preset_dirty", False),
        ("log", "info", "Cleared all subscriptions"),
        ("event", "preset.recalled", {"preset_name": "Off"}),
        ("log", "info", "Recalled preset: Off (cleared all)"),
    ]


async def test_recall_whose_clear_fails_warns_and_still_applies_the_routes():
    plugin, api = _plugin({"_presets": {"Off": [], "On": _MEETING[:1]}},
                          answers=[_NOT_OK, _OK])
    plugin._devices = _devices()
    assert await plugin.recall_preset("Off") is False
    assert api.calls[1:] == [
        ("log", "warning", "Warning: failed to clear some subscriptions before preset recall"),
        ("event", "preset.recalled", {"preset_name": "Off"}),
        ("log", "info", "Recalled preset: Off (cleared all)"),
    ]
    plugin, api = _plugin({"_presets": {"On": _MEETING[:1]}}, answers=[_NOT_OK, _OK])
    plugin._devices = _devices()
    assert await plugin.recall_preset("On") is True
    assert api.calls[1:] == [
        ("log", "warning", "Warning: failed to clear some subscriptions before preset recall"),
        _mutation("dev-002", [_sub(3, "MIC-01", "02")], True),
        ("event", "preset.recalled", {"preset_name": "On"}),
        ("state", "active_preset", "On"),
        ("state", "preset_dirty", False),
        ("log", "info", "Recalled preset: On"),
    ]


async def test_recall_with_a_missing_receiver_or_a_refused_batch_is_a_partial_failure():
    routes = [
        {"tx_device": "A", "tx_channel": "1", "rx_device": "Gone", "rx_channel_index": 1},
        {"tx_device": "A", "tx_channel": "1", "rx_device": "AMP 01.a", "rx_channel_index": 2},
        {"tx_device": "A", "tx_channel": "1", "rx_device": "Stage", "rx_channel_index": 1},
        {"tx_device": "A", "tx_channel": "1"},
    ]
    plugin, api = _plugin({"_presets": {"P": routes}}, answers=[_OK, _OK, _NOT_OK, _OK])
    plugin._devices = _devices()
    assert await plugin.recall_preset("P") is False
    assert api.calls[4:] == [
        ("log", "error", "Preset: Rx device not found: Gone"),
        _mutation("dev-002", [_sub(2, "A", "1")], True),
        _mutation("dev-003", [_sub(1, "A", "1")], True),
        ("log", "error", "Preset: Failed to set routes on Stage"),
        ("log", "error", "Preset: Rx device not found: "),
        ("event", "route.failed", {"reason": "preset_partial_failure", "preset_name": "P"}),
    ]


async def test_presets_that_are_not_a_mapping_read_as_none():
    plugin, api = _plugin({"_presets": [["Meeting"]]})
    assert plugin._parse_presets() == {}
    assert await plugin.recall_preset("Meeting") is False


async def test_clear_all_skips_receivers_with_nothing_routed_and_reports_a_refusal():
    devices = _devices()
    devices["dev-004"] = {"id": "dev-004", "name": "Spare", "rxChannels": [
        {"index": 1, "subscribedDevice": None}, {"index": 2}]}
    devices["dev-005"] = {"id": "dev-005", "name": "Other", "rxChannels": [
        {"index": 7, "subscribedDevice": "B"}]}
    plugin, api = _plugin(answers=[_NOT_OK, _OK])
    plugin._devices = devices
    assert await plugin._clear_all_subscriptions() is False
    assert api.calls == [
        _mutation("dev-002", [_sub(1, "", ""), _sub(3, "", "")], True),
        _mutation("dev-005", [_sub(7, "", "")], True),
    ]


async def test_clear_all_with_nothing_routed_still_resets_the_preset_shown():
    plugin, api = _plugin(answers=[_OK])
    plugin._devices = {"dev-001": _devices()["dev-001"]}
    assert await plugin._clear_all_subscriptions() is True
    assert api.calls == [
        ("state", "active_preset", ""),
        ("state", "preset_dirty", False),
        ("log", "info", "Cleared all subscriptions"),
    ]


async def test_snapshot_lists_each_routed_receiver_channel_in_device_order():
    plugin, api = _plugin()
    plugin._devices = _devices()
    assert plugin._snapshot_current_routes() == [
        {"tx_device": "MIC-01", "tx_channel": "01", "rx_device": "AMP 01.a", "rx_channel_index": 1},
        {"tx_device": "MIC-01", "tx_channel": "", "rx_device": "AMP 01.a", "rx_channel_index": 3},
    ]


# ──── saving, updating and deleting a preset ────


_SNAP = [
    {"tx_device": "MIC-01", "tx_channel": "01", "rx_device": "AMP 01.a", "rx_channel_index": 1},
    {"tx_device": "MIC-01", "tx_channel": "", "rx_device": "AMP 01.a", "rx_channel_index": 3},
]


async def test_save_keeps_the_other_settings_and_shows_the_new_preset():
    plugin, api = _plugin({"connection": {"api_key": "k"}, "_presets": {"Old": []}})
    plugin._devices = _devices()
    await plugin._handle_save_preset({"name": "  New  "})
    assert api.calls == [
        ("save_config", {"connection": {"api_key": "k"}, "_presets": {"Old": [], "New": _SNAP}}),
        ("state", "active_preset", "New"),
        ("state", "preset_dirty", False),
        ("log", "info", "Saved preset: New (2 routes)"),
    ]


@pytest.mark.parametrize("payload, message", [
    ({}, "Save preset: name is required"),
    ({"name": None}, "Save preset: name is required"),
    ({"name": "  "}, "Save preset: name is required"),
    ({"name": "Old"}, "Preset 'Old' already exists"),
])
async def test_save_refuses_a_blank_or_taken_name(payload, message):
    plugin, api = _plugin({"_presets": {"Old": []}})
    await plugin._handle_save_preset(payload)
    assert api.calls == [("log", "error", message)]


async def test_update_replaces_the_routes_and_marks_them_unchanged():
    plugin, api = _plugin({"_presets": {"Old": [{"x": 1}], "Keep": []}})
    plugin._devices = _devices()
    await plugin._handle_update_preset({"name": "Old"})
    assert api.calls == [
        ("save_config", {"_presets": {"Old": _SNAP, "Keep": []}}),
        ("state", "preset_dirty", False),
        ("log", "info", "Updated preset: Old (2 routes)"),
    ]


@pytest.mark.parametrize("payload, message", [
    ({"name": ""}, "Update preset: name is required"),
    ({"name": "New"}, "Preset 'New' does not exist"),
])
async def test_update_refuses_a_blank_or_unknown_name(payload, message):
    plugin, api = _plugin({"_presets": {"Old": []}})
    await plugin._handle_update_preset(payload)
    assert api.calls == [("log", "error", message)]


async def test_delete_removes_it_and_shows_no_preset():
    plugin, api = _plugin({"_presets": {"Old": [], "Keep": []}, "x": 1})
    await plugin._handle_delete_preset({"name": " Old"})
    assert api.calls == [
        ("save_config", {"_presets": {"Keep": []}, "x": 1}),
        ("state", "active_preset", ""),
        ("state", "preset_dirty", False),
        ("log", "info", "Deleted preset: Old"),
    ]


@pytest.mark.parametrize("payload, message", [
    ({"name": None}, "Delete preset: name is required"),
    ({"name": "New"}, "Preset 'New' does not exist"),
])
async def test_delete_refuses_a_blank_or_unknown_name(payload, message):
    plugin, api = _plugin({"_presets": {"Old": []}})
    await plugin._handle_delete_preset(payload)
    assert api.calls == [("log", "error", message)]


async def test_a_preset_edit_with_presets_that_are_not_a_mapping_starts_from_none():
    plugin, api = _plugin({"_presets": "broken"})
    await plugin._handle_save_preset({"name": "A"})
    assert api.calls[0] == ("save_config", {"_presets": {"A": []}})


# ──── actions ────


async def _dispatch(event_name, payload, config=None):
    plugin, api = _plugin(config)
    seen = []
    names = ["_handle_refresh_domains", "recall_preset", "_handle_save_preset",
             "_handle_update_preset", "_handle_delete_preset", "_clear_all_subscriptions",
             "_handle_route_action", "_handle_unroute_action"]
    for name in names:
        async def record(*args, _name=name):
            seen.append((_name, args))
        setattr(plugin, name, record)
    await plugin._on_action(event_name, payload)
    return seen, api.calls


@pytest.mark.parametrize("event_name, payload, expected", [
    ("plugin.dante.action.refresh_domains", {"x": 1}, [("_handle_refresh_domains", ())]),
    ("plugin.dante.action.recall_preset", {"preset_name": "M"}, [("recall_preset", ("M",))]),
    ("plugin.dante.action.save_preset", {"name": "M"}, [("_handle_save_preset", ({"name": "M"},))]),
    ("plugin.dante.action.save_preset", None, [("_handle_save_preset", ({},))]),
    ("plugin.dante.action.update_preset", None, [("_handle_update_preset", ({},))]),
    ("plugin.dante.action.delete_preset", {"name": "M"}, [("_handle_delete_preset", ({"name": "M"},))]),
    ("plugin.dante.action.clear_all", {"x": 1}, [("_clear_all_subscriptions", ())]),
    ("plugin.dante.action.route", None, [("_handle_route_action", ({},))]),
    ("plugin.dante.action.route", {"row": "a.1"}, [("_handle_route_action", ({"row": "a.1"},))]),
    ("plugin.dante.action.unroute", {"row": "a.1"}, [("_handle_unroute_action", ({"row": "a.1"},))]),
    ("unroute", None, [("_handle_unroute_action", ({},))]),
    ("plugin.dante.action.reboot", {"x": 1}, []),
    ("plugin.dante.action.", {"x": 1}, []),
    ("plugin.dante.action.Route", {"x": 1}, []),
])
async def test_each_action_name_runs_its_handler_with_its_payload(event_name, payload, expected):
    seen, calls = await _dispatch(event_name, payload)
    assert seen == expected and calls == []


@pytest.mark.parametrize("payload, config, logs", [
    (None, {"_presets": {"A": [], "B": []}}, [("log", "info", "Available presets: A, B")]),
    ({"preset_name": ""}, {}, [("log", "warning", "No presets configured")]),
    ({"preset_name": None}, {"_presets": "x"}, [("log", "warning", "No presets configured")]),
])
async def test_a_recall_with_no_preset_name_lists_the_presets_instead(payload, config, logs):
    seen, calls = await _dispatch("plugin.dante.action.recall_preset", payload, config)
    assert seen == [] and calls == logs


async def test_refresh_domains_logs_each_domain_or_why_there_are_none():
    plugin, api = _plugin(answers=[_domains(("d1", "Main"), ("d2", "B"))])
    await plugin._handle_refresh_domains()
    assert api.calls[1:] == [
        ("log", "info", "Domain: Main (ID: d1)"),
        ("log", "info", "Domain: B (ID: d2)"),
    ]
    plugin, api = _plugin(answers=[_domains()])
    await plugin._handle_refresh_domains()
    assert api.calls[1:] == [("log", "info", "No domains found")]
    plugin, api = _plugin()
    await plugin._handle_refresh_domains()
    assert api.calls == [("log", "error", "Failed to fetch domains")]
    plugin, api = _plugin(answers=[ConnectionError("gone")])
    await plugin._handle_refresh_domains()
    assert api.calls[1:] == [("log", "error", "Failed to fetch domains: gone")]


# ──── the two forms a route request comes in ────


def _router(devices=None):
    plugin, api = _plugin()
    plugin._devices = _devices() if devices is None else devices
    routed = []

    async def route(*args):
        routed.append(("route",) + args)

    async def unroute(*args):
        routed.append(("unroute",) + args)

    plugin.route = route
    plugin.unroute = unroute
    return plugin, api, routed


@pytest.mark.parametrize("payload, expected, logs", [
    ({"row": "amp_01_a.2", "col": "mic_01.2"}, [("route", "AMP 01.a", 2, "MIC-01", "02")], []),
    ({"row": "amp_01_a.9", "col": "mic_01.1"}, [("route", "AMP 01.a", 9, "MIC-01", "01")], []),
    ({"row": "amp_01_a.2", "col": "mic_01.3"}, [],
     [("log", "error", "Could not resolve matrix reference: row=amp_01_a.2 col=mic_01.3")]),
    ({"row": "nope.1", "col": "mic_01.1", "rx_device": "AMP 01.a"}, [],
     [("log", "error", "Could not resolve matrix reference: row=nope.1 col=mic_01.1")]),
    ({"row": "amp_01_a.2", "rx_device": "AMP 01.a", "rx_channel_index": 1,
      "tx_device_name": "MIC-01", "tx_channel_name": "01"},
     [("route", "AMP 01.a", 1, "MIC-01", "01")], []),
    ({"rx_device": "AMP 01.a", "rx_channel_index": "1", "tx_device_name": "A", "tx_channel_name": "B"},
     [("route", "AMP 01.a", "1", "A", "B")], []),
    ({"rx_device": "AMP 01.a", "rx_channel_index": 0, "tx_device_name": "A", "tx_channel_name": "B"},
     [], [("log", "error", "Route action missing required fields")]),
    ({"rx_device": "AMP 01.a", "rx_channel_index": 1, "tx_device_name": "A"},
     [], [("log", "error", "Route action missing required fields")]),
    ({}, [], [("log", "error", "Route action missing required fields")]),
])
async def test_a_route_request_by_matrix_cell_or_by_name(payload, expected, logs):
    plugin, api, routed = _router()
    await plugin._handle_route_action(payload)
    assert routed == expected and api.calls == logs


@pytest.mark.parametrize("payload, expected, logs", [
    ({"row": "amp_01_a.3", "col": "whatever"}, [("unroute", "AMP 01.a", 3)], []),
    ({"row": "mic_01.1"}, [("unroute", "MIC-01", 1)], []),
    ({"row": "amp_01_a.x", "rx_device": "AMP 01.a", "rx_channel_index": 1}, [],
     [("log", "error", "Could not resolve matrix reference: row=amp_01_a.x")]),
    ({"rx_device": "AMP 01.a", "rx_channel_index": 2}, [("unroute", "AMP 01.a", 2)], []),
    ({"rx_device": "AMP 01.a"}, [], [("log", "error", "Unroute action missing required fields")]),
    ({"rx_channel_index": 2}, [], [("log", "error", "Unroute action missing required fields")]),
])
async def test_an_unroute_request_by_matrix_row_or_by_name(payload, expected, logs):
    plugin, api, routed = _router()
    await plugin._handle_unroute_action(payload)
    assert routed == expected and api.calls == logs


# ──── reading a matrix row or column back ────


@pytest.mark.parametrize("ref, direction, expected", [
    ("amp_01_a.2", "rx", ("AMP 01.a", 2)),
    ("amp_01_a.99", "rx", ("AMP 01.a", 99)),
    ("amp_01_a.2", "anything", ("AMP 01.a", 2)),
    ("mic_01.2", "tx", ("MIC-01", "02")),
    ("mic_01.3", "tx", (None, None)),
    ("stage.1", "tx", (None, None)),
    ("mic_01.x", "rx", (None, None)),
    ("mic_01", "rx", (None, None)),
    ("MIC-01.1", "rx", (None, None)),
    ("amp_01.a.2", "rx", (None, None)),
    ("mic_01. 2 ", "rx", ("MIC-01", 2)),
])
async def test_a_matrix_reference_names_a_device_and_a_channel(ref, direction, expected):
    plugin, api = _plugin()
    plugin._devices = _devices()
    assert plugin._resolve_matrix_ref(ref, direction) == expected


async def test_two_devices_with_the_same_key_resolve_to_the_first_even_without_the_channel():
    plugin, api = _plugin()
    plugin._devices = {
        "a": {"id": "a", "name": "Desk-1", "txChannels": [{"index": 1, "name": "L"}]},
        "b": {"id": "b", "name": "Desk 1", "txChannels": [{"index": 2, "name": "R"}]},
    }
    assert plugin._resolve_matrix_ref("desk_1.1", "tx") == ("Desk-1", "L")
    assert plugin._resolve_matrix_ref("desk_1.2", "tx") == (None, None)
    assert plugin._resolve_matrix_ref("desk_1.2", "rx") == ("Desk-1", 2)
