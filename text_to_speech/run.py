#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests"]
# ///

import json
import os
import pathlib
import sys
from typing import TypedDict

import requests


KNOWN_PARAMETERS: set[str] = {"text", "voice", "language"}
OUTPUT_FILENAME = "output.mp3"
# The plugin runner kills a tool at 30 seconds. Timing out first lets us report a
# Soniox error instead of dying with no explanation.
TIMEOUT_SECONDS = 25
# Region "us" is the default and has no regional prefix in its hostname, while the
# others embed the region, so the mapping cannot be derived uniformly.
TTS_HOSTS: dict[str, str] = {
    "us": "tts-rt.soniox.com",
    "eu": "tts-rt.eu.soniox.com",
    "jp": "tts-rt.jp.soniox.com",
    "in": "tts-rt.in.soniox.com",
}
# The tool runs inside a Docker container where the working directory is
# text_to_speech/, so the plugin config lives one level up.
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
    if "text" not in raw:
        print("Missing required parameter: text", file=sys.stderr)
        sys.exit(1)
    return raw


def read_config() -> Config:
    config: Config = json.loads(CONFIG_PATH.read_text())
    # A pasted key often carries a trailing newline, which makes requests raise
    # InvalidHeader and include the full header value, leaking the key to stderr.
    config["api_key"] = config["api_key"].strip()
    return config


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


def call_text_to_speech_api(
    text: str,
    voice: str,
    language: str,
    model: str,
    region: str,
    api_key: str,
) -> bytes:
    url = f"https://{TTS_HOSTS[region]}/tts"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "language": language,
            "voice": voice,
            "audio_format": "mp3",
            "text": text,
        },
        timeout=TIMEOUT_SECONDS,
    )
    if not response.ok:
        print(format_api_error(response), file=sys.stderr)
        sys.exit(1)
    return response.content


def write_output(audio_bytes: bytes) -> None:
    output_directory = pathlib.Path(os.environ["STAVROBOT_TEMP_DIR"])
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / OUTPUT_FILENAME).write_bytes(audio_bytes)


def main() -> None:
    parameters = read_parameters()
    config = read_config()

    region = config["region"]
    if region not in TTS_HOSTS:
        regions = ", ".join(sorted(TTS_HOSTS))
        print(f"Unknown region: {region}. Valid regions: {regions}", file=sys.stderr)
        sys.exit(1)

    # Callers usually want the configured voice and language, and only override them
    # for a specific request, so the config values are the defaults here.
    voice = parameters.get("voice", config["default_voice"])
    language = parameters.get("language", config["default_language"])

    audio_bytes = call_text_to_speech_api(
        text=parameters["text"],
        voice=voice,
        language=language,
        model=config["tts_model"],
        region=region,
        api_key=config["api_key"],
    )
    write_output(audio_bytes)
    print(json.dumps({"file": OUTPUT_FILENAME}))


if __name__ == "__main__":
    main()
