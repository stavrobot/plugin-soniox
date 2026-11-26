# Development

Run the tests (all HTTP calls are mocked, no API key needed):

```sh
uv run --with pytest --with responses --with requests pytest
```

To try a tool by hand, create `config.json` at the repository root (it is gitignored), then run the tool from its own directory:

```sh
cd text_to_speech
echo '{"text": "Hello"}' | STAVROBOT_TEMP_DIR=/tmp/soniox-test ./run.py
```
