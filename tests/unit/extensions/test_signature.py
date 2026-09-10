from extensions import signature


def test_build_signature_always_includes_request_param():
    sig = signature.build_signature({"path": "/ping", "query_params": []})
    assert list(sig.parameters.keys()) == ["request"]
    assert sig.parameters["request"].annotation is signature.Request


def test_build_signature_adds_one_path_param_per_placeholder():
    route = {"path": "/devices/{device_name}/status/{status_id}", "query_params": []}
    sig = signature.build_signature(route)
    names = list(sig.parameters.keys())
    assert names == ["request", "device_name", "status_id"]
    for name in ("device_name", "status_id"):
        param = sig.parameters[name]
        assert param.kind is param.KEYWORD_ONLY
        assert param.annotation is str


def test_build_signature_adds_required_and_optional_query_params_with_right_types():
    route = {
        "path": "/things",
        "query_params": [
            {"name": "count", "type": "integer", "required": True},
            {"name": "verbose", "type": "boolean", "required": False},
        ],
    }
    sig = signature.build_signature(route)
    count_param = sig.parameters["count"]
    verbose_param = sig.parameters["verbose"]
    assert count_param.annotation is int
    assert verbose_param.annotation == signature.Optional[bool]


def test_build_signature_skips_query_param_name_colliding_with_path_param():
    route = {
        "path": "/devices/{device_name}",
        "query_params": [{"name": "device_name", "type": "string", "required": True}],
    }
    sig = signature.build_signature(route)
    # only one 'device_name' parameter — no duplicate-name ValueError, path param wins
    assert list(sig.parameters.keys()) == ["request", "device_name"]


def test_build_signature_with_no_path_or_query_params_is_request_only():
    sig = signature.build_signature({"path": "/health", "query_params": []})
    assert list(sig.parameters.keys()) == ["request"]
