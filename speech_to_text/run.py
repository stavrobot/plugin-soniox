#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///

import json
import pathlib
import sys
import time
from typing import NoReturn, TypedDict

import requests


KNOWN_PARAMETERS: set[str] = {"audio"}
# The plugin runner kills a tool at 30 seconds, counted from process start, which
# includes uv startup. Stopping at 22 seconds leaves room for that and for the two
# cleanup deletes.
DEADLINE_SECONDS = 22
POLL_INTERVAL_SECONDS = 1.0
# Cleanup runs inside the few seconds left after the deadline, so it cannot use the
# same long timeout as the main requests.
CLEANUP_TIMEOUT_SECONDS = 2
# Requests rejects a zero or negative timeout. A small positive one lets an already
# expired deadline surface as a request timeout instead of an argument error.
MINIMUM_REQUEST_TIMEOUT_SECONDS = 0.5
TIMEOUT_MESSAGE = (
    "Transcription timed out: the audio is too long to transcribe within the "
    "tool's time limit."
)
# Region "us" is the default and has no regional prefix in its hostname, while the
# others embed the region, so the mapping cannot be derived uniformly.
API_HOSTS: dict[str, str] = {
    "us": "api.soniox.com",
    "eu": "api.eu.soniox.com",
    "jp": "api.jp.soniox.com",
    "in": "api.in.soniox.com",
}
# The tool runs inside a Docker container where the working directory is
# speech_to_text/, so the plugin config lives one level up.
CONFIG_PATH = pathlib.Path("../config.json")


class Config(TypedDict):
    api_key: str
    default_voice: str
    default_language: str
    language_hints: str
    tts_model: str
    stt_model: str
    region: str


def read_parameters() -> dict[str, str]:
    raw: dict[str, str] = json.load(sys.stdin)
    unknown = set(raw.keys()) - KNOWN_PARAMETERS
    if unknown:
        print(f"Unknown parameters: {sorted(unknown)}", file=sys.stderr)
        sys.exit(1)
    if "audio" not in raw:
        print("Missing required parameter: audio", file=sys.stderr)
        sys.exit(1)
    return raw


def read_config() -> Config:
    config: Config = json.loads(CONFIG_PATH.read_text())
    # A pasted key often carries a trailing newline, which makes requests raise
    # InvalidHeader and include the full header value, leaking the key to stderr.
    config["api_key"] = config["api_key"].strip()
    return config


def parse_language_hints(language_hints: str) -> list[str]:
    return [hint.strip() for hint in language_hints.split(",") if hint.strip()]


def authorization_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def remaining_request_timeout(deadline: float) -> float:
    """Keep every request inside the overall deadline, leaving time for cleanup."""
    return max(deadline - time.monotonic(), MINIMUM_REQUEST_TIMEOUT_SECONDS)


def collapse_whitespace(text: str) -> str:
    """Collapse whitespace so a multi-line error body still fits on one stderr line."""
    return " ".join(text.split())


def format_api_error(response: requests.Response) -> str:
    """Format a Soniox error as the single stderr line the plugin runner reports.

    The TTS REST API calls the human-readable text error_message, while the main
    REST API calls it message, so both are accepted. Non-JSON bodies, such as a
    proxy error page, fall back to the raw text.
    """
    try:
        body: dict[str, str] | None = response.json()
    except ValueError:
        body = None

    if body is None:
        error_type = "api_error"
        message = collapse_whitespace(response.text) or "unknown error"
        request_id = "unknown"
    else:
        error_type = body.get("error_type", "api_error")
        message = (
            body.get("error_message")
            or body.get("message")
            or collapse_whitespace(response.text)
            or "unknown error"
        )
        request_id = body.get("request_id", "unknown")

    # Soniox's own 401 text points at the console and varies by API. The assistant
    # only needs to know that the configured key is unusable, and the raw body might
    # echo request details, so replace it with a stable message.
    if response.status_code == 401:
        error_type = "unauthenticated"
        message = "invalid Soniox API key"

    return (
        f"error_type: {error_type}; "
        f"message: {collapse_whitespace(message)}; "
        f"request_id: {request_id}"
    )


def fail_with_api_error(response: requests.Response) -> NoReturn:
    print(format_api_error(response), file=sys.stderr)
    sys.exit(1)


def fail_with_message(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(1)


def upload_file(
    base_url: str, api_key: str, audio_path: str, deadline: float
) -> str:
    # Soniox detects the audio format from the file contents, so the original
    # filename and extension are passed through untouched.
    with open(audio_path, "rb") as audio_file:
        response = requests.post(
            f"{base_url}/files",
            headers=authorization_headers(api_key),
            files={"file": audio_file},
            timeout=remaining_request_timeout(deadline),
        )
    if not response.ok:
        fail_with_api_error(response)
    return response.json()["id"]


def create_transcription(
    base_url: str,
    api_key: str,
    file_id: str,
    model: str,
    language_hints: list[str],
    deadline: float,
) -> str:
    response = requests.post(
        f"{base_url}/transcriptions",
        headers=authorization_headers(api_key),
        json={
            "model": model,
            "file_id": file_id,
            "language_hints": language_hints,
        },
        timeout=remaining_request_timeout(deadline),
    )
    if not response.ok:
        fail_with_api_error(response)
    return response.json()["id"]


def fetch_transcript(
    base_url: str, api_key: str, transcription_id: str, deadline: float
) -> str:
    response = requests.get(
        f"{base_url}/transcriptions/{transcription_id}/transcript",
        headers=authorization_headers(api_key),
        timeout=remaining_request_timeout(deadline),
    )
    if not response.ok:
        fail_with_api_error(response)
    return response.json()["text"]


def wait_for_transcription(
    base_url: str, api_key: str, transcription_id: str, deadline: float
) -> str:
    while True:
        if time.monotonic() >= deadline:
            fail_with_message(TIMEOUT_MESSAGE)
        response = requests.get(
            f"{base_url}/transcriptions/{transcription_id}",
            headers=authorization_headers(api_key),
            timeout=remaining_request_timeout(deadline),
        )
        if not response.ok:
            fail_with_api_error(response)
        transcription = response.json()
        status = transcription["status"]
        if status == "completed":
            return fetch_transcript(
                base_url=base_url,
                api_key=api_key,
                transcription_id=transcription_id,
                deadline=deadline,
            )
        # The transcription schema lists "error" while the errors reference shows
        # "failed", so both are treated as terminal failures.
        if status in {"error", "failed"}:
            error_type = transcription.get("error_type") or "unknown_error"
            error_message = transcription.get("error_message") or "no error message"
            fail_with_message(
                f"error_type: {error_type}; message: {error_message}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail_with_message(TIMEOUT_MESSAGE)
        time.sleep(min(POLL_INTERVAL_SECONDS, remaining))


def delete_resource(url: str, api_key: str) -> None:
    try:
        response = requests.delete(
            url,
            headers=authorization_headers(api_key),
            timeout=CLEANUP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        # A cleanup failure must never replace the original error or a successful
        # transcription, so it is reported and never re-raised.
        print(f"Cleanup request failed for {url}: {error}", file=sys.stderr)
        return
    if not response.ok:
        print(f"Cleanup failed for {url}: {format_api_error(response)}", file=sys.stderr)


def clean_up(
    base_url: str, api_key: str, transcription_id: str | None, file_id: str
) -> None:
    # Soniox rejects deleting a queued or processing transcription with 409, so on
    # the timeout path the transcription survives (Soniox removes it after 30 days).
    # The file can be deleted at any time, which also discards the uploaded audio.
    # A background sweep of leftovers was rejected: live tests transcribed a 67 s
    # clip in about 2 s, so timeouts should be rare.
    if transcription_id is not None:
        delete_resource(f"{base_url}/transcriptions/{transcription_id}", api_key)
    delete_resource(f"{base_url}/files/{file_id}", api_key)


def main() -> None:
    parameters = read_parameters()
    config = read_config()

    region = config["region"]
    if region not in API_HOSTS:
        regions = ", ".join(sorted(API_HOSTS))
        fail_with_message(f"Unknown region: {region}. Valid regions: {regions}")
    base_url = f"https://{API_HOSTS[region]}/v1"
    deadline = time.monotonic() + DEADLINE_SECONDS

    file_id = upload_file(
        base_url=base_url,
        api_key=config["api_key"],
        audio_path=parameters["audio"],
        deadline=deadline,
    )
    transcription_id: str | None = None
    try:
        transcription_id = create_transcription(
            base_url=base_url,
            api_key=config["api_key"],
            file_id=file_id,
            model=config["stt_model"],
            language_hints=parse_language_hints(config["language_hints"]),
            deadline=deadline,
        )
        text = wait_for_transcription(
            base_url=base_url,
            api_key=config["api_key"],
            transcription_id=transcription_id,
            deadline=deadline,
        )
    finally:
        clean_up(base_url, config["api_key"], transcription_id, file_id)

    print(json.dumps({"text": text}))


if __name__ == "__main__":
    main()
