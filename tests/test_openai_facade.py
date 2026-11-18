import httpx
import pytest

from conftest import wav_bytes

AUDIO = {"file": ("a.wav", wav_bytes(seconds=2), "audio/wav")}


async def test_transcription_proxied_to_engine(client_ready):
    response = await client_ready.post(
        "/v1/audio/transcriptions", files=AUDIO, data={"model": "whisper-1"}
    )
    assert response.status_code == 200
    assert response.json() == {"text": "привет мир"}


async def test_text_format_passes_through(client_ready):
    response = await client_ready.post(
        "/v1/audio/transcriptions", files=AUDIO, data={"response_format": "text"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == "привет мир"


async def test_srt_and_verbose_json_pass_through(client_ready):
    srt = await client_ready.post(
        "/v1/audio/transcriptions", files=AUDIO, data={"response_format": "srt"}
    )
    assert "00:00:00,000 --> 00:00:02,000" in srt.text

    verbose = await client_ready.post(
        "/v1/audio/transcriptions", files=AUDIO, data={"response_format": "verbose_json"}
    )
    payload = verbose.json()
    assert payload["language"] == "ru" and payload["segments"][0]["text"] == "привет мир"


async def test_streaming_sse_passes_through(client_ready):
    async with client_ready.stream(
        "POST", "/v1/audio/transcriptions", files=AUDIO, data={"stream": "true"}
    ) as response:
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "transcript.text.delta" in body
    assert "[DONE]" in body


@pytest.mark.parametrize("path", ["/v1/audio/transcriptions", "/v1/transcribe"])
async def test_failed_stream_releases_engine_and_is_not_a_success(client_ready, path, monkeypatch):
    class BrokenStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadError("test stream ended early")

        async def aclose(self):
            self.closed = True

    stream = BrokenStream()
    transport = httpx.MockTransport(lambda req: httpx.Response(200, stream=stream))
    upstream = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: upstream)
    with pytest.raises(httpx.ReadError):
        await client_ready.post(path, files=AUDIO)
    assert stream.closed and upstream.is_closed
    assert client_ready.app.state.metrics.snapshot()["count"] == 0


async def test_models_endpoint_lists_heads_and_alias(client_ready):
    payload = (await client_ready.get("/v1/models")).json()
    ids = [model["id"] for model in payload["data"]]
    assert payload["object"] == "list"
    assert "gigaam-v3-rnnt" in ids
    assert "gigaam-multilingual-large-ctc" in ids
    assert "whisper-1" in ids
    deployed = [model["id"] for model in payload["data"] if model["deployed"]]
    assert "gigaam-v3-rnnt" in deployed


async def test_translations_rejected_with_explanation(client_ready):
    response = await client_ready.post("/v1/audio/translations", files=AUDIO)
    assert response.status_code == 400
    assert "не переводит" in response.json()["error"]["message"]
    assert response.json()["error"]["code"] == "translations_not_supported"


async def test_503_with_retry_after_when_engine_not_ready(client_stopped):
    response = await client_stopped.post("/v1/audio/transcriptions", files=AUDIO)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"]["code"] == "engine_not_ready"
    assert "Развернуть" in response.json()["error"]["message"]


async def test_missing_file_rejected(client_ready):
    response = await client_ready.post("/v1/audio/transcriptions", data={"model": "whisper-1"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_file"


async def test_upload_larger_than_limit_rejected(client_ready):
    client_ready.app.state.settings.max_upload_mb = 1
    big = b"0" * (2 * 1024 * 1024)
    response = await client_ready.post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", big, "audio/wav")}
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "file_too_large"
    assert "МБ" in response.json()["error"]["message"]


async def test_metrics_recorded_with_rtf_for_wav(client_ready):
    await client_ready.post("/v1/audio/transcriptions", files=AUDIO)
    snapshot = client_ready.app.state.metrics.snapshot()
    assert snapshot["count"] == 1
    assert snapshot["avg_rtf"] is not None  # 2 s WAV => duration known


@pytest.mark.parametrize("path", ["/v1/audio/transcriptions", "/api/test", "/v1/transcribe"])
async def test_upload_limit_while_receiving_chunked_body(client_ready, path):
    client_ready.app.state.settings.max_upload_mb = 1

    async def chunks():
        yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="sample.wav"\r\n\r\n'
        for _ in range(5):
            yield b"0" * (512 * 1024)
        pytest.fail("Получатель продолжил читать тело после отказа по размеру")

    response = await client_ready.post(path, content=chunks(), headers={
        "Content-Type": "multipart/form-data; boundary=test",
    })
    assert response.status_code == 413 and response.json()["error"]["code"] == "file_too_large"


async def test_local_engine_traffic_ignores_environment_proxy(client_ready, monkeypatch):
    # Заведомо несуществующая схема не открывает соединений даже при регрессии.
    monkeypatch.setenv("HTTP_PROXY", "invalid-scheme://no-proxy.invalid")
    monkeypatch.setenv("http_proxy", "invalid-scheme://no-proxy.invalid")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    process = client_ready.app.state.supervisor.process
    assert await process.health() and await process.reload()
    for path in ("/api/test", "/v1/audio/transcriptions", "/v1/transcribe", "/v1/admin/reload"):
        assert (await client_ready.post(path, files=AUDIO)).status_code == 200


async def test_native_query_excludes_console_key_and_preserves_lists(client_ready_with_key):
    client = client_ready_with_key
    response = await client.post("/v1/admin/reload", params=[
        ("api_key", "secret"), ("option", "one"), ("option", "two"),
    ])
    assert response.status_code == 200
    logs = client.app.state.bus.log_lines()
    assert any("/v1/admin/reload?option=one&option=two" in line for line in logs)
    assert all("api_key=" not in line for line in logs)


async def test_native_engine_endpoint_passthrough(client_ready):
    response = await client_ready.post("/v1/transcribe", files=AUDIO)
    assert response.status_code == 200
    assert response.json()["text"] == "привет мир"


async def engine_upload(client) -> dict:
    """What the engine actually received in the last transcription request."""
    import httpx

    base = client.app.state.settings.engine_base_url
    async with httpx.AsyncClient(timeout=5) as engine:
        response = await engine.get(f"{base}/debug/last_upload")
    return response.json()


async def test_browser_webm_reaches_the_engine_untouched(client_ready):
    """The engine demuxes WebM itself since 2.17.0, so the console must not rewrite it."""
    from conftest import opus_code3_packet, webm_opus_bytes

    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)] * 4)
    response = await client_ready.post(
        "/v1/audio/transcriptions",
        files={"file": ("dictation.webm", webm, "audio/webm")},
    )
    assert response.status_code == 200
    upload = await engine_upload(client_ready)
    assert upload["magic"] == b"\x1a\x45\xdf\xa3".decode("latin-1")  # the EBML header
    assert upload["filename"] == "dictation.webm"
    assert upload["content_type"] == "audio/webm"
    assert upload["size"] == len(webm)


async def test_webm_gets_an_rtf_the_engine_would_not_have_reported(client_ready):
    """`json` responses carry no duration, so the console reads it off the container."""
    from conftest import opus_code3_packet, webm_opus_bytes

    webm = webm_opus_bytes([opus_code3_packet([b"m" * 30] * 3)] * 50)
    await client_ready.post(
        "/v1/audio/transcriptions", files={"file": ("d.webm", webm, "audio/webm")}
    )
    snapshot = client_ready.app.state.metrics.snapshot()
    assert snapshot["avg_rtf"] is not None  # 150 frames x 20 ms => 3 s of audio


async def test_formats_the_engine_supports_reach_it_byte_for_byte(client_ready):
    payload = wav_bytes(seconds=2)
    response = await client_ready.post(
        "/v1/audio/transcriptions", files={"file": ("a.wav", payload, "audio/wav")}
    )
    assert response.status_code == 200
    upload = await engine_upload(client_ready)
    assert upload["magic"] == "RIFF"
    assert upload["filename"] == "a.wav"
    assert upload["size"] == len(payload)


# --------------------------------------------- словарь клиента, который не дошел
#
# Клиенты диктовки из мира Whisper кладут пользовательский словарь в `prompt` и
# на самохостный адрес шлют его вслепую. Движок - трансдьюсер, `prompt` он
# принимает и не читает, а 200 без единого признака этого означало бы, что
# словарь клиента молча не работает. Заголовок - единственный канал до чужого
# приложения, поэтому он проверяется как часть контракта.


async def test_prompt_is_answered_with_a_header_saying_it_was_ignored(client_ready):
    response = await client_ready.post(
        "/v1/audio/transcriptions", files=AUDIO, data={"prompt": "АйМоп, GigaAM"}
    )
    assert response.status_code == 200
    assert response.headers["x-ignored-fields"] == "prompt"
    assert response.headers["x-glossary-source"] == "server-hotwords"


async def test_request_without_a_prompt_carries_no_such_headers(client_ready):
    response = await client_ready.post("/v1/audio/transcriptions", files=AUDIO)
    assert response.status_code == 200
    assert "x-ignored-fields" not in response.headers


async def test_blank_prompt_is_not_worth_a_warning(client_ready):
    response = await client_ready.post(
        "/v1/audio/transcriptions", files=AUDIO, data={"prompt": "   "}
    )
    assert "x-ignored-fields" not in response.headers


async def test_ignored_prompt_is_logged_once_not_on_every_request(client_ready):
    for _ in range(3):
        await client_ready.post(
            "/v1/audio/transcriptions", files=AUDIO, data={"prompt": "АйМоп"}
        )
    bus = client_ready.app.state.bus
    warnings = [line for line in bus.log_lines(limit=200) if "client sent prompt" in line]
    assert len(warnings) == 1
