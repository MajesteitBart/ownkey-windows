# GPU rewrite model research (archived)

The built-in local rewrite LLM was removed on 16 September 2026 at the user's
request. These measurements describe the retired prototype. Larger-model
integration is no longer planned; Orukeet transcription remains available.

## Hardware and prototype at the time of testing

Checked on 16 September 2026:

- NVIDIA GeForce GTX 1060 6GB, compute capability 6.1, driver 581.57.
- NVIDIA reports 6,144 MiB total VRAM. About 2,630 MiB was free at the initial check.
- 64 GiB system RAM.
- The prototype bundled the CPU runtime and explicitly passes `--device none --gpu-layers 0`.

The isolated llama.cpp `b10985` Vulkan build detected this GPU and successfully
ran the existing Qwen3 1.7B model with 20 layers on the GPU. No driver installation,
app configuration change or application shutdown was needed.

For this Pascal GPU, use Vulkan or a CUDA 12 build. NVIDIA removed Pascal
offline compilation and library support in CUDA 13. A current CUDA 13 build
is therefore not a suitable default for this machine.
[NVIDIA release notes](https://docs.nvidia.com/cuda/archive/13.0.1/cuda-toolkit-release-notes/index.html),
[llama.cpp Vulkan backend](https://github.com/ggml-org/llama.cpp/blob/b10985/docs/build.md#vulkan).

## Candidates

Sizes below are GGUF weights, in decimal GB. Runtime memory and the context cache
are additional. Availability in VRAM depends on other open applications.

| Model | Q4_K_M weights | Assessment for this machine |
|---|---|---|
| Qwen3-4B-Instruct-2507 | 2.50 GB | A practical candidate for rewriting. Text-only, non-thinking model. Test with partial GPU loading under the current desktop workload. |
| Qwen3.5-4B | 2.74 GB | Newer candidate. Disable thinking and load only the text weights; its vision projector is unnecessary for Ownkey. |
| Gemma 3 4B IT | 2.49 GB | A similarly sized alternative; not included in this local writing comparison. |
| Qwen3 8B | 5.03 GB | Possible with CPU/GPU splitting, but too large for the currently free VRAM. Not the first latency-sensitive choice. |
| Qwen3.5 9B | 5.68 GB | The next quality candidate. Requires CPU/GPU splitting with the current workload; not locally benchmarked in this investigation. |
| Gemma 4 E4B IT | 4.98 GB | Its name describes effective parameters; it has 8B total including embeddings. Memory requirements resemble an 8B model. |

Model descriptions:
[Qwen3 4B Instruct](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507),
[Qwen3.5 4B](https://huggingface.co/Qwen/Qwen3.5-4B),
[Gemma 3](https://huggingface.co/google/gemma-3-4b-it),
[Gemma 4](https://huggingface.co/google/gemma-4-E4B-it).

Exact weight sizes:
[Qwen3 4B](https://huggingface.co/unsloth/Qwen3-4B-Instruct-2507-GGUF/tree/main),
[Qwen3.5 4B](https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/tree/main),
[Gemma 3 4B](https://huggingface.co/unsloth/gemma-3-4b-it-GGUF/tree/main),
[Qwen3 8B](https://huggingface.co/Qwen/Qwen3-8B-GGUF/tree/main),
[Gemma 4 E4B](https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/tree/main).
The [Qwen3.5 9B model card](https://huggingface.co/Qwen/Qwen3.5-9B) documents its
architecture and evaluations; its [Q4_K_M file](https://huggingface.co/unsloth/Qwen3.5-9B-GGUF/tree/main)
is 5,680,522,464 bytes. Published general benchmarks do not establish Dutch rewrite quality.

## Comparison protocol

Use the existing 12 synthetic writing cases plus four new cases covering Dutch
and English self-corrections, Dutch prices and a conditional deadline. Keep the
current Ownkey local rewrite prompt and selected-text rewrite prompt unchanged.

- Vulkan runtime, 20 GPU layers, four CPU threads, 2,048-token context.
- Q4_K_M weights, checked against their pinned SHA-256 hashes.
- Thinking disabled; temperature 0.7, top-p 0.8, top-k 20, min-p 0, seed 42.
- Presence penalty zero for all models. This differs from Ownkey's currently
  shipped 1.7B setting; the baseline is rerun with the same settings for comparison.
- Sequential model runs. Requests use authenticated loopback HTTP with proxies
  disabled. Models are local; text does not go to a cloud service.
- Short-case timing includes prompt processing and generation. Memory figures
  are total device usage, including the desktop, not just the model's allocation.

The isolated scripts, pinned download metadata, native logs and reports are in
`build/gpu-rewrite-research`. Benchmark processes stop after each model.
This investigation does not change Ownkey's provider, runtime or model catalog.

## Measured results

[Full outputs and timings](validation/gpu-rewrite-benchmark.json) include all
16 cases for each run. They contain synthetic text only.

| Model, same compact prompt | Load | Median rewrite | Result |
|---|---|---|---|
| Qwen3 1.7B baseline | 4.20 s | 0.88 s | Kept much of the filler. Reversed the intended corrected date in English and Dutch. |
| Qwen3 4B Instruct 2507 | 2.14 s | 1.72 s | Better correction and shortening in several cases. Translated Dutch, German and French examples into English; changed a Dutch deadline into an invoice date; produced poor Dutch in the new friendly-tone case. |
| Qwen3.5 4B | 2.92 s | 2.01 s | Preserved Dutch more consistently and shortened messages well. Still selected the wrong day in the new English and Dutch correction cases, and produced awkward Dutch when asked to change tone. |

For example, the new Dutch input began:

> We vertrekken maandag, nee woensdag, naar Eindhoven.

Qwen3.5 4B returned:

> We vertrekken maandag naar Eindhoven.

That is a meaning error, even though the sentence reads smoothly. The new
English equivalent also kept Monday instead of Wednesday. Qwen3 4B corrected
the Dutch version but failed the English one.

Repeating Qwen3.5 4B with Ownkey's longer cloud prompt fixed both new correction
cases, at a median 1.88 seconds per rewrite. However, it translated the original
Dutch date-correction case into English and still produced awkward Dutch in the
friendly-tone case. Prompt choice matters, but this run did not establish a
reliable Dutch rewrite option. This follow-up is recorded separately in the JSON
report; it is not mixed into the same-prompt comparison above.

During the 4B runs, total GPU memory use rose to about 5,300 MiB, leaving about
700 MiB free. GPU usage includes the user's desktop workload. The benchmark used
20 GPU layers, with the remaining layers on CPU; these are not full-GPU timing
claims. All benchmark subprocesses stopped when their runs completed.

## Candidate considered before removal

GPU inference is viable on this machine. However, these two 4B models did not
clear the writing-quality bar for a default replacement. Hardware compatibility
and fluent-looking text are insufficient when edits change dates or deadlines.

The next quality candidate considered was **Qwen3.5 9B Q4_K_M**, with thinking disabled and
partial GPU loading. Its weights alone are about 5.29 GiB, so a 6 GiB display GPU
cannot comfortably hold the whole model plus runtime while the current desktop
workload is active. The machine's 64 GiB system RAM allows splitting the model
between CPU and GPU. Expect a latency tradeoff, but its latency and Dutch writing
quality need measurement before selecting it. This is a candidate recommendation,
not a claim that the 9B model passed this benchmark.

Implementation would require packaging a Vulkan-capable runtime, selecting the
GPU and layer count according to available memory, adding the larger pinned
model download, and retaining the existing folder and unload-timeout controls.
That integration was not built. The current source removes the built-in local
rewrite provider and its bundled runtime.
