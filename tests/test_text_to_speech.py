import importlib.util
import io
import json
import pathlib
import sys
from types import ModuleType

import pytest
import responses


RUN_PATH = pathlib.Path(__file__).parents[1] / "text_to_speech" / "run.py"
BASE_CONFIG: dict[str, object] = {
    "api_key": "test-api-key",
    "default_voice": "Sebastian",
    "default_language": "en",
    "default_speed": "1.0",
    "default_reduce_silence": "false",
    "language_hints": "el,en",
    "tts_model": "tts-rt-v2",
    "stt_model": "stt-async-v6",
    "region": "us",
}


def load_run_module() -> ModuleType:
    """Load the tool the same way the plugin runner does, from its file path."""
    spec = importlib.util.spec_from_file_location("soniox_text_to_speech", RUN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_config(path: pathlib.Path, **overrides: object) -> None:
    path.write_text(json.dumps({**BASE_CONFIG, **overrides}))


def set_stdin(
    monkeypatch: pytest.MonkeyPatch, parameters: dict[str, str | float | bool]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(parameters)))


@pytest.fixture
def plugin(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, pathlib.Path]:
    module = load_run_module()
    config_path = tmp_path / "config.json"
    write_config(config_path)
    monkeypatch.setattr(module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("STAVROBOT_TEMP_DIR", str(tmp_path / "output"))
    return module, config_path


@pytest.mark.parametrize(
    ("region", "parameters", "expected_url", "expected_voice", "expected_language"),
    [
        (
            "us",
            {"text": "Hello"},
            "https://tts-rt.soniox.com/tts",
            "Sebastian",
            "en",
        ),
        (
            "eu",
            {"text": "Γεια σου", "voice": "Katerina", "language": "el"},
            "https://tts-rt.eu.soniox.com/tts",
            "Katerina",
            "el",
        ),
    ],
)
@responses.activate
def test_request_uses_region_host_and_configured_defaults(
    plugin: tuple[ModuleType, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    region: str,
    parameters: dict[str, str],
    expected_url: str,
    expected_voice: str,
    expected_language: str,
) -> None:
    module, config_path = plugin
    write_config(config_path, region=region)
    set_stdin(monkeypatch, parameters)
    responses.post(expected_url, body=b"audio-bytes")

    module.main()

    assert len(responses.calls) == 1
    request = responses.calls[0].request
    assert request.url == expected_url
    assert request.headers["Authorization"] == "Bearer test-api-key"
    assert json.loads(request.body) == {
        "model": "tts-rt-v2",
        "language": expected_language,
        "voice": expected_voice,
        "speed": 1.0,
        "reduce_silence": False,
        "audio_format": "mp3",
        "text": parameters["text"],
    }
    assert json.loads(capsys.readouterr().out) == {"file": "output.mp3"}
    assert (tmp_path / "output" / "output.mp3").read_bytes() == b"audio-bytes"


@pytest.mark.parametrize(
    ("config_values", "parameters", "expected_speed", "expected_reduce_silence"),
    [
        (
            {"default_speed": "1.0", "default_reduce_silence": "false"},
            {"text": "Hello"},
            1.0,
            False,
        ),
        (
            {"default_speed": 0.8, "default_reduce_silence": True},
            {"text": "Hello"},
            0.8,
            True,
        ),
        (
            {"default_speed": "0.8", "default_reduce_silence": "true"},
            {"text": "Hello", "speed": 1.2, "reduce_silence": False},
            1.2,
            False,
        ),
    ],
)
@responses.activate
def test_speed_and_reduce_silence_come_from_config_or_request(
    plugin: tuple[ModuleType, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    config_values: dict[str, object],
    parameters: dict[str, str | float | bool],
    expected_speed: float,
    expected_reduce_silence: bool,
) -> None:
    module, config_path = plugin
    write_config(config_path, **config_values)
    set_stdin(monkeypatch, parameters)
    responses.post("https://tts-rt.soniox.com/tts", body=b"audio-bytes")

    module.main()

    body = json.loads(responses.calls[0].request.body)
    assert body["speed"] == expected_speed
    assert body["reduce_silence"] is expected_reduce_silence


@responses.activate
def test_invalid_default_reduce_silence_fails_without_a_request(
    plugin: tuple[ModuleType, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, config_path = plugin
    write_config(config_path, default_reduce_silence="yes")
    set_stdin(monkeypatch, {"text": "Hello"})

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 1
    assert "Invalid default_reduce_silence: 'yes'" in capsys.readouterr().err
    assert len(responses.calls) == 0


@pytest.mark.parametrize(
    ("status_code", "body", "expected_stderr"),
    [
        (
            400,
            json.dumps(
                {
                    "error_type": "invalid_request",
                    "error_message": "Text is too long.",
                    "request_id": "request-tts",
                }
            ),
            "error_type: invalid_request; message: Text is too long.; "
            "request_id: request-tts",
        ),
        (
            400,
            json.dumps(
                {
                    "error_type": "invalid_request",
                    "message": "Bad request.",
                    "request_id": "request-rest",
                }
            ),
            "error_type: invalid_request; message: Bad request.; "
            "request_id: request-rest",
        ),
        (
            502,
            "Bad gateway",
            "error_type: api_error; message: Bad gateway; request_id: unknown",
        ),
    ],
)
@responses.activate
def test_api_errors_become_one_stderr_line(
    plugin: tuple[ModuleType, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status_code: int,
    body: str,
    expected_stderr: str,
) -> None:
    module, _ = plugin
    set_stdin(monkeypatch, {"text": "Hello"})
    responses.post("https://tts-rt.soniox.com/tts", status=status_code, body=body)

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 1
    assert capsys.readouterr().err.strip() == expected_stderr


@responses.activate
def test_authentication_error_uses_plain_wording(
    plugin: tuple[ModuleType, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _ = plugin
    set_stdin(monkeypatch, {"text": "Hello"})
    responses.post(
        "https://tts-rt.soniox.com/tts",
        status=401,
        json={
            "error_type": "unauthenticated",
            "message": "Incorrect API key provided. You can get an API key at the console.",
            "request_id": "request-401",
        },
    )

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    stderr = capsys.readouterr().err
    assert exit_info.value.code == 1
    assert stderr.strip() == (
        "error_type: unauthenticated; message: invalid Soniox API key; "
        "request_id: request-401"
    )
    assert "test-api-key" not in stderr
    assert "Incorrect API key" not in stderr
    assert not (tmp_path / "output" / "output.mp3").exists()


@responses.activate
def test_unknown_region_fails_without_a_request(
    plugin: tuple[ModuleType, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, config_path = plugin
    write_config(config_path, region="mars")
    set_stdin(monkeypatch, {"text": "Hello"})

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 1
    assert "Unknown region: mars" in capsys.readouterr().err
    assert len(responses.calls) == 0
