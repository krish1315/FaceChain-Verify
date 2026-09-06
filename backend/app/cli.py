"""
CLI for the face-ID chain-verify pipeline.

Usage:
    python -m backend.app.cli run <image_path>
    python -m backend.app.cli verify <record_id> [--image <path>]
"""

import argparse
import sys
from pathlib import Path


def cmd_run(args: argparse.Namespace) -> int:
    """Run the full pipeline on a single image."""
    from backend.app.pipeline.pipeline import run_pipeline

    image_path = Path(args.image_path)
    if not image_path.exists():
        print(f"ERROR: image not found: {image_path}", file=sys.stderr)
        return 2

    result = run_pipeline(image_path)
    print()
    print(result.summary)
    return 0 if result.tx_hash else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify a record id against the chain and IPFS."""
    from backend.app.pipeline.verify import verify_record

    result = verify_record(
        record_id=args.record_id,
        original_image_path=args.image,
    )
    print()
    print(result.summary)
    return 0 if result.all_passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="faceid-chain-verify",
        description="Face ID chain-verify pipeline CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── run ──────────────────────────────────────────────────────────────
    p_run = subparsers.add_parser("run", help="Run the full pipeline on an image")
    p_run.add_argument("image_path", help="Path to the input image")
    p_run.set_defaults(func=cmd_run)

    # ── verify ───────────────────────────────────────────────────────────
    p_verify = subparsers.add_parser("verify", help="Verify a record by id")
    p_verify.add_argument("record_id", type=int, help="On-chain record id")
    p_verify.add_argument(
        "--image", type=str, default=None,
        help="Optional local image to compare face embedding against"
    )
    p_verify.set_defaults(func=cmd_verify)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
