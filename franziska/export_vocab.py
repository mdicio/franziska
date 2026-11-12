"""Export a model's tokenizer as a tensor-free GGUF (~16 MB instead of ~8 GB), for the router on the always-on box.

AnyJev renders prompts and maps option labels with the GGUF's own vocabulary and chat template, while the logprobs
come from Ollama on the desktop. The box only needs the vocabulary. Run on the desktop, then copy the file over:

    uv run --with gguf==0.19.0 python -m franziska.export_vocab gemma4:12b ~/.local/share/franziska/router-vocab.gguf
    uv run --with gguf==0.19.0 python -m franziska.export_vocab ~/models/gguf/gemma-4-26B-A4B-it-UD-Q4_K_M.gguf \
        ~/.local/share/franziska/router-vocab.gguf      # llama-server: the GGUF file itself

Verified for gemma4:12b (2026-10-25): identical tokens and chat template to the full blob under llama.cpp vocab_only.
Re-export whenever the Ollama model is updated (the router checks nothing about versions).
"""

import sys
from pathlib import Path

from franziska.jev_ollama import ollama_model_blob


def export(src: str, dst: Path) -> None:
    from gguf import GGUFReader, GGUFValueType, GGUFWriter  # only for this one-off command

    reader = GGUFReader(src)
    writer = GGUFWriter(str(dst), reader.fields["general.architecture"].contents())
    for name, field in reader.fields.items():
        if name.startswith("GGUF.") or name == "general.architecture":
            continue  # written by GGUFWriter itself
        vtype = field.types[0]
        sub = field.types[-1] if vtype == GGUFValueType.ARRAY and len(field.types) > 1 else None
        writer.add_key_value(name, field.contents(), vtype, sub_type=sub)
    writer.write_header_to_file()
    writer.write_kv_data_to_file()
    writer.write_tensors_to_file()
    writer.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    model, out = sys.argv[1], Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    export(model if model.endswith(".gguf") else ollama_model_blob(model), out)
    print(f"{out}: {out.stat().st_size / 1e6:.1f} MB")
