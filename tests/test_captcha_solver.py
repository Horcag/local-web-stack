import asyncio
import base64
import json
import traceback

import httpx
import pytest

from service.captcha_solver import (
    CAPTCHA_PROMPT,
    DEFAULT_MODEL,
    MAX_IMAGE_BYTES,
    PNG_SIGNATURE,
    CaptchaSolver,
    CaptchaSolverError,
)

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
)
SECRET = "private-api-key-never-disclose"


@pytest.fixture
def gateway(monkeypatch):
    requests = []
    options = []
    response = {"choices": [{"message": {"content": "1234"}}]}
    state = {"response": response, "status": 200, "error": None}
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        if state["error"]:
            raise state["error"]
        if isinstance(state["response"], bytes):
            return httpx.Response(state["status"], content=state["response"])
        return httpx.Response(state["status"], json=state["response"])

    def client(**kwargs):
        options.append(kwargs)
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state, requests, options


def solver(**kwargs):
    return CaptchaSolver(enabled=True, base_url="http://gateway.test/v1/", **kwargs)


def solve(subject, image=PNG):
    return asyncio.run(subject.solve(image))


def assert_reason(subject, reason, image=PNG):
    with pytest.raises(CaptchaSolverError) as exc:
        solve(subject, image)
    assert exc.value.reason == reason
    assert str(exc.value) == reason
    assert SECRET not in repr(exc.value)
    assert base64.b64encode(PNG).decode() not in repr(exc.value)
    return exc.value


def test_default_disabled_and_empty_image_never_create_client(gateway):
    _, requests, options = gateway
    assert solve(CaptchaSolver()) is None
    assert solve(solver(), b"") is None
    assert solve(CaptchaSolver(enabled=True), b"") is None
    assert not requests
    assert not options


def test_request_reuses_work_ocr_wire_contract(gateway):
    _, requests, options = gateway
    assert solve(solver(api_key=SECRET)) == "1234"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "http://gateway.test/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert request.headers["Content-Type"] == "application/json"
    body = json.loads(request.content)
    assert body == {
        "model": DEFAULT_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CAPTCHA_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + base64.b64encode(PNG).decode()
                        },
                    },
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 1000,
    }
    assert options == [{"timeout": 15.0, "trust_env": False}]


def test_two_images_preserve_main_then_reference_wire_order(gateway):
    _, requests, _ = gateway
    reference = PNG + b"reference"
    assert asyncio.run(solver().complete_images([PNG, reference], "ordered objects")) == "1234"
    parts = json.loads(requests[0].content)["messages"][0]["content"]
    assert json.loads(requests[0].content)["max_tokens"] == 4096
    assert parts[0] == {"type": "text", "text": "ordered objects"}
    assert [base64.b64decode(p["image_url"]["url"].split(",", 1)[1]) for p in parts[1:]] == [
        PNG,
        reference,
    ]


@pytest.mark.parametrize("images", [[], [PNG] * 3, [PNG, b"bad"], [PNG, PNG * 20000]])
def test_image_pair_limits_reject_before_gateway(images, gateway):
    _, requests, _ = gateway
    with pytest.raises(CaptchaSolverError, match="invalid_image"):
        asyncio.run(solver().complete_images(images, "ordered objects"))
    assert not requests


def test_optional_auth_and_custom_model(gateway):
    _, requests, _ = gateway
    assert solve(solver(model="chosen-model")) == "1234"
    assert "Authorization" not in requests[0].headers
    assert json.loads(requests[0].content)["model"] == "chosen-model"


def test_env_key_file_and_bounded_timeout(monkeypatch, tmp_path, gateway):
    _, requests, options = gateway
    keyfile = tmp_path / "gateway-key"
    keyfile.write_text(SECRET + "\n")
    monkeypatch.delenv("LOCAL_WEB_CAPTCHA_API_KEY", raising=False)
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_ENABLED", "true")
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_BASE_URL", "http://gateway.test/v1")
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_API_KEY_FILE", str(keyfile))
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_TIMEOUT_SECONDS", "120")
    subject = CaptchaSolver.from_env()
    assert subject.enabled and subject.configured
    assert solve(subject) == "1234"
    assert requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert options[0]["timeout"] == 30.0
    assert SECRET not in repr(subject)
    assert str(keyfile) not in repr(subject)


def test_direct_key_takes_precedence_over_key_file(tmp_path, gateway):
    _, requests, _ = gateway
    assert solve(solver(api_key=SECRET, api_key_file=str(tmp_path / "missing"))) == "1234"
    assert requests[0].headers["Authorization"] == f"Bearer {SECRET}"


@pytest.mark.parametrize("value", ["garbage", "nan", "inf", "0", "-1"])
def test_invalid_env_timeout_uses_bounded_default(value, monkeypatch, gateway):
    _, _, options = gateway
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_ENABLED", "1")
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_BASE_URL", "http://gateway.test")
    monkeypatch.setenv("LOCAL_WEB_CAPTCHA_TIMEOUT_SECONDS", value)
    assert solve(CaptchaSolver.from_env()) == "1234"
    assert options[0]["timeout"] == 15.0


def test_env_default_has_no_automatic_gateway(monkeypatch, gateway):
    _, requests, options = gateway
    for key in ("ENABLED", "BASE_URL", "API_KEY", "API_KEY_FILE", "MODEL", "TIMEOUT_SECONDS"):
        monkeypatch.delenv("LOCAL_WEB_CAPTCHA_" + key, raising=False)
    subject = CaptchaSolver.from_env()
    assert not subject.enabled and not subject.configured
    assert solve(subject) is None
    assert not requests and not options


@pytest.mark.parametrize(
    "base_url",
    [None, "", "file:///private", "http://", "https://user:secret@gateway.test", "http://x/?key=s"],
)
def test_missing_or_invalid_gateway_rejected_before_network(base_url, gateway):
    _, requests, options = gateway
    subject = CaptchaSolver(enabled=True, base_url=base_url)
    assert not subject.configured
    assert_reason(subject, "unconfigured")
    assert not requests and not options


def test_missing_key_file_rejected_before_network(tmp_path, gateway):
    _, requests, options = gateway
    subject = solver(api_key_file=str(tmp_path / "missing"))
    assert not subject.configured
    assert_reason(subject, "unconfigured")
    assert not requests and not options


@pytest.mark.parametrize("image", [b"not png", PNG_SIGNATURE + b"x" * MAX_IMAGE_BYTES])
def test_invalid_image_rejected_before_network(image, gateway):
    _, requests, options = gateway
    assert_reason(solver(), "invalid_image", image)
    assert not requests and not options


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1234", "1234"),
        ("1 " * 80, "1" * 80),
        ('  Ответ: "1 2 3 4"  ', "1234"),
        ("«текст: два слова»", "два слова"),
        ("капча: кот", "кот"),
        ("результат: Пример", "Пример"),
        ("two   words", "two words"),
        ("", None),
        ("   ", None),
    ],
)
def test_answer_normalization(raw, expected, gateway):
    state, _, _ = gateway
    state["response"] = {"choices": [{"message": {"content": raw}}]}
    assert solve(solver()) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "a" * 101,
        "1234\nexplanation",
        "123\x00",
        "12\t34",
        "123\u200b4",
        "```1234```",
        "https://example.com",
        "<script>1234</script>",
        "The answer is 1234",
        "распознанный текст 1234",
        "на изображении 1234",
    ],
)
def test_unsafe_or_explanatory_answer_rejected(raw, gateway):
    state, _, _ = gateway
    state["response"] = {"choices": [{"message": {"content": raw}}]}
    assert_reason(solver(), "invalid_answer")


@pytest.mark.parametrize(
    "payload",
    [
        b"not JSON",
        None,
        [],
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": ["1234"]}}]},
    ],
)
def test_malformed_or_missing_content_is_sanitized(payload, gateway):
    state, _, _ = gateway
    state["response"] = payload
    assert_reason(solver(), "gateway_invalid_response")


@pytest.mark.parametrize("status", [400, 401, 429, 500])
def test_http_error_is_sanitized_without_retry(status, gateway):
    state, requests, _ = gateway
    state.update(status=status, response={"error": SECRET})
    error = assert_reason(solver(api_key=SECRET), "gateway_http_error")
    assert len(requests) == 1
    assert error.__suppress_context__


@pytest.mark.parametrize(
    "error,reason",
    [
        (httpx.ReadTimeout(SECRET), "gateway_timeout"),
        (httpx.ConnectError(SECRET), "gateway_http_error"),
    ],
)
def test_gateway_exceptions_hide_secret_and_have_no_retry(error, reason, gateway):
    state, requests, _ = gateway
    state["error"] = error
    sanitized = assert_reason(solver(api_key=SECRET), reason)
    assert SECRET not in "".join(traceback.format_exception(sanitized))
    assert len(requests) == 1


@pytest.mark.parametrize("key", ["secret\nheader", "nonasciiключ"])
def test_invalid_auth_configuration_is_sanitized_before_network(key, gateway):
    _, requests, options = gateway
    assert_reason(solver(api_key=key), "unconfigured")
    assert not requests and not options


def test_exact_image_size_limit_is_accepted(gateway):
    image = PNG_SIGNATURE + b"x" * (MAX_IMAGE_BYTES - len(PNG_SIGNATURE))
    assert solve(solver(), image) == "1234"


@pytest.mark.parametrize("raw", ['  {"points": [[10, 20], [30, 40]]}\n', "x" * 4000, ""])
def test_complete_image_returns_bounded_raw_text_without_ocr_normalization(raw, gateway):
    state, requests, options = gateway
    state["response"] = {"choices": [{"message": {"content": raw}}]}
    prompt = "Return JSON points for the supplied puzzle."
    assert asyncio.run(solver(api_key=SECRET).complete_image(PNG, prompt)) == raw
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    assert body["messages"][0]["content"][0] == {"type": "text", "text": prompt}
    assert body["model"] == DEFAULT_MODEL
    assert body["max_tokens"] == 1000 and body["temperature"] == 0.0
    assert requests[0].headers["Authorization"] == f"Bearer {SECRET}"
    assert options == [{"timeout": 15.0, "trust_env": False}]


def test_complete_image_rejects_oversized_raw_output(gateway):
    state, requests, _ = gateway
    state["response"] = {"choices": [{"message": {"content": "x" * 4001}}]}
    with pytest.raises(CaptchaSolverError) as exc:
        asyncio.run(solver().complete_image(PNG, "Return points"))
    assert exc.value.reason == "gateway_invalid_response"
    assert len(requests) == 1


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 2001])
def test_complete_image_rejects_invalid_prompt_before_network(prompt, gateway):
    _, requests, options = gateway
    with pytest.raises(CaptchaSolverError) as exc:
        asyncio.run(solver().complete_image(PNG, prompt))
    assert exc.value.reason == "invalid_prompt"
    assert not requests and not options


def test_complete_image_disabled_or_empty_returns_none_without_network(gateway):
    _, requests, options = gateway
    assert asyncio.run(CaptchaSolver().complete_image(PNG, "Return points")) is None
    assert asyncio.run(solver().complete_image(b"", "Return points")) is None
    assert not requests and not options


def test_complete_image_accepts_exact_prompt_bound(gateway):
    assert asyncio.run(solver().complete_image(PNG, "x" * 2000)) == "1234"
