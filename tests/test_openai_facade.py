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
