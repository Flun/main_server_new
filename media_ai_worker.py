import argparse
import json
import logging
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / ".media_ai_packages"))


def main():
    parser = argparse.ArgumentParser(description="Media Studio AI voice isolation worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-dir", required=True)
    args = parser.parse_args()

    from audio_separator.separator import Separator

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    separator = Separator(
        log_level=logging.INFO,
        model_file_dir=str(Path(args.model_dir).resolve()),
        output_dir=str(output_dir),
        output_format="WAV",
        output_single_stem="Vocals",
        use_autocast=True,
        use_soundfile=True,
        chunk_duration=600,
    )
    separator.load_model(model_filename="model_bs_roformer_ep_317_sdr_12.9755.ckpt")
    outputs = separator.separate(str(Path(args.input).resolve()), custom_output_names={"Vocals": "isolated_voice"})
    candidates = []
    for value in outputs or []:
        path = Path(value)
        candidates.append(path if path.is_absolute() else output_dir / path)
    result = next((path for path in candidates if path.is_file()), None)
    if result is None:
        result = next(iter(sorted(output_dir.glob("*.wav"))), None)
    if result is None:
        raise RuntimeError(f"Voice separator returned no output: {json.dumps(outputs, ensure_ascii=False)}")
    print(f"MEDIA_AI_RESULT={result.resolve()}")


if __name__ == "__main__":
    main()
