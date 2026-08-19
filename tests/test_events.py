from console.events import EventBus


async def test_subscriber_receives_published_event():
    bus = EventBus()
    stream = bus.subscribe()
    bus.publish({"type": "status", "status": "ready"})
    assert (await anext(stream))["status"] == "ready"


async def test_slow_subscriber_drops_oldest_instead_of_blocking():
    bus = EventBus()
    stream = bus.subscribe()
    for index in range(150):
        bus.publish({"type": "log", "index": index})
    first = await anext(stream)
    assert first["index"] > 0  # oldest dropped, publisher never blocked


async def test_two_subscribers_both_get_events():
    bus = EventBus()
    first, second = bus.subscribe(), bus.subscribe()
    bus.publish({"type": "status", "status": "starting"})
    assert (await anext(first))["status"] == "starting"
    assert (await anext(second))["status"] == "starting"


def test_log_ring_buffer_keeps_last_lines():
    bus = EventBus()
    for index in range(600):
        bus.publish_log(f"line {index}")
    lines = bus.log_lines(limit=10)
    assert lines[-1] == "line 599"
    assert len(lines) == 10


def test_log_lines_returns_everything_when_under_limit():
    bus = EventBus()
    bus.publish_log("one")
    assert bus.log_lines(limit=50) == ["one"]


async def test_publish_log_also_reaches_subscribers():
    bus = EventBus()
    stream = bus.subscribe()
    bus.publish_log("hello")
    event = await anext(stream)
    assert event == {"type": "log", "line": "hello"}


async def test_unsubscribe_removes_queue():
    bus = EventBus()
    stream = bus.subscribe()
    await stream.aclose()
    bus.publish({"type": "status", "status": "ready"})
    assert bus.subscriber_count == 0
