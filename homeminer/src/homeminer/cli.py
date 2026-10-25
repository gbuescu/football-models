"""Command-line entry point: ``python -m homeminer <command>``."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import __version__, discovery, ledger, routes
from .benchmark import sha256_reference
from .benchmark.results import ResultStore
from .benchmark.runner import BenchmarkRunner
from .config import ConfigError, load_config
from .market import snapshot
from .miners import install, registry
from .process import ProcessSupervisor, sweep_tools_processes
from .redact import configure_logging
from .wallet import validate_btc_address


def _config(args):
    try:
        return load_config(Path(args.config) if args.config else None)
    except ConfigError as exc:
        sys.exit(f"Configuration problem: {exc}")


def _load_discovery(cfg) -> dict | None:
    try:
        return json.loads((cfg.data_dir / "discovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_discover(args) -> int:
    cfg = _config(args)
    report = discovery.collect(tools_status=registry.tools_status(cfg))
    path = cfg.data_dir / "discovery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) if args.json else discovery.to_text(report),
                                  encoding="utf-8")
    print(json.dumps(report, indent=2) if args.json else discovery.to_text(report))
    return 0


def cmd_benchmark(args) -> int:
    cfg = _config(args)
    report = _load_discovery(cfg)
    if report is None:
        print("No discovery report found; running read-only discovery first.")
        report = discovery.collect(tools_status=registry.tools_status(cfg))
    runner = BenchmarkRunner(cfg, ProcessSupervisor(cfg.tools_dir), discovery=report)
    plan = runner.plan(args.device, algorithm=args.algo, size=args.size)
    print(f"{args.device.upper()} benchmark plan: {plan.miner} {plan.miner_version}, {plan.algorithm}, {plan.size} hashes")
    print("Command:", " ".join(plan.public_command()) or "(not built)")
    for warning in plan.warnings:
        print("  warning:", warning)
    for blocker in plan.blockers:
        print("  BLOCKED:", blocker)
    if args.dry_run:
        print("Dry run: nothing was launched.")
        return 1 if plan.blockers else 0
    if plan.blockers:
        return 2
    if not args.yes:
        answer = input(f"Start the {args.device.upper()} benchmark now (offline, no pool)? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1
    print("Running. Press Ctrl+C to stop.")
    try:
        result = runner.run(plan)
    except KeyboardInterrupt:
        runner.supervisor.emergency_stop()
        print("Stopped.")
        return 130
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status == "completed" else 3


def cmd_market(args) -> int:
    cfg = _config(args)
    snap = snapshot.refresh(cfg)
    snapshot.save(cfg, snap)
    print(json.dumps(snap.to_dict(), indent=2))
    return 0 if not snap.errors else 1


def cmd_estimate(args) -> int:
    cfg = _config(args)
    if args.electricity is not None:
        cfg.data["economics"]["electricity_eur_per_kwh"] = args.electricity
    snap = snapshot.load_cached(cfg)
    out = routes.compare(cfg, snap, ResultStore(cfg.data_dir))
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_validate(args) -> int:
    address = args.address or getpass.getpass("BTC receiving address (input hidden): ")
    check = validate_btc_address(address)
    print(json.dumps(check.to_dict(), indent=2))
    return 0 if check.valid else 1


def cmd_install(args) -> int:
    cfg = _config(args)
    release = registry.releases(cfg)[args.miner]
    print(f"About to download {release.name} {release.version} ({release.asset})")
    print(f"  from {release.asset_url}")
    print("  and verify it against the release's SHA256SUMS (and GPG signature if gpg is installed).")
    print("This installs third-party mining software. Windows Defender may flag it; HomeMiner will not change Defender.")
    if not args.approve:
        print("Not approved. Re-run with --approve to install.")
        return 1
    try:
        report = install.install(cfg, release, approved=True)
    except install.InstallError as exc:
        print(f"Install refused: {exc}")
        return 2
    print(json.dumps(report.__dict__, indent=2))
    return 0


def cmd_ui(args) -> int:
    from .ui.server import serve  # noqa: PLC0415

    serve(_config(args), port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_stop(args) -> int:
    cfg = _config(args)
    swept = sweep_tools_processes(cfg.tools_dir)
    print(f"Killed {len(swept)} miner process(es): {', '.join(swept) or 'none running'}")
    return 0


def cmd_ledger(args) -> int:
    cfg = _config(args)
    book = ledger.Ledger(cfg.data_dir)
    if args.kind:
        book.add(args.kind, args.amount, args.source or "", args.note or "")
    print(json.dumps(book.summary(), indent=2))
    return 0


def cmd_sha(args) -> int:
    print(json.dumps(sha256_reference.measure(args.seconds), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homeminer", description="Benchmark this PC and estimate BTC payouts honestly.")
    parser.add_argument("--version", action="version", version=f"HomeMiner {__version__}")
    parser.add_argument("--config", help="Path to a config file (default: config/homeminer.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="Read-only hardware and software report")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", help="Also write the report to this file")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("benchmark", help="Plan or run an offline benchmark")
    p.add_argument("device", choices=["cpu", "gpu"])
    p.add_argument("--dry-run", action="store_true", help="Check everything and print the command; launch nothing")
    p.add_argument("--algo", choices=["rx/0", "rx/2"])
    p.add_argument("--size", help="250K, 500K, 1M ... 10M")
    p.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("market", help="Fetch timestamped NiceHash prices, BTC difficulty and price")
    p.set_defaults(func=cmd_market)

    p = sub.add_parser("estimate", help="Compare payout routes using the latest results and cached market data")
    p.add_argument("--electricity", type=float, help="EUR per kWh for this run")
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("validate-address", help="Check a BTC receiving address (nothing is stored)")
    p.add_argument("address", nargs="?")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("install-miner", help="Download and verify an official miner release (needs --approve)")
    p.add_argument("miner", choices=["xmrig", "xmrig-cuda"])
    p.add_argument("--approve", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("ui", help="Open the local interface")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("emergency-stop", help="Kill every process running from the tools folder")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("ledger", help="Show or add ledger entries")
    p.add_argument("--kind", choices=sorted(ledger.KINDS))
    p.add_argument("--amount")
    p.add_argument("--source")
    p.add_argument("--note")
    p.set_defaults(func=cmd_ledger)

    p = sub.add_parser("sha256-reference", help="Educational Python SHA-256d timing (not a miner)")
    p.add_argument("--seconds", type=float, default=3.0)
    p.set_defaults(func=cmd_sha)

    args = parser.parse_args(argv)
    import logging  # noqa: PLC0415

    configure_logging(logging.DEBUG if args.verbose else logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
