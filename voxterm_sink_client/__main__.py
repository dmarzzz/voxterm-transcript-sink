from __future__ import annotations

import argparse
import json
import sys

from .identity import load_or_create_author
from .trust import TrustStore
from .upload import collect_markdown_paths, upload_files
from .verify import VerificationError, normalize_sink_url, verify_sink


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voxterm-sink-upload")
    sub = parser.add_subparsers(dest="command", required=True)

    verify_p = sub.add_parser("verify")
    verify_p.add_argument("--sink-url", required=True)

    upload_p = sub.add_parser("upload")
    upload_p.add_argument("paths", nargs="+", metavar="PATH")
    upload_p.add_argument("--sink-url", required=True)
    upload_p.add_argument("--hivemind-id", required=True)
    upload_p.add_argument("--recursive", action="store_true")
    upload_p.add_argument("--tag", action="append", default=[])
    upload_p.add_argument("--dry-run", action="store_true")
    upload_p.add_argument("--json", action="store_true", dest="json_output")

    trust_p = sub.add_parser("trust")
    trust_sub = trust_p.add_subparsers(dest="trust_command", required=True)
    trust_sub.add_parser("inspect")
    reset_p = trust_sub.add_parser("reset")
    reset_p.add_argument("--sink-url", required=True)

    args = parser.parse_args(argv)

    try:
        if args.command == "verify":
            sink_url, verified, _ = verify_sink(args.sink_url)
            print(f"verified sink {verified['sink_sig_pubkey']} at {sink_url}")
            return 0

        if args.command == "upload":
            sink_url, verified, info = verify_sink(args.sink_url)
            paths = collect_markdown_paths(args.paths, args.recursive)
            author = load_or_create_author()
            uploaded, failed = upload_files(
                paths,
                sink_url=sink_url,
                sink_info=info,
                sink_pubkey=verified["sink_sig_pubkey"],
                hivemind_id=args.hivemind_id,
                tags=args.tag,
                author=author,
                dry_run=args.dry_run,
            )
            if args.json_output:
                print(
                    json.dumps(
                        {
                            "sink_url": sink_url,
                            "verified": True,
                            "uploaded": [r.public_dict() for r in uploaded],
                            "failed": [r.public_dict() for r in failed],
                        },
                        separators=(",", ":"),
                    )
                )
            else:
                print(f"verified sink {verified['sink_sig_pubkey']}")
                for result in uploaded:
                    print(f"uploaded {result.path} id={result.id} status={result.status}")
                for result in failed:
                    print(f"failed {result.path}: {result.error}", file=sys.stderr)
            return 1 if failed else 0

        if args.command == "trust" and args.trust_command == "inspect":
            print(json.dumps(TrustStore().inspect_public(), indent=2, sort_keys=True))
            return 0

        if args.command == "trust" and args.trust_command == "reset":
            sink_url = normalize_sink_url(args.sink_url)
            removed = TrustStore().reset_url(sink_url)
            print(f"removed trust for {sink_url}" if removed else f"no trust record for {sink_url}")
            return 0
    except (OSError, ValueError, VerificationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error("unhandled command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
