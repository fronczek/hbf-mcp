"""Stable JSON-emitting CLI; writes require an explicit --apply."""
from __future__ import annotations

import argparse
import json
import sys

from . import dsh
from .gitea import Gitea
from .youtrack import YouTrack


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hbf-api", description="Typed HBF API operations")
    groups = p.add_subparsers(dest="service", required=True)
    yt = groups.add_parser("youtrack", help="YouTrack issue operations").add_subparsers(dest="op", required=True)
    s = yt.add_parser("search"); s.add_argument("query", nargs="?", default=""); s.add_argument("--limit", type=int, default=15); s.add_argument("--skip", type=int, default=0)
    i = yt.add_parser("issue"); i.add_argument("issue_id")
    c = yt.add_parser("comments"); c.add_argument("issue_id"); c.add_argument("--limit", type=int, default=20)
    a = yt.add_parser("add-comment", help="read comment text from stdin")
    a.add_argument("issue_id"); a.add_argument("--apply", action="store_true", help="confirm external write")

    gt = groups.add_parser("gitea", help="Gitea repository operations").add_subparsers(dest="op", required=True)
    gt.add_parser("connection")
    b = gt.add_parser("branches"); b.add_argument("repo"); b.add_argument("--page", type=int, default=1); b.add_argument("--limit", type=int, default=30)
    pr = gt.add_parser("pulls"); pr.add_argument("repo"); pr.add_argument("--state", choices=("open", "closed", "all"), default="open"); pr.add_argument("--page", type=int, default=1); pr.add_argument("--limit", type=int, default=20)
    one = gt.add_parser("pull"); one.add_argument("repo"); one.add_argument("number", type=int)
    diff = gt.add_parser("diff"); diff.add_argument("repo"); diff.add_argument("number", type=int)
    files = gt.add_parser("files"); files.add_argument("repo"); files.add_argument("number", type=int); files.add_argument("--page", type=int, default=1); files.add_argument("--limit", type=int, default=30)
    st = gt.add_parser("status"); st.add_argument("repo"); st.add_argument("ref")
    create = gt.add_parser("create-pr", help="create a PR (body from stdin)")
    create.add_argument("repo"); create.add_argument("--title", required=True)
    create.add_argument("--head", required=True); create.add_argument("--base", default="master")
    create.add_argument("--apply", action="store_true", help="confirm external write")

    ds = groups.add_parser("dsh", help="Bounded DeepSeek Harness operations").add_subparsers(dest="op", required=True)
    sessions = ds.add_parser("sessions"); sessions.add_argument("--limit", type=int, default=10)
    flash = ds.add_parser("flash", help="Dispatch a budget-capped task (task text from stdin)")
    flash.add_argument("issue_id"); flash.add_argument("--workdir", required=True, help="absolute Harness workspace path")
    flash.add_argument("--max-usd", type=float, default=1.0)
    flash.add_argument("--timeout", type=int, default=180); flash.add_argument("--apply", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.service == "youtrack":
            if args.op == "add-comment" and not args.apply:
                raise ValueError("Write not sent; repeat with --apply to confirm")
            api = YouTrack()
            if args.op == "search": result = api.search(args.query, args.limit, args.skip)
            elif args.op == "issue": result = api.issue(args.issue_id)
            elif args.op == "comments": result = api.comments(args.issue_id, args.limit)
            elif args.op == "add-comment": result = api.add_comment(args.issue_id, sys.stdin.read(10_001))
        elif args.service == "gitea":
            if args.op == "create-pr" and not args.apply:
                raise ValueError("Write not sent; repeat with --apply to confirm")
            api = Gitea()
            if args.op == "connection": result = api.connection()
            elif args.op == "branches": result = api.branches(args.repo, args.page, args.limit)
            elif args.op == "pulls": result = api.pull_requests(args.repo, args.state, args.page, args.limit)
            elif args.op == "pull": result = api.pull_request(args.repo, args.number)
            elif args.op == "diff": result = api.pull_diff(args.repo, args.number)
            elif args.op == "files": result = api.pull_files(args.repo, args.number, args.page, args.limit)
            elif args.op == "status": result = api.commit_status(args.repo, args.ref)
            elif args.op == "create-pr":
                result = api.create_pull_request(args.repo, args.title, args.head, args.base,
                                                 sys.stdin.read(20_001))
        else:
            if args.op == "sessions": result = dsh.sessions(args.limit)
            elif args.op == "flash":
                if not args.apply:
                    raise ValueError("Dispatch not sent; repeat with --apply to confirm")
                task = sys.stdin.read(8_001)
                result = dsh.flash_task(args.issue_id, task, workdir=args.workdir, max_usd=args.max_usd,
                                        timeout_seconds=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (RuntimeError, ValueError) as exc:
        print(f"hbf-api: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
