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


class ApiErrorTests(unittest.TestCase):
    def test_falsey_403_response_preserves_provider_explanation(self):
        response = providers.requests.Response()
        response.status_code = 403
        response._content = json.dumps({"error": {"message":
            "This model requires 18+ age confirmation. Confirm at https://openrouter.ai/settings/preferences."
        }}).encode()
        self.assertFalse(response)
        error = providers.requests.HTTPError(response=response)
        message = providers.describe_api_error(error)
        self.assertIn("API error 403", message)
        self.assertIn("18+ age confirmation", message)
        self.assertIn("https://openrouter.ai/settings/preferences", message)

    def test_plain_text_and_missing_responses(self):
        response = providers.requests.Response()
        response.status_code = 502
        response._content = b"Upstream temporarily unavailable"
        self.assertIn("Upstream temporarily unavailable", providers.describe_api_error(
            providers.requests.HTTPError(response=response)))
        self.assertIn("No response", providers.describe_api_error(providers.requests.HTTPError()))


class ProviderPresetTests(unittest.TestCase):
    def test_audio_and_rewrite_capabilities_are_explicit(self):
        self.assertEqual(
            providers.AUDIO_PROVIDER_IDS, ("openai", "google", "mistral", "custom")
        )
        self.assertEqual(
            providers.REWRITE_PROVIDER_IDS,
            ("openai", "anthropic", "google", "mistral", "ollama", "openrouter", "custom"),
        )

    def test_ollama_has_local_and_cloud_endpoint_presets(self):
        self.assertEqual(
            providers.provider_endpoints("ollama", "rewrite"),
            (
                "http://localhost:11434/api/chat",
                "https://ollama.com/api/chat",
            ),
        )

    def test_openai_prefers_responses_and_keeps_chat_completions_compatible(self):
        self.assertEqual(
            providers.provider_endpoints("openai", "rewrite"),
            (
                "https://api.openai.com/v1/responses",
                "https://api.openai.com/v1/chat/completions",
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

    def test_openai_responses_endpoint_derives_models_endpoint(self):
        self.assertEqual(
            providers.models_endpoint(
                "openai", "https://api.openai.com/v1/responses"
            ),
            "https://api.openai.com/v1/models",
        )


class ModelDiscoveryTests(unittest.TestCase):
    def test_lists_models_for_openai_anthropic_and_mistral(self):
        cases = (
            ("openai", "audio"),
            ("anthropic", "rewrite"),
            ("mistral", "rewrite"),
            ("openrouter", "rewrite"),
            ("custom", "rewrite"),
            ("custom", "audio"),
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
    def test_output_limit_never_returns_a_partial_rewrite(self):
        payload = {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        with patch.object(providers.requests, "post", return_value=FakeResponse(payload)):
            with self.assertRaisesRegex(providers.ProviderConfigurationError, "output limit"):
                providers.complete_rewrite("openrouter", "key", providers.default_endpoint("openrouter", "rewrite"),
                                           "model", "edit", "long text")

    def test_compatible_rewrite_preserves_url_model_and_auth(self):
        for provider, endpoint, key in (
            ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "router-key"),
            ("custom", "http://localhost:1234/v1/chat/completions", ""),
            ("custom", "https://example.test/proxy/v2/chat/completions", "custom-key"),
        ):
            with self.subTest(provider=provider, endpoint=endpoint), patch.object(
                providers.requests, "post",
                return_value=FakeResponse({"choices": [{"message": {"content": "edited"}}]}),
            ) as request:
                result = providers.complete_rewrite(provider, key, endpoint, "vendor/model", "edit", "hello")
                self.assertEqual(result, "edited")
                self.assertEqual(request.call_args.args[0], endpoint)
                self.assertEqual(request.call_args.kwargs["json"]["model"], "vendor/model")
                self.assertEqual(request.call_args.kwargs["json"]["max_tokens"], 4096)
                self.assertEqual(request.call_args.kwargs["headers"].get("Authorization"),
                                 f"Bearer {key}" if key else None)
                self.assertEqual(providers.models_endpoint(provider, endpoint),
                                 endpoint.replace("/chat/completions", "/models"))

    def test_custom_audio_supports_optional_auth_and_multipart(self):
        with patch.object(providers.requests, "post", return_value=FakeResponse({"text": "hello"})) as request:
            result = providers.transcribe_audio("custom", "", "http://localhost:1234/v1/audio/transcriptions",
                                                "whisper", b"wav", "nl")
        self.assertEqual(result, "hello")
        self.assertFalse(providers.provider_requires_key("custom"))
        self.assertEqual(request.call_args.kwargs["data"], {"model": "whisper", "language": "nl"})
        self.assertEqual(request.call_args.kwargs["files"]["file"][1], b"wav")

    def test_custom_responses_api(self):
        payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "edited"}]}]}
        with patch.object(providers.requests, "post", return_value=FakeResponse(payload)) as request:
            self.assertEqual(providers.complete_rewrite("custom", "", "http://localhost:1234/v1/responses",
                                                       "model", "edit", "hello"), "edited")
        self.assertEqual(request.call_args.kwargs["json"]["instructions"], "edit")

    def test_openai_rewrite_uses_responses_api_shape(self):
        payload = {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "rewritten response"}
                    ],
                }
            ]
        }
        with patch.object(
            providers.requests, "post", return_value=FakeResponse(payload)
        ) as request:
            result = providers.complete_rewrite(
                "openai",
                "key",
                "https://api.openai.com/v1/responses",
                "gpt-test",
                "system guidance",
                "user text",
            )

        self.assertEqual(result, "rewritten response")
        body = request.call_args.kwargs["json"]
        self.assertEqual(
            body,
            {
                "model": "gpt-test",
                "instructions": "system guidance",
                "input": "user text",
                "store": False,
            },
        )
        self.assertEqual(
            request.call_args.args[0], "https://api.openai.com/v1/responses"
        )

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
    def test_custom_and_openrouter_configs_preserve_provider_endpoint_and_model(self):
        for provider, endpoint in (
            ("custom", "http://localhost:1234/v1/chat/completions"),
            ("custom", "https://example.test/proxy/v1/responses"),
            ("openrouter", "https://openrouter.ai/api/v1/chat/completions"),
        ):
            for explicit in (False, True):
                config = {"rewrite_endpoint": endpoint, "rewrite_model": "vendor/model"}
                if explicit:
                    config["rewrite_provider"] = provider
                with self.subTest(provider=provider, explicit=explicit), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with patch.object(ownkey, "CONFIG_FILE", str(path)):
                        loaded = ownkey.load_config()
                    self.assertEqual(loaded["rewrite_provider"], provider)
                    self.assertEqual(loaded["rewrite_endpoint"], endpoint)
                    self.assertEqual(loaded["rewrite_model"], "vendor/model")

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
