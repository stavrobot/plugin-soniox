# plugin-soniox

A Stavrobot plugin that turns text into speech and transcribes audio using [Soniox](https://soniox.com). Text-to-speech returns an MP3 file. Speech-to-text handles voice notes and recordings, including audio that mixes Greek and English.

## Before you install

You need a Soniox account and an API key. Create a key in the [Soniox Console](https://console.soniox.com).

## Installation

Tell Stavrobot:

> Install the plugin at `https://github.com/stavrobot/plugin-soniox`

Then give it your API key:

> Configure the soniox plugin with my API key: `<your-api-key>`

## Configuration

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `api_key` | Yes | | Your Soniox API key, from the [Soniox Console](https://console.soniox.com). |
| `default_voice` | No | `Sebastian` | The voice to use when a request does not name one. You can listen to the voices in the Soniox Console, under Text-to-speech > Voices. |
| `default_language` | No | `en` | The ISO language code to use for text-to-speech when a request does not give one. It sets the accent for the whole clip. |
| `default_speed` | No | `1.0` | The speech speed when a request does not give one. Use a value from `0.7` to `1.3`, where `1.0` is normal. |
| `default_reduce_silence` | No | `false` | Whether to shorten the pauses between words when a request does not give a value. Only works on models that support it. |
| `language_hints` | No | `el,en` | A comma-separated list of the languages you expect in audio you transcribe. Hints make transcription more accurate. |
| `tts_model` | No | `tts-rt-v2` | The Soniox text-to-speech model. See [TTS models](https://docs.soniox.com/tts/models). |
| `stt_model` | No | `stt-async-v6` | The Soniox speech-to-text model. See [STT models](https://docs.soniox.com/stt/models). |
| `region` | No | `us` | The Soniox region of your project: `us`, `eu`, `jp` or `in`. It must match the region of the project that your API key belongs to. See [data residency](https://docs.soniox.com/data-residency). |

## Tools

- `text_to_speech`: turns text into an MP3 file. You can choose a voice, a language, a speed from `0.7` to `1.3`, and whether to shorten the pauses between words. Silence reduction only works on models that support it. A request can hold up to 5000 characters, and Soniox stops the audio after 2 minutes, so Stavrobot splits longer text.
- `speech_to_text`: transcribes an audio file and returns the text. Supported formats include ogg (Signal voice notes), mp3, m4a, wav, flac, aac and webm. A transcription must finish in about 20 seconds, so very long recordings fail with an error. The plugin deletes the uploaded audio and the transcript from Soniox when it is done. If a transcription times out, Soniox does not allow it to be deleted while it is still running, so the transcript stays in your Soniox project until Soniox removes it after 30 days. The uploaded audio is still deleted.

## Usage

> Read this aloud: "Hello, world!"

> Say "Καλημέρα" in the voice Nina.

> Transcribe this voice note.
