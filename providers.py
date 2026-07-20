"""Provider presets and HTTP adapters for Ownkey's audio and rewrite activities."""

from __future__ import annotations

import base64
from urllib.parse import quote, urlparse, urlunparse

import requests


PROVIDER_PRESETS = {
    "openai": {
        "label": "OpenAI",
        "audio_endpoints": ("https://api.openai.com/v1/audio/transcriptions",),
        "rewrite_endpoints": ("https://api.openai.com/v1/chat/completions",),
        "models_endpoint": "https://api.openai.com/v1/models",
    },
    "anthropic": {
        "label": "Anthropic",
        "audio_endpoints": (),
        "rewrite_endpoints": ("https://api.anthropic.com/v1/messages",),
        "models_endpoint": "https://api.anthropic.com/v1/models",
    },
    "google": {
        "label": "Google",
        "audio_endpoints": (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        ),
        "rewrite_endpoints": (
            "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        ),
        "models_endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
    },
    "mistral": {
        "label": "Mistral",
        "audio_endpoints": ("https://api.mistral.ai/v1/audio/transcriptions",),
        "rewrite_endpoints": ("https://api.mistral.ai/v1/chat/completions",),
        "models_endpoint": "https://api.mistral.ai/v1/models",
    },
    "ollama": {
        "label": "Ollama",
        "audio_endpoints": (),
        "rewrite_endpoints": (
            "http://localhost:11434/api/chat",
            "https://ollama.com/api/chat",
        ),
        "models_endpoint": "http://localhost:11434/api/tags",
    },
}

AUDIO_PROVIDER_IDS = tuple(
    provider_id
    for provider_id, preset in PROVIDER_PRESETS.items()
    if preset["audio_endpoints"]
)
REWRITE_PROVIDER_IDS = tuple(PROVIDER_PRESETS)
PROVIDER_LABEL_TO_ID = {
    preset["label"]: provider_id for provider_id, preset in PROVIDER_PRESETS.items()
}


class ProviderConfigurationError(ValueError):
    """Raised when an activity is configured for an unsupported provider."""


def normalize_provider(value: object, fallback: str = "mistral") -> str:
    """Return a known provider id from either an id or display label."""
    normalized = str(value or "").strip().lower()
    if normalized in PROVIDER_PRESETS:
        return normalized
    for label, provider_id in PROVIDER_LABEL_TO_ID.items():
        if normalized == label.lower():
            return provider_id
    return fallback


def provider_label(provider: str) -> str:
    provider_id = normalize_provider(provider)
    return PROVIDER_PRESETS[provider_id]["label"]


def provider_labels(provider_ids: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(provider_label(provider_id) for provider_id in provider_ids)


def provider_endpoints(provider: str, activity: str) -> tuple[str, ...]:
    provider_id = normalize_provider(provider)
    key = "audio_endpoints" if activity == "audio" else "rewrite_endpoints"
    return tuple(PROVIDER_PRESETS[provider_id][key])


def default_endpoint(provider: str, activity: str) -> str:
    endpoints = provider_endpoints(provider, activity)
    return endpoints[0] if endpoints else ""


def provider_from_endpoint(endpoint: object, fallback: str = "mistral") -> str:
    """Best-effort provider inference used when migrating the old shared config."""
    host = (urlparse(str(endpoint or "")).hostname or "").lower()
    if "anthropic" in host:
        return "anthropic"
    if "generativelanguage.googleapis" in host:
        return "google"
    if "mistral" in host:
        return "mistral"
    if "openai" in host:
        return "openai"
    if host in {"localhost", "127.0.0.1", "::1", "ollama.com"} or "ollama" in host:
        return "ollama"
    return fallback


def provider_requires_key(provider: str, endpoint: str = "") -> bool:
    """Return whether Ownkey should block a request when no API key is set."""
    if normalize_provider(provider) != "ollama":
        return True
    return (urlparse(str(endpoint or "")).hostname or "").lower() == "ollama.com"


def _auth_headers(provider: str, api_key: str) -> dict[str, str]:
    provider_id = normalize_provider(provider)
    key = str(api_key or "").strip()
    if provider_id == "anthropic":
        headers = {"anthropic-version": "2023-06-01"}
        if key:
            headers["x-api-key"] = key
        return headers
    if provider_id == "google":
        return {"x-goog-api-key": key} if key else {}
    return {"Authorization": f"Bearer {key}"} if key else {}


def _model_url(endpoint: str, model: str) -> str:
    clean_model = str(model or "").strip()
    if clean_model.startswith("models/"):
        clean_model = clean_model[len("models/") :]
    encoded_model = quote(clean_model, safe="-._:")
    if "{model}" in endpoint:
        return endpoint.replace("{model}", encoded_model)
    return endpoint.rstrip("/") + f"/models/{encoded_model}:generateContent"


def _extract_openai_text(result: dict) -> str:
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        ).strip()
    return ""


def _extract_google_text(result: dict) -> str:
    try:
        parts = result["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(
        str(part.get("text", "")) for part in parts if isinstance(part, dict)
    ).strip()


def transcribe_audio(
    provider: str,
    api_key: str,
    endpoint: str,
    model: str,
    wav_bytes: bytes,
    language: str = "auto",
    *,
    timeout: float = 30,
) -> str:
    """Transcribe WAV bytes through an officially supported provider API."""
    provider_id = normalize_provider(provider)
    if provider_id not in AUDIO_PROVIDER_IDS:
        raise ProviderConfigurationError(
            f"{provider_label(provider_id)} does not provide a supported audio transcription API."
        )
    if not str(model or "").strip():
        raise ProviderConfigurationError("Select an audio model in Settings.")

    headers = _auth_headers(provider_id, api_key)
    if provider_id in {"openai", "mistral"}:
        data = {"model": model}
        if language and language != "auto":
            data["language"] = language
        response = requests.post(
            endpoint,
            headers=headers,
            data=data,
            files={"file": ("audio.wav", wav_bytes, "audio/wav")},
            timeout=timeout,
        )
        response.raise_for_status()
        return str(response.json().get("text", "")).strip()

    language_instruction = (
        " Detect the language automatically."
        if not language or language == "auto"
        else f" The expected language is {language}."
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {
                        "text": (
                            "Transcribe the speech in this audio exactly. Return only the "
                            "transcript, without timestamps, labels, or commentary."
                            + language_instruction
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": "audio/wav",
                            "data": base64.b64encode(wav_bytes).decode("ascii"),
                        }
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    response = requests.post(
        _model_url(endpoint, model), headers=headers, json=body, timeout=timeout
    )
    response.raise_for_status()
    return _extract_google_text(response.json())


def complete_rewrite(
    provider: str,
    api_key: str,
    endpoint: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    *,
    timeout: float = 30,
) -> str:
    """Run a text rewrite through the selected provider's native API."""
    provider_id = normalize_provider(provider)
    if not str(model or "").strip():
        raise ProviderConfigurationError("Select a rewrite model in Settings.")
    headers = _auth_headers(provider_id, api_key)
    headers["Content-Type"] = "application/json"

    if provider_id in {"openai", "mistral"}:
        body = {
            "model": model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
        response.raise_for_status()
        return _extract_openai_text(response.json())

    if provider_id == "anthropic":
        body = {
            "model": model,
            "max_tokens": 4096,
            "temperature": 0.2,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        response = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
        response.raise_for_status()
        result = response.json()
        return "".join(
            str(block.get("text", ""))
            for block in result.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()

    if provider_id == "google":
        body = {
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [
                {"role": "user", "parts": [{"text": user_prompt}]},
            ],
            "generationConfig": {"temperature": 0.2},
        }
        response = requests.post(
            _model_url(endpoint, model), headers=headers, json=body, timeout=timeout
        )
        response.raise_for_status()
        return _extract_google_text(response.json())

    body = {
        "model": model,
        "stream": False,
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    response = requests.post(endpoint, headers=headers, json=body, timeout=timeout)
    response.raise_for_status()
    try:
        return str(response.json()["message"]["content"]).strip()
    except (KeyError, TypeError):
        return ""


def _replace_endpoint_path(endpoint: str, suffixes: tuple[str, ...], replacement: str) -> str:
    parsed = urlparse(endpoint)
    path = parsed.path.rstrip("/")
    for suffix in suffixes:
        if path.endswith(suffix):
            path = path[: -len(suffix)] + replacement
            break
    else:
        path = path + replacement
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def models_endpoint(provider: str, activity_endpoint: str) -> str:
    """Derive a model-list endpoint, including custom/self-hosted base URLs."""
    provider_id = normalize_provider(provider)
    endpoint = str(activity_endpoint or "").strip()
    if not endpoint:
        return str(PROVIDER_PRESETS[provider_id]["models_endpoint"])
    if provider_id in {"openai", "mistral"}:
        return _replace_endpoint_path(
            endpoint, ("/audio/transcriptions", "/chat/completions"), "/models"
        )
    if provider_id == "anthropic":
        return _replace_endpoint_path(endpoint, ("/messages",), "/models")
    if provider_id == "google":
        parsed = urlparse(endpoint)
        path = parsed.path
        models_index = path.find("/models")
        if models_index >= 0:
            path = path[:models_index] + "/models"
        else:
            path = path.rstrip("/") + "/models"
        return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))
    return _replace_endpoint_path(endpoint, ("/api/chat",), "/api/tags")


def list_available_models(
    provider: str,
    api_key: str,
    activity_endpoint: str,
    activity: str,
    *,
    timeout: float = 15,
) -> list[str]:
    """Retrieve model identifiers from the selected provider's models API."""
    provider_id = normalize_provider(provider)
    if activity not in {"audio", "rewrite"}:
        raise ProviderConfigurationError(f"Unknown provider activity: {activity}")
    if activity == "audio" and provider_id not in AUDIO_PROVIDER_IDS:
        raise ProviderConfigurationError(
            f"{provider_label(provider_id)} does not provide a supported audio transcription API."
        )
    headers = _auth_headers(provider_id, api_key)
    params = None
    if provider_id == "anthropic":
        params = {"limit": 1000}
    elif provider_id == "google":
        params = {"pageSize": 1000}

    response = requests.get(
        models_endpoint(provider_id, activity_endpoint),
        headers=headers,
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()

    if provider_id in {"openai", "mistral", "anthropic"}:
        items = result if isinstance(result, list) else result.get("data", [])
        models = [item.get("id") for item in items if isinstance(item, dict)]
    elif provider_id == "google":
        models = []
        for item in result.get("models", []):
            if not isinstance(item, dict):
                continue
            supported = item.get("supportedGenerationMethods", item.get("supportedActions", []))
            if supported and "generateContent" not in supported:
                continue
            model_id = item.get("baseModelId") or item.get("name")
            if isinstance(model_id, str) and model_id.startswith("models/"):
                model_id = model_id[len("models/") :]
            models.append(model_id)
    else:
        models = [
            item.get("name") or item.get("model")
            for item in result.get("models", [])
            if isinstance(item, dict)
        ]

    return sorted(
        {str(model).strip() for model in models if str(model or "").strip()},
        key=str.casefold,
    )
