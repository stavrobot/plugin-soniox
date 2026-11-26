import importlib.util
import io
import json
import pathlib
import sys
from types import ModuleType

import pytest
import requests
import responses


RUN_PATH = pathlib.Path(__file__).parents[1] / "speech_to_text" / "run.py"
BASE_CONFIG: dict[str, str] = {
    "api_key": "test-api-key",
    "default_voice": "Sebastian",
    "default_language": "en",
    "language_hints": "el,en",
    "tts_model": "tts-rt-v2",
    "stt_model": "stt-async-v6",
    "region": "us",
}


def load_run_module() -> ModuleType:
    """Load the tool the same way the plugin runner does, from its file path."""
    spec = importlib.util.spec_from_file_location("soniox_speech_to_text", RUN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_config(path: pathlib.Path, **overrides: str) -> None:
    path.write_text(json.dumps({**BASE_CONFIG, **overrides}))


def set_stdin(monkeypatch: pytest.MonkeyPatch, parameters: dict[str, str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(parameters)))


@pytest.fixture
def plugin(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, pathlib.Path, pathlib.Path]:
    module = load_run_module()
    config_path = tmp_path / "config.json"
    write_config(config_path)
    monkeypatch.setattr(module, "CONFIG_PATH", config_path)
    audio_path = tmp_path / "audio.ogg"
    audio_path.write_bytes(b"audio-bytes")
    return module, config_path, audio_path


def set_up_happy_path(base_url: str) -> None:
    responses.post(f"{base_url}/files", status=201, json={"id": "file-1"})
    responses.post(
        f"{base_url}/transcriptions",
        status=201,
        json={"id": "transcription-1", "status": "queued"},
    )
    responses.get(
        f"{base_url}/transcriptions/transcription-1",
        json={"id": "transcription-1", "status": "processing"},
    )
    responses.get(
        f"{base_url}/transcriptions/transcription-1",
        json={"id": "transcription-1", "status": "completed"},
    )
    responses.get(
        f"{base_url}/transcriptions/transcription-1/transcript",
        json={"id": "transcription-1", "text": "Hello", "tokens": []},
    )
    responses.delete(f"{base_url}/transcriptions/transcription-1", status=204)
    responses.delete(f"{base_url}/files/file-1", status=204)


@pytest.mark.parametrize(
    ("region", "base_url"),
    [
        ("us", "https://api.soniox.com/v1"),
        ("eu", "https://api.eu.soniox.com/v1"),
    ],
)
@responses.activate
def test_happy_path_uploads_polls_transcribes_and_cleans_up(
    plugin: tuple[ModuleType, pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    region: str,
    base_url: str,
) -> None:
    module, config_path, audio_path = plugin
    write_config(config_path, region=region)
    set_stdin(monkeypatch, {"audio": str(audio_path)})
    # The poll interval exists to be gentle on the API, not to be tested here.
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    set_up_happy_path(base_url)

    module.main()

    calls = [(call.request.method, call.request.url) for call in responses.calls]
    assert calls == [
        ("POST", f"{base_url}/files"),
        ("POST", f"{base_url}/transcriptions"),
        ("GET", f"{base_url}/transcriptions/transcription-1"),
        ("GET", f"{base_url}/transcriptions/transcription-1"),
        ("GET", f"{base_url}/transcriptions/transcription-1/transcript"),
        ("DELETE", f"{base_url}/transcriptions/transcription-1"),
        ("DELETE", f"{base_url}/files/file-1"),
    ]

    upload_request = responses.calls[0].request
    assert b'name="file"' in upload_request.body
    assert b"audio-bytes" in upload_request.body
    assert upload_request.headers["Authorization"] == "Bearer test-api-key"

    create_request = responses.calls[1].request
    assert json.loads(create_request.body) == {
        "model": "stt-async-v6",
        "file_id": "file-1",
        "language_hints": ["el", "en"],
    }
    assert json.loads(capsys.readouterr().out) == {"text": "Hello"}


@pytest.mark.parametrize("status", ["error", "failed"])
@responses.activate
def test_failed_transcription_reports_error_and_cleans_up(
    plugin: tuple[ModuleType, pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
) -> None:
    module, _, audio_path = plugin
    set_stdin(monkeypatch, {"audio": str(audio_path)})
    base_url = "https://api.soniox.com/v1"
    responses.post(f"{base_url}/files", status=201, json={"id": "file-1"})
    responses.post(
        f"{base_url}/transcriptions",
        status=201,
        json={"id": "transcription-1", "status": "queued"},
    )
    responses.get(
        f"{base_url}/transcriptions/transcription-1",
        json={
            "id": "transcription-1",
            "status": status,
            "error_type": "invalid_audio_file",
            "error_message": "The audio could not be decoded.",
        },
    )
    responses.delete(f"{base_url}/transcriptions/transcription-1", status=204)
    responses.delete(f"{base_url}/files/file-1", status=204)

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "error_type: invalid_audio_file" in stderr
    assert "The audio could not be decoded." in stderr
    calls = [(call.request.method, call.request.url) for call in responses.calls]
    assert calls[-2:] == [
        ("DELETE", f"{base_url}/transcriptions/transcription-1"),
        ("DELETE", f"{base_url}/files/file-1"),
    ]
    assert not any(call.request.url.endswith("/transcript") for call in responses.calls)


def always_processing(
    request: requests.PreparedRequest,
) -> tuple[int, dict[str, str], str]:
    return 200, {}, json.dumps({"status": "processing"})


@responses.activate
def test_timeout_reports_long_audio_and_cleans_up(
    plugin: tuple[ModuleType, pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, audio_path = plugin
    set_stdin(monkeypatch, {"audio": str(audio_path)})
    monkeypatch.setattr(module, "DEADLINE_SECONDS", 3)

    # Drive the clock from the fake sleep so the poll loop can hit its deadline
    # without waiting in real time.
    current_time = 0.0

    def monotonic() -> float:
        return current_time

    def sleep(seconds: float) -> None:
        nonlocal current_time
        current_time += seconds

    monkeypatch.setattr(module.time, "monotonic", monotonic)
    monkeypatch.setattr(module.time, "sleep", sleep)

    base_url = "https://api.soniox.com/v1"
    responses.post(f"{base_url}/files", status=201, json={"id": "file-1"})
    responses.post(
        f"{base_url}/transcriptions",
        status=201,
        json={"id": "transcription-1", "status": "queued"},
    )
    responses.add_callback(
        responses.GET,
        f"{base_url}/transcriptions/transcription-1",
        callback=always_processing,
        content_type="application/json",
    )
    # Soniox refuses to delete a transcription that is still processing, which is
    # exactly the state a timed-out run leaves it in.
    responses.delete(
        f"{base_url}/transcriptions/transcription-1",
        status=409,
        json={
            "error_type": "transcription_invalid_state",
            "message": "This transcription is currently being processed and cannot be deleted yet.",
            "request_id": "request-processing",
        },
    )
    responses.delete(f"{base_url}/files/file-1", status=204)

    with pytest.raises(SystemExit) as exit_info:
        module.main()

    assert exit_info.value.code == 1
    stderr = capsys.readouterr().err
    assert "too long to transcribe" in stderr
    assert "transcription_invalid_state" in stderr
    calls = [(call.request.method, call.request.url) for call in responses.calls]
    assert ("DELETE", f"{base_url}/files/file-1") in calls
    assert calls[-2:] == [
        ("DELETE", f"{base_url}/transcriptions/transcription-1"),
        ("DELETE", f"{base_url}/files/file-1"),
    ]


@responses.activate
def test_cleanup_failure_does_not_hide_success(
    plugin: tuple[ModuleType, pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, audio_path = plugin
    set_stdin(monkeypatch, {"audio": str(audio_path)})
    base_url = "https://api.soniox.com/v1"
    responses.post(f"{base_url}/files", status=201, json={"id": "file-1"})
    responses.post(
        f"{base_url}/transcriptions",
        status=201,
        json={"id": "transcription-1", "status": "queued"},
    )
    responses.get(
        f"{base_url}/transcriptions/transcription-1",
        json={"id": "transcription-1", "status": "completed"},
    )
    responses.get(
        f"{base_url}/transcriptions/transcription-1/transcript",
        json={"id": "transcription-1", "text": "Hello", "tokens": []},
    )
    cleanup_error = {
        "error_type": "internal_error",
        "message": "Server error.",
        "request_id": "request-cleanup",
    }
    responses.delete(
        f"{base_url}/transcriptions/transcription-1",
        status=500,
        json=cleanup_error,
    )
    responses.delete(f"{base_url}/files/file-1", status=500, json=cleanup_error)

    module.main()

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"text": "Hello"}
    assert "Cleanup failed" in captured.err
    assert "internal_error" in captured.err
    assert len(
        [call for call in responses.calls if call.request.method == "DELETE"]
    ) == 2


@responses.activate
def test_authentication_error_uses_plain_wording(
    plugin: tuple[ModuleType, pathlib.Path, pathlib.Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module, _, audio_path = plugin
    set_stdin(monkeypatch, {"audio": str(audio_path)})
    responses.post(
        "https://api.soniox.com/v1/files",
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
    assert len(responses.calls) == 1
