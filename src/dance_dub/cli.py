"""Dance Dub CLI — subcommands per pipeline stage plus `all`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dance_dub import pipeline as P

STAGE_NAMES = ["download", "extract-audio", "transcribe", "translate", "burn"]


def workdir_from(args) -> Path:
    if getattr(args, "input", None):
        return Path(args.input).expanduser().resolve()
    if getattr(args, "url", None):
        out_root = Path(args.output).expanduser().resolve() if args.output else Path("output")
        return out_root / P.slugify(args.url)
    raise SystemExit("error: provide --url (for download/all) or --input <folder> (for other stages)")


def cmd_download(args) -> int:
    P.download_video(args.url, workdir_from(args), log=print)
    return 0


def cmd_extract(args) -> int:
    P.extract_audio(workdir_from(args), log=print)
    return 0


def cmd_transcribe(args) -> int:
    srt = P.transcribe_audio(
        workdir_from(args), model_size=args.model, log=print
    )
    txt = srt.with_suffix(".txt")
    if txt.exists():
        print(f"\nTranscript preview:\n{'-' * 40}")
        print(txt.read_text(encoding="utf-8")[:800])
    return 0


def cmd_translate(args) -> int:
    wd = workdir_from(args)
    srt = wd / "transcript.srt"
    if not srt.exists():
        raise SystemExit(f"error: {srt} not found — run transcribe first")
    P.translate_srt_file(
        srt,
        target_lang=args.target_lang,
        source_lang=args.source_lang,
        log=print,
    )
    return 0


def cmd_burn(args) -> int:
    P.burn_subtitles(workdir_from(args), log=print)
    return 0


def cmd_all(args) -> int:
    out_root = Path(args.output).expanduser().resolve() if args.output else Path("output")
    final = P.run_all(
        args.url,
        out_root,
        target_lang=args.target_lang,
        source_lang=args.source_lang,
        model_size=args.model,
        log=print,
    )
    print(f"\nDone! Subtitled video: {final}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dance-dub",
        description="Download Wix videos, transcribe, translate, and burn subtitles.",
    )
    parser.add_argument(
        "--version", action="store_true", help="print version and exit"
    )
    sub = parser.add_subparsers(dest="command")

    def add_url_options(sp, need_url: bool):
        sp.add_argument("--url", required=need_url, help="video page or direct video URL")
        sp.add_argument("-o", "--output", default="output", help="output root folder (default: ./output)")
        sp.add_argument("--input", help="existing per-video work folder (for stages 2-5)")

    def add_lang_options(sp):
        sp.add_argument("--target-lang", default="en", help="translation target language (default: en)")
        sp.add_argument("--source-lang", default="auto", help="source language (default: auto-detect)")

    def add_model_option(sp):
        sp.add_argument(
            "--model", default="small", choices=["tiny", "base", "small", "medium"],
            help="Whisper model size (default: small)",
        )

    p = sub.add_parser("download", help="download a video from a URL")
    add_url_options(p, need_url=True)
    p.set_defaults(func=cmd_download)

    p = sub.add_parser("extract-audio", help="extract audio.wav from the downloaded video")
    add_url_options(p, need_url=False)
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("transcribe", help="transcribe audio.wav to transcript.srt/.txt")
    add_url_options(p, need_url=False)
    add_model_option(p)
    p.set_defaults(func=cmd_transcribe)

    p = sub.add_parser("translate", help="translate transcript.srt")
    add_url_options(p, need_url=False)
    add_lang_options(p)
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("burn", help="burn subtitles into a copy of the video")
    add_url_options(p, need_url=False)
    p.set_defaults(func=cmd_burn)

    p = sub.add_parser("all", help="run the full pipeline (skips stages already done)")
    add_url_options(p, need_url=True)
    add_lang_options(p)
    add_model_option(p)
    p.set_defaults(func=cmd_all)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False) and not getattr(args, "command", None):
        from dance_dub import __version__

        print(__version__)
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
