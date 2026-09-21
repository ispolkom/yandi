"""python -m contract.runner <command>

  validate                      check schemas, fixtures, leak patterns and coverage.json (offline, no target)
  coverage                      print the traceability table
  list                          list fixtures
  run --target-file F.json      run the fixtures against an already running test core described in F.json
  run --target reference        run against the in-repo test double (proves the fixtures can be satisfied)
  run --target current-pet      run against a TEMPORARY copy of today's Python system (never the live one)
  run --target python-core-shell   the Python Core's lifecycle boundary, started as a separate process (fast)
  run --target python-core         the same boundary in front of the real PET application
  run --target none --only supervisor. --supervisor-harness CMD   the supervisor scenarios against a supervisor's harness
"""
from __future__ import annotations

import argparse
import json
import sys

from . import coverage
from .engine import run_suite
from .loader import ContractError, load_suite
from .report import line, summary, to_json
from .wire import RefusedTarget


def _suite():
    try:
        return load_suite()
    except ContractError as exc:
        print(f"CONTRACT FILES ARE NOT VALID: {exc}", file=sys.stderr)
        raise SystemExit(2)


def cmd_validate(_args) -> int:
    suite = _suite()
    problems = coverage.check(suite)
    if problems:
        print("COVERAGE DECLARATION IS NOT VALID:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    print(f"contract {suite.version}: {len(suite.schemas.by_name)} schemas, {len(suite.scenarios)} fixtures — all valid")
    return 0


def cmd_coverage(_args) -> int:
    suite = _suite()
    problems = coverage.check(suite)
    if problems:
        print("COVERAGE DECLARATION IS NOT VALID:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    print(coverage.render(suite))
    return 0


def cmd_list(_args) -> int:
    for sc in _suite().scenarios:
        need = f" requires={','.join(sc.requires)}" if sc.requires else ""
        print(f"{sc.status:<8}{sc.kind:<11}{sc.id}{need}")
    return 0


class _NoTarget:
    """No core to talk to: only for runs that use nothing but a supervisor harness (--only supervisor.)."""
    name = "no HTTP target"
    hooks: set = set()
    launch_secret = "0" * 64
    unlock_key = "A" * 43 + "="
    base_url = "http://127.0.0.1:1"

    def reset(self):
        raise RuntimeError("this run has no HTTP target")

    def close(self):
        pass


def cmd_run(args) -> int:
    suite = _suite()
    closer = None
    extra = None
    try:
        if args.target == "reference":
            from contract.selftest.reference_double import ReferenceTarget
            target = ReferenceTarget()
        elif args.target in ("python-core-shell", "python-core"):
            from contract.targets.python_core import PythonCoreTarget
            target = PythonCoreTarget("real" if args.target == "python-core" else "shell")
        elif args.target == "none":
            target = _NoTarget()
        elif args.target == "current-pet":
            from contract.selftest.current_pet import CurrentPetTarget
            target = CurrentPetTarget()
            extra = {"first_contact": target.first_contact}
        elif args.target_file:
            from .targets import ExternalTarget
            target = ExternalTarget.from_file(args.target_file)
        else:
            print("choose --target-file, --target reference or --target current-pet", file=sys.stderr)
            return 2
        closer = target.close
    except RefusedTarget as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    try:
        harness = None
        if args.supervisor_harness:
            from .supervisor import SupervisorHarness
            harness = SupervisorHarness(args.supervisor_harness)
        results = run_suite(suite, target, only=args.only, lenient=args.lenient, on_result=lambda r: print(line(r), flush=True), supervisor=harness)
    finally:
        if closer:
            closer()
    print("\n" + summary(results))
    if args.json:
        mode = "lenient (given-state checks skipped)" if args.lenient else "strict"
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(to_json(suite.version, target.name, mode, results, extra), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
    bad = [r for r in results if r.status in ("fail", "error")]
    return 1 if bad else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m contract.runner", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    sub.add_parser("coverage")
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("--target-file")
    r.add_argument("--target", choices=["reference", "current-pet", "python-core-shell", "python-core", "none"])
    r.add_argument("--supervisor-harness", help="command of a supervisor implementation's harness; runs the supervisor scenarios")
    r.add_argument("--only", action="append", help="run only fixtures whose id starts with this (repeatable)")
    r.add_argument("--lenient", action="store_true", help="skip the given-state checks so every fixture's own assertions run (used for the RED baseline)")
    r.add_argument("--json", help="write a JSON report here")
    args = p.parse_args(argv)
    return {"validate": cmd_validate, "coverage": cmd_coverage, "list": cmd_list, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
