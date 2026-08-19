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
