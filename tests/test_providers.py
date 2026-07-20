import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ownkey
import providers


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class ProviderPresetTests(unittest.TestCase):
    def test_audio_and_rewrite_capabilities_are_explicit(self):
        self.assertEqual(
            providers.AUDIO_PROVIDER_IDS, ("openai", "google", "mistral")
        )
        self.assertEqual(
            providers.REWRITE_PROVIDER_IDS,
            ("openai", "anthropic", "google", "mistral", "ollama"),
        )

    def test_ollama_has_local_and_cloud_endpoint_presets(self):
        self.assertEqual(
            providers.provider_endpoints("ollama", "rewrite"),
            (
                "http://localhost:11434/api/chat",
                "https://ollama.com/api/chat",
            ),
        )

    def test_model_endpoints_are_derived_from_activity_endpoints(self):
        cases = {
            "openai": (
                "https://api.openai.com/v1/audio/transcriptions",
                "https://api.openai.com/v1/models",
            ),
            "anthropic": (
                "https://api.anthropic.com/v1/messages",
                "https://api.anthropic.com/v1/models",
            ),
            "google": (
                "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                "https://generativelanguage.googleapis.com/v1beta/models",
            ),
            "mistral": (
                "https://api.mistral.ai/v1/chat/completions",
                "https://api.mistral.ai/v1/models",
            ),
            "ollama": (
                "https://ollama.com/api/chat",
                "https://ollama.com/api/tags",
            ),
        }
        for provider, (activity_endpoint, expected) in cases.items():
            with self.subTest(provider=provider):
                self.assertEqual(
                    providers.models_endpoint(provider, activity_endpoint), expected
                )


class ModelDiscoveryTests(unittest.TestCase):
    def test_lists_models_for_openai_anthropic_and_mistral(self):
        cases = (
            ("openai", "audio"),
            ("anthropic", "rewrite"),
            ("mistral", "rewrite"),
        )
        for provider, activity in cases:
            with self.subTest(provider=provider), patch.object(
                providers.requests,
                "get",
                return_value=FakeResponse({"data": [{"id": "z"}, {"id": "a"}]}),
            ) as request:
                models = providers.list_available_models(
                    provider,
                    "secret",
                    providers.default_endpoint(provider, activity),
                    activity,
                )

            self.assertEqual(models, ["a", "z"])
            headers = request.call_args.kwargs["headers"]
            if provider == "anthropic":
                self.assertEqual(headers["x-api-key"], "secret")
                self.assertEqual(headers["anthropic-version"], "2023-06-01")
            else:
                self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_lists_google_generate_content_models(self):
        payload = {
            "models": [
                {
                    "name": "models/gemini-a",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/embed-only",
                    "supportedGenerationMethods": ["embedContent"],
                },
            ]
        }
        with patch.object(
            providers.requests, "get", return_value=FakeResponse(payload)
        ) as request:
            models = providers.list_available_models(
                "google",
                "gemini-key",
                providers.default_endpoint("google", "rewrite"),
                "rewrite",
            )

        self.assertEqual(models, ["gemini-a"])
        self.assertEqual(
            request.call_args.kwargs["headers"]["x-goog-api-key"], "gemini-key"
        )

    def test_lists_ollama_models_without_hardcoded_names(self):
        payload = {"models": [{"name": "local-b"}, {"model": "local-a"}]}
        with patch.object(
            providers.requests, "get", return_value=FakeResponse(payload)
        ):
            models = providers.list_available_models(
                "ollama",
                "",
                "http://localhost:11434/api/chat",
                "rewrite",
            )
        self.assertEqual(models, ["local-a", "local-b"])


class ActivityAdapterTests(unittest.TestCase):
    def test_google_audio_uses_inline_wav_and_selected_model(self):
        payload = {
            "candidates": [{"content": {"parts": [{"text": "hello world"}]}}]
        }
        with patch.object(
            providers.requests, "post", return_value=FakeResponse(payload)
        ) as request:
            result = providers.transcribe_audio(
                "google",
                "key",
                providers.default_endpoint("google", "audio"),
                "gemini-test",
                b"wav data",
                "en",
            )

        self.assertEqual(result, "hello world")
        self.assertIn("gemini-test:generateContent", request.call_args.args[0])
        parts = request.call_args.kwargs["json"]["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "audio/wav")

    def test_anthropic_rewrite_uses_messages_api_shape(self):
        payload = {"content": [{"type": "text", "text": "rewritten"}]}
        with patch.object(
            providers.requests, "post", return_value=FakeResponse(payload)
        ) as request:
            result = providers.complete_rewrite(
                "anthropic",
                "key",
                providers.default_endpoint("anthropic", "rewrite"),
                "claude-test",
                "system",
                "user",
            )

        self.assertEqual(result, "rewritten")
        body = request.call_args.kwargs["json"]
        self.assertEqual(body["system"], "system")
        self.assertEqual(body["messages"], [{"role": "user", "content": "user"}])
        self.assertEqual(request.call_args.kwargs["headers"]["x-api-key"], "key")

    def test_ollama_rewrite_uses_native_chat_api(self):
        payload = {"message": {"content": "local rewrite"}}
        with patch.object(
            providers.requests, "post", return_value=FakeResponse(payload)
        ) as request:
            result = providers.complete_rewrite(
                "ollama",
                "",
                "http://localhost:11434/api/chat",
                "user-selected-model",
                "system",
                "user",
            )

        self.assertEqual(result, "local rewrite")
        self.assertEqual(
            request.call_args.kwargs["json"]["model"], "user-selected-model"
        )


class ConfigMigrationTests(unittest.TestCase):
    def test_old_shared_mistral_config_migrates_to_both_activities(self):
        legacy = {
            "api_key": "legacy-key",
            "endpoint": "https://api.mistral.ai/v1/audio/transcriptions",
            "model": "voxtral-old",
            "chat_endpoint": "https://api.mistral.ai/v1/chat/completions",
            "rewrite_model": "mistral-old",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            with patch.object(ownkey, "CONFIG_FILE", str(path)):
                config = ownkey.load_config()

        self.assertEqual(config["audio_provider"], "mistral")
        self.assertEqual(config["audio_api_key"], "legacy-key")
        self.assertEqual(config["audio_model"], "voxtral-old")
        self.assertEqual(config["rewrite_provider"], "mistral")
        self.assertEqual(config["rewrite_api_key"], "legacy-key")
        self.assertEqual(config["rewrite_model"], "mistral-old")


if __name__ == "__main__":
    unittest.main()
