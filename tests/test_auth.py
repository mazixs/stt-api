async def test_open_when_no_key_configured(client_ready):
    assert (await client_ready.get("/v1/models")).status_code == 200


async def test_401_without_bearer_when_key_set(client_ready_with_key):
    response = await client_ready_with_key.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.json()["error"]["type"] == "authentication_error"


async def test_401_with_wrong_key(client_ready_with_key):
    response = await client_ready_with_key.get(
        "/v1/models", headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401


async def test_200_with_correct_bearer(client_ready_with_key):
    response = await client_ready_with_key.get(
        "/v1/models", headers={"Authorization": "Bearer secret"}
    )
    assert response.status_code == 200


async def test_health_never_protected(client_ready_with_key):
    assert (await client_ready_with_key.get("/health")).status_code == 200


async def test_docs_open_when_no_key_configured(client_ready):
    """Пустой API_KEY — сервис открыт целиком, и схема тоже."""
    response = await client_ready.get("/api/docs")
    assert response.status_code == 200
    assert "/api/openapi.json" in response.text
    assert (await client_ready.get("/api/openapi.json")).status_code == 200


async def test_docs_closed_when_key_set(client_ready_with_key):
    assert (await client_ready_with_key.get("/api/docs")).status_code == 401
    assert (await client_ready_with_key.get("/api/openapi.json")).status_code == 401


async def test_docs_open_with_key_in_query(client_ready_with_key):
    response = await client_ready_with_key.get("/api/docs?api_key=secret")
    assert response.status_code == 200
    # Swagger грузит схему сам и заголовок послать не может, поэтому ключ, пришедший
    # запросом страницы, должен уехать в ссылку на схему.
    assert "/api/openapi.json?api_key=secret" in response.text

    schema = await client_ready_with_key.get("/api/openapi.json?api_key=secret")
    assert schema.status_code == 200
    assert "/api/deploy" in schema.json()["paths"]


async def test_docs_have_no_open_default_paths(client_ready_with_key):
    """Штатные /docs и /openapi.json выключены, иначе схема утекала бы мимо ключа."""
    assert (await client_ready_with_key.get("/docs")).status_code == 404
    assert (await client_ready_with_key.get("/openapi.json")).status_code == 404
