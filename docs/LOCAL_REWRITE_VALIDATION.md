# Local rewrite validation (archived)

The built-in local rewrite LLM was removed on 16 September 2026 because its
edits were not reliable enough, especially in Dutch. Orukeet transcription
remains available. Ownkey no longer bundles llama.cpp or offers rewrite model
downloads. Existing rewrite weights are left on disk.

The results below record the retired prototype, not the current app. See also
[the larger-model comparison](GPU_REWRITE_RESEARCH.md).

## Documented language support

| Candidate | English | Dutch | Other documented languages |
|---|---|---|---|
| Liquid LFM2.5-1.2B-Instruct | Listed | Not listed | Arabic, Chinese, French, German, Japanese, Korean, Spanish |
| Qwen2.5-1.5B-Instruct | Listed | Not confirmed by its model card | Over 29 languages; examples include French, German, Spanish, Portuguese, Italian, Russian, Chinese, Japanese, Korean, Vietnamese, Thai and Arabic |
| Qwen3-1.7B | Listed | Explicitly listed | 119 languages and dialects, including German, French, Afrikaans and Limburgish |

Sources: [Liquid model card](https://huggingface.co/LiquidAI/LFM2.5-1.2B-Instruct),
[Qwen2.5 model card](https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct),
[Qwen3 language list](https://qwenlm.github.io/blog/qwen3/).
These are the publishers' coverage claims, not evidence that every language works
equally well for rewriting. The current Liquid 1.2B instruction model was used
for this comparison, rather than the older LFM2 checkpoint.

## Writing test

Run on DESKTOP-ZOLDER with four CPU threads, Q4_K_M weights, an 8,192-token
context and no cloud HTTP. The set contains five English and five Dutch cases,
plus one German and one French case. It covers hesitation, sentence boundaries,
spoken corrections, dates, amounts, negations, embedded requests and shortening
a selected message. Inputs are synthetic and contain no user text.

The original cloud prompt caused example copying and unwanted translation.
Local dictation therefore uses a shorter prompt without examples. Qwen3 uses
the publisher's non-thinking sampling settings: temperature 0.7, top-p 0.8,
top-k 20 and presence penalty 1.5, with a fixed seed of 42. Liquid and Qwen2.5
use temperature 0.1. The
[Qwen3 GGUF card](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF) documents its
non-thinking settings. Selected-text rewrites retain the existing instruction prompt.

Results and timings for the final prompt are saved in
[local-rewrite-benchmark.json](validation/local-rewrite-benchmark.json).
Literal-preservation checks in that file are aids to review, not a quality score.
A response can preserve every number and still change the meaning.

| Candidate | Download | Observed behavior |
|---|---|---|
| Qwen3 1.7B | 1,107 MB | Kept Dutch in all five Dutch cases. Preserved the invoice and negation examples. Failed to cleanly resolve the Dutch date correction and left some hesitation in other languages. Shortening was modest. |
| Liquid 1.2B | 731 MB | Smaller and fast, but Dutch outputs mixed languages, copied instructions, or changed meaning. English edits also sometimes changed the request or correction. |
| Qwen2.5 1.5B | 1,117 MB | Frequently translated Dutch, German and French into English. Its Dutch shortening result was unnatural. |

This small, single-seed test supports choosing Qwen3 for a prototype. It does
**not** establish production-quality Dutch rewriting. Natural dictation, longer
selections, more speakers and independent holdout cases remain release checks.

## Runtime and lifecycle checks

- Models load on first use; neither app startup nor selecting a provider downloads weights.
- Save validates and loads the selected model before committing settings.
- Idle expiry unloads the subprocess, and the next request reloads it. Zero disables idle expiry.
- A pending rewrite keeps its model loaded through timeout expiry or a provider switch.
- The runtime binds to `127.0.0.1` with a random per-process API key. Requests bypass environment proxies; llama.cpp runs with `--offline` and no web UI.
- A Windows job object stops the runtime even if its owning Python process is forcibly terminated. The forced-exit probe passed on this machine.
- Output limits, empty responses and unexpected thinking tags produce an error instead of inserting partial output.
- Settings UI smoke tests loaded Orukeet and Qwen3 from existing folders, saved both providers, and retained OpenRouter and Custom in the rewrite menu.

The isolated runtime is larger in memory than the GGUF download alone. The
JSON report includes measured process RSS after the cases, cold-load latency and
warm request timings. These are single-machine measurements, not minimum requirements.
Clean Windows installation and lower-memory hardware remain untested.

| Candidate | Load | Median warm rewrite | Process RSS after cases |
|---|---|---|---|
| Qwen3 1.7B | 2.53 s | 1.58 s | 2,715 MiB |
| Liquid 1.2B | 1.31 s | 1.28 s | 1,349 MiB |
| Qwen2.5 1.5B | 2.23 s | 1.47 s | 1,852 MiB |

The 100-test Python suite finished with 92 passes and eight Linux-only skips on Windows,
including the real overlay process checks. Signing-pipeline checks passed.
Both frozen smoke modes passed with Python removed from PATH: Dutch/English
transcription, Dutch/English rewriting, idle unload, reload, and subprocess exit.

