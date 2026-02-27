#!/usr/bin/env python3
"""CLI wrapper for generating StyleTTS2 audio from text + speaker id."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from styletts2_engine import StyleTTS2Engine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize speech from text and speaker id (0/1) using StyleTTS2.",
    )
    parser.add_argument("text", help="Text to synthesize.")
    parser.add_argument("speaker", type=int, choices=[0, 1], help="Speaker id (0 or 1).")
    parser.add_argument(
        "--preset",
        choices=["default", "less", "more"],
        default="default",
        help="Inference preset (default: %(default)s).",
    )
    parser.add_argument(
        "--num-refs",
        type=int,
        default=3,
        help="Number of reference wav files to average (default: %(default)s).",
    )
    parser.add_argument(
        "--refs-root",
        type=Path,
        default=None,
        help="Directory containing speaker folders '0' and '1'. Defaults to StyleTTS2 project root.",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=None,
        help="Path to StyleTTS2 codebase (defaults to STYLE_TTS2_DIR env or auto-detect).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output m4a path (default: <project_root>/synth_speaker_<id>.m4a).",
    )
    parser.add_argument(
        "--trim-in-ms",
        type=int,
        default=0,
        help="Trim duration removed from the beginning (ms).",
    )
    parser.add_argument(
        "--trim-out-ms",
        type=int,
        default=200,
        help="Trim duration removed from the end (ms).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model_dir = args.model_dir.expanduser().resolve() if args.model_dir else None
    refs_root = args.refs_root.expanduser().resolve() if args.refs_root else None

    engine = StyleTTS2Engine(base_dir=model_dir, refs_root=refs_root)
    output_path = args.output
    if output_path is None:
        default_root = engine.project_root
        output_path = default_root / f"synth_speaker_{args.speaker}.m4a"
    result = engine.synthesize_to_file(
        text=args.text,
        speaker=args.speaker,
        preset=args.preset,
        num_refs=args.num_refs,
        trim_in_ms=args.trim_in_ms,
        trim_out_ms=args.trim_out_ms,
        output_path=output_path,
    )

    print(f"Using device: {engine.device}")
    print(f"Using speaker: {args.speaker}")
    print("Reference files:")
    for path in result.refs_used:
        print(f"  - {path}")
    print(f"Preset: {result.preset}")
    print(f"RTF: {result.rtf:.5f}")
    print(
        f"Post-process: trim_in={args.trim_in_ms}ms, trim_out={args.trim_out_ms}ms, "
        "fade=200ms each side"
    )
    print(f"Saved: {result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
