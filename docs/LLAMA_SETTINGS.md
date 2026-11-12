# llama.cpp settings: what each one changes, and when it matters here

Box: RTX 3060 12 GB (360 GB/s, Ampere, 3 MB L2), Ryzen 5 3600 (6 cores / 12 threads), 30 GB DDR4, llama.cpp b11936.
Measurements: `evals/speed/sweep.py` (llama-bench grids + speculative configs), older ones in `MODEL_NOTES.md`.
Written 2026-11-01; the "measured" notes say where a number comes from, the rest is how llama.cpp works.

## The one idea behind most of it
Generating a token reads every weight the token uses, once. So **generation speed ≈ memory bandwidth ÷ bytes
read per token**, and the question is always *where those bytes live*:

- On the GPU: 360 GB/s. A dense 27B at IQ3_XXS (~10 GB) can't beat ~36 tok/s even in theory; in practice
  55–65% of that (measured: 20.7 tok/s). A 4070 Ti (504 GB/s, 48 MB L2) gets ~40 on the same file.
- In RAM: dual-channel DDR4 is roughly 35–45 GB/s, ten times slower. Every layer of a dense model moved to RAM
  costs a lot (measured: 8 of 65 Qwen3.8 layers in RAM → 6.7 tok/s instead of 20).
- **MoE models** (gemma-4-26B-A4B, Qwen3.6-35B-A3B) are why a 17 GB file runs at 38 tok/s on a 12 GB card:
  each token only uses a few experts (~4B of 26B parameters), so the experts that don't fit can sit in RAM and
  only the ones chosen are read from there. Attention and shared weights stay on the GPU.

**Prompt reading (prefill)** is different: many tokens go through each weight at once, so it is limited by
compute, not bandwidth, and by how big the batches are (`-ub`). It's what makes the router and long tool
prompts slow (~550–800 tok/s here, so a 5k-token agent prompt is 6–9 s before the first token).

## Placement: what goes on the GPU
| flag | what it does | here |
|---|---|---|
| `-ngl N` | how many layers go on the GPU (`99` = all) | dense models: must be all, or speed falls off a cliff |
| `--fit on` | llama.cpp places things itself: fills the GPU leaving a margin, moves MoE experts (then layers) to RAM | what production gemma uses; good, but it can't see a draft model (DFlash OOM until `-fitt 2048`) |
| `-fitt MiB` | the margin `--fit` leaves free per GPU (default 1024) | 256 lets Qwen3.8 IQ3 keep all layers; 2048 for DFlash |
| `-fitc N` | the smallest context `--fit` must keep room for | |
| `-ncmoe N` / `--n-cpu-moe N` | experts of the first N layers in RAM, the rest on GPU (manual version of what fit does) | sweep: does a hand-picked N beat fit? |
| `-ot 'regex=CPU'` | per-tensor placement by name (e.g. all `ffn_*` weights in RAM) | the "FFN in RAM" 27B recipe: 3.6 tok/s, tuned for 16 GB cards |
| `--mmproj` | vision projector | +1.2 GB, −5–11% speed (measured), off until documents use it |

**Desktop VRAM counts.** On 2026-11-01 the desktop held ~730 MB of the 12 GB (gnome-shell 265, Chrome ~270,
VS Code 110, Docker 61, Nautilus 26). That's experts `--fit` puts in RAM, or room a small router can't have.
Headless (or closing those apps) gives it back. It also makes eval numbers depend on what's open.

## Context and the KV cache
| flag | what it does | here |
|---|---|---|
| `-c N` | context length; the KV cache (the model's memory of the conversation) grows linearly with it | 32k for the bot; each extra 1k costs VRAM that experts or layers could use |
| `-ctk / -ctv` | KV cache precision: f16 (default), q8_0 (half), q4_0 (quarter) | q8_0 is close to lossless; q4_0 is what lets Qwen3.8 IQ3 keep every layer on the GPU |
| `-fa on` | flash attention: less memory, faster at long context; needed for a quantized V cache | always on |
| `-np N` | parallel slots: N conversations at once, the context split between them | 1: one GPU, one request at a time |
| `--swa-full` | full-size cache for gemma's sliding-window layers | measured worse on every count |
| `--cache-ram MiB` | RAM for saved prompt prefixes between requests | |

Speed falls as the context fills (every token attends to all earlier ones): the sweep measures at 0 and 8k.

## CPU side (only matters for weights in RAM: gemma's experts)
| flag | what it does | here |
|---|---|---|
| `-t N` | CPU threads for generation | 6 real cores; hyperthreads share them and the RAM bus, so 12 is often not faster. Sweep: 4/6/8/12 |
| `-tb N` | threads for prompt batches (default: same as `-t`) | |
| `--load-mode none` (`--no-mmap`) | read the model into RAM instead of mapping the file | llama.cpp itself logs "tensor overrides to CPU are used with mmap enabled - consider using --load-mode none for better performance" for production gemma. Sweep: mmap vs none |
| `--repack` | rearranges CPU weights for faster kernels (on by default; needs non-mmap memory to apply fully) | |

## Batch sizes (prompt reading only)
| flag | what it does | here |
|---|---|---|
| `-b N` | logical batch: how many prompt tokens are submitted at once (default 2048) | |
| `-ub N` | physical batch: how many go through the GPU in one pass (default 512) | bigger = faster prefill (especially with experts in RAM: each expert copy to the GPU serves more tokens), but a bigger compute buffer in VRAM. Sweep: 512/1024/2048 (256 for dense) |

No effect on generation speed.

## Speculative decoding (faster generation, same output)
A cheap guesser proposes several next tokens; the big model checks them all in one pass and keeps the ones
it agrees with. The output is exactly what the big model would have written; only speed changes, and only if
enough guesses are accepted to pay for the checking.

| flag | what it does | here |
|---|---|---|
| `--spec-type draft-mtp` | the model's own built-in guesser head (needs a file with it, e.g. `*-mtp.gguf`) | lost before (6–9 tok/s vs 20) because its memory pushed layers off the GPU; retest with short drafts and q4_0 draft KV, and on IQ2_XS which leaves room |
| `--spec-type draft-dflash` + `-md file` | a separate small drafter that guesses a whole block in one pass (z-lab DFlash) | gemma's drafter is 457 MB (converted locally). MoE with experts in RAM makes checking 8–15 tokens costly: measure |
| `--spec-draft-n-max N` | most tokens guessed per step | short (2–4) when acceptance is low, long when the text is predictable (code) |
| `--spec-draft-p-min p` | only keep guessing while the guesser is this sure | |
| `--spec-draft-type-k/v` | the draft's own KV cache precision | q4_0 saves VRAM |
| `ngram-*` types | guess by copying earlier text: free, no model | could help when replies quote the prompt (mail drafts) |

Acceptance is in the server's timings (`draft_n_accepted / draft_n`); the speed bench prints it.

## Templates and thinking
| flag / field | what it does |
|---|---|
| `--jinja` | use the model's own chat template (tool calls, thinking) |
| `chat_template_kwargs.enable_thinking` | thinking on/off per request |
| `chat_template_kwargs.reasoning_effort` | Qwen3.8 only: low / medium / xhigh. **Default is xhigh**; medium was its best (MODEL_NOTES). bench.py sent nothing until 2026-11-01, so earlier Qwen3.8 bench scores ran at xhigh |
| `max_tokens` | caps a reasoning loop; thinking tokens are most of the time in hard turns |

## Sampling (quality, not speed)
`--temp`, `--top-k`, `--top-p`, `--min-p`, `--repeat-penalty`: each family's recommended values are in
`evals/mail_eval.py` SERVERS. They change answers, not tok/s, except that a bad setting can cause loops.

## Forks seen in 12 GB success stories (not stock llama.cpp)
- RouteWeaver (github.com/kadenball/qwen38-27b-rtx3060-dcfr): 3060, Qwen3.8-27B GSQ IQ3 + MTP, 16–23 tok/s
  baseline, 18–26 with its patches, 35–48 on code in its "fast" variant. A patch set, not flags.
- GenerelSchwerz/llama.cpp: an MoE expert cache (`--moe-expert-cache-size`); numbers from a 16 GB card.
- Both are worth knowing about; neither is a setting we can turn on in b11936.
