"""Command line: run the bot, or look up prices and scraper health without Discord.

    python -m gpu_alerts [run]
    python -m gpu_alerts lookup "rtx 5070 ti" [--open-box] [--marketplace] [--top N]
    python -m gpu_alerts health [--query "rtx 5080"]
    python -m gpu_alerts import-v1 gpu_alerts.json [--force]
"""

import argparse
import logging
import sys

from . import catalog, config, pricing
from .retailers.base import Status


def _table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*(str(c) for c in r)) for r in rows]
    return "\n".join(lines)


def _name(rid: str) -> str:
    from . import retailers

    return retailers.display_name(rid)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number of 1 or more")
    return value


def cmd_lookup(args: argparse.Namespace) -> int:
    model = catalog.by_id(args.gpu) or catalog.resolve(args.gpu)
    if model is None:
        hits = catalog.search(args.gpu, limit=10)
        print(f"Unknown GPU {args.gpu!r}.", file=sys.stderr)
        if hits:
            print("Did you mean: " + ", ".join(f"{m.name} ({m.id})" for m in hits), file=sys.stderr)
        return 2
    policy = pricing.Policy(include_open_box=args.open_box, include_marketplace=args.marketplace)
    print(f"Searching {model.query!r} for {model.name} (min ${model.min_price:,.0f})...")
    results = pricing.scan(model.query)
    found = pricing.offers(model, results, policy)
    per = pricing.best_per_retailer(found)
    rows = [
        [_name(r.retailer), Status(r.status).value, r.http_status or "", r.n_raw, len(r.listings),
         pricing.status_label(r, per.get(r.retailer), model), r.detail[:60]]
        for r in results
    ]
    print(_table(rows, ["retailer", "status", "http", "raw", "listings", "best eligible", "detail"]))
    print()
    if not any(r.ok for r in results):
        # Not a market state: exit 1 as `health` does, so a script can tell.
        print("No retailer answered (blocked, errors or all disabled), so there are no prices.")
        return 1
    if not found:
        print("No eligible offers.")
        return 0
    print(f"Top {min(args.top, len(found))} of {len(found)} eligible offers:")
    for i, li in enumerate(found[: args.top], 1):
        flags = [li.condition] if li.condition != "new" else []
        if li.first_party is False:
            flags.append(f"seller: {li.seller or 'marketplace'}")
        if li.in_stock is None:
            flags.append("stock unknown")
        extra = f" [{', '.join(flags)}]" if flags else ""
        print(f"{i:>2}. ${li.price:>9,.2f}  {_name(li.retailer):<18} {li.title[:90]}{extra}")
        print(f"    {li.url}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    print(f"Scanning {args.query!r} on every retailer...")
    results = pricing.scan(args.query)
    rows = [
        [_name(r.retailer), Status(r.status).value, r.http_status or "", r.n_raw, len(r.listings),
         f"{r.elapsed_ms / 1000:.1f} s", r.detail[:70]]
        for r in results
    ]
    print(_table(rows, ["retailer", "status", "http", "raw", "listings", "elapsed", "detail"]))
    failed = [r for r in results if not r.ok and Status(r.status) is not Status.DISABLED]
    return 1 if failed else 0


def cmd_import_v1(args: argparse.Namespace) -> int:
    from .store import Store

    with Store(config.DB_PATH) as store:
        report = store.import_v1(args.path, force=args.force)
    for line in report.imported:
        print(f"imported {line}")
    for line in report.updated:
        print(f"updated  {line}")
    for line in report.skipped:
        print(f"skipped  {line}")
    print(
        f"{len(report.imported)} imported, {len(report.updated)} updated, {len(report.skipped)} skipped,"
        f" into {config.DB_PATH}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .bot import run

    run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gpu_alerts", description="GPU price alerts for Canadian retailers")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="start the Discord bot (default)").set_defaults(func=cmd_run)
    p = sub.add_parser("lookup", help="live prices for one GPU, no Discord needed")
    p.add_argument("gpu", help='catalog id or name, e.g. "rtx-5070-ti" or "RTX 5070 Ti"')
    p.add_argument("--open-box", action="store_true", help="count open-box and refurbished listings")
    p.add_argument("--marketplace", action="store_true", help="count third-party marketplace sellers")
    p.add_argument("--top", type=_positive_int, default=10, help="how many offers to list (default 10)")
    p.set_defaults(func=cmd_lookup)
    p = sub.add_parser("health", help="scan one query on every retailer and report status")
    p.add_argument("--query", default="rtx 5080", help='search string (default "rtx 5080")')
    p.set_defaults(func=cmd_health)
    p = sub.add_parser("import-v1", help="import alerts from the v1 gpu_alerts.json")
    p.add_argument("path")
    p.add_argument(
        "--force", action="store_true",
        help="also set existing alerts to the file's target and channel (whether they already pinged stays)",
    )
    p.set_defaults(func=cmd_import_v1)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if getattr(args, "func", None) is None:
        args.func = cmd_run
    if args.func is not cmd_run:   # run() sets up its own logging
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
        config.warn_unknown_retailers()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
