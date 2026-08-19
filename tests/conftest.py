import asyncio
import socket
import struct
from pathlib import Path

import pytest

from console.settings import Settings

FAKE_ENGINE = Path(__file__).parent / "fake_gigastt.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wav_bytes(seconds: float = 1.0, sample_rate: int = 16000) -> bytes:
    """Minimal silent mono 16-bit WAV, used to exercise duration parsing."""
    frames = int(seconds * sample_rate)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


OPUS_HEAD = (
    b"OpusHead"
    + bytes((1, 2))
    + struct.pack("<HIh", 312, 48000, 0)  # pre-skip, input rate, output gain
    + b"\x00"  # channel mapping family
)


def opus_code3_packet(frames: list[bytes]) -> bytes:
    """A 60 ms Opus packet: several equal frames in one CBR code 3 packet.

    This is the framing Chromium's MediaRecorder emits, and the shape the engine
    rejected until 2.17.0 (ekhodzitsky/gigastt#259).
    """
    return bytes((0xFF, len(frames))) + b"".join(frames)


def webm_opus_bytes(
    packets: list[bytes],
    *,
    codec: bytes = b"A_OPUS",
    codec_private: bytes | None = OPUS_HEAD,
    lacing: int = 0,
    decoy: tuple[int, bytes] | None = None,
) -> bytes:
    """WebM shaped the way `MediaRecorder` writes it: sizeless Segment and Cluster.

    `decoy` adds a second track whose blocks must be ignored; `lacing` sets the
    block flag bits that mean several frames share one block.
    """

    def sized(element_id: bytes, payload: bytes) -> bytes:
        return element_id + b"\x01" + len(payload).to_bytes(7, "big") + payload

    def block(track: int, payload: bytes) -> bytes:
        head = bytes((0x80 | track,)) + struct.pack(">hB", 0, 0x80 | lacing)
        return sized(b"\xa3", head + payload)

    track_entry = sized(b"\xd7", b"\x01") + sized(b"\x86", codec)
    if codec_private is not None:
        track_entry += sized(b"\x63\xa2", codec_private)
    tracks = sized(b"\xae", track_entry)
    if decoy is not None:
        number, decoy_codec = decoy
        tracks += sized(b"\xae", sized(b"\xd7", bytes((number,))) + sized(b"\x86", decoy_codec))

    blocks = b"".join(block(1, packet) for packet in packets)
    if decoy is not None:
        blocks += block(decoy[0], b"ignored payload")

    return (
        sized(b"\x1a\x45\xdf\xa3", sized(b"\x42\x82", b"webm"))
        + b"\x18\x53\x80\x67\xff"  # Segment, size unknown
        + sized(b"\x16\x54\xae\x6b", tracks)
        + b"\x1f\x43\xb6\x75\xff"  # Cluster, size unknown
        + sized(b"\xe7", b"\x00")  # Timecode
        + blocks
    )


def route_paths(app) -> set[str]:
    """All routed paths, including those inside included routers.

    Recent FastAPI keeps `include_router` results as lazy `_IncludedRouter` entries
    instead of flattening them into `app.routes`, so a plain list comprehension over
    `app.routes` misses every API endpoint.
    """
    paths: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
            nested = getattr(route, "original_router", None) or getattr(route, "router", None)
            if nested is not None:
                walk(getattr(nested, "routes", []))

    walk(app.routes)
    return paths


async def wait_for(predicate, timeout: float = 10.0, interval: float = 0.1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def engine_bin() -> str:
    return str(FAKE_ENGINE)


@pytest.fixture
def bus():
    from console.events import EventBus

    return EventBus()


@pytest.fixture
def console_settings(tmp_path, engine_bin) -> Settings:
    return Settings(
        _env_file=None,
        engine_bin=engine_bin,
        engine_port=free_port(),
        model_dir=tmp_path / "models",
        data_dir=tmp_path / "data",
    )


async def _client(settings):
    """HTTP client bound to a freshly built app (no lifespan, tests drive deploys)."""
    import httpx

    from console.main import create_app

    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://console", timeout=30)
    client.app = app
    return client


@pytest.fixture
async def client_stopped(console_settings):
    client = await _client(console_settings)
    try:
        yield client
    finally:
        await client.app.state.supervisor.shutdown()
        await client.aclose()


@pytest.fixture
async def client_ready(client_stopped):
    from console.state import EngineConfig

    await client_stopped.app.state.supervisor.deploy(EngineConfig())
    assert client_stopped.app.state.supervisor.status == "ready"
    yield client_stopped


@pytest.fixture
async def live_client(console_settings):
    """Client talking to a real uvicorn server.

    Needed for endless responses: httpx's ASGITransport buffers the whole body
    before returning, so an SSE stream would deadlock in-process.
    """
    import httpx
    import uvicorn

    from console.main import create_app

    app = create_app(console_settings)
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30)
    client.app = app
    try:
        yield client
    finally:
        await client.aclose()
        await app.state.supervisor.shutdown()
        server.should_exit = True
        await task


@pytest.fixture
async def client_ready_with_key(console_settings):
    from console.state import EngineConfig

    client = await _client(console_settings.model_copy(update={"api_key": "secret"}))
    try:
        await client.app.state.supervisor.deploy(EngineConfig())
        yield client
    finally:
        await client.app.state.supervisor.shutdown()
        await client.aclose()
