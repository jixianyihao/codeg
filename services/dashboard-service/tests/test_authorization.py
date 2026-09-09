from fastapi.testclient import TestClient
from dashboard_service.app import create_app


def test_unauthenticated_api_fails_closed():
    with TestClient(create_app(test_configuration=True)) as client:
        assert client.get('/api/v1/me').status_code == 401


def test_jwt_rejects_wrong_audience_signature_type_and_expiry():
    import time
    import jwt
    import pytest
    from dashboard_service.auth import decode_service_token
    from dashboard_service.config import Config
    from dashboard_service.models import ApiError
    config = Config.testing()
    claims = dict(sub='account-a', iss=config.issuer, aud=config.audience,
                  token_type='service', ver=1, iat=int(time.time()), exp=int(time.time())+60)
    assert decode_service_token(jwt.encode(claims, config.jwt_secret, algorithm='HS256'), config)['sub'] == 'account-a'
    for change in [dict(aud='wrong'), dict(iss='wrong'), dict(token_type='human'), dict(exp=1), dict(iat=int(time.time())+1000)]:
        with pytest.raises(ApiError):
            decode_service_token(jwt.encode(claims | change, config.jwt_secret, algorithm='HS256'), config)
    with pytest.raises(ApiError):
        decode_service_token(jwt.encode(claims, b'wrong-key' * 8, algorithm='HS256'), config)
    with pytest.raises(ApiError):
        decode_service_token(jwt.encode(claims, config.jwt_secret, algorithm='HS384'), config)
    for field in claims:
        with pytest.raises(ApiError):
            decode_service_token(jwt.encode({k:v for k,v in claims.items() if k != field}, config.jwt_secret, algorithm='HS256'), config)


def test_configuration_rejects_sqlite_same_origin_and_non_https_verifier():
    import pytest
    from dataclasses import replace
    from dashboard_service.config import Config
    config = Config.testing()
    for change in [dict(database_url='sqlite://'), dict(content_origin=config.control_origin), dict(w3_verify_url='http://intranet/verify'), dict(jwt_secret=b'short')]:
        with pytest.raises(ValueError):
            replace(config, **change).validate()
