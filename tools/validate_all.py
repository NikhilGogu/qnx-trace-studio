#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Regenerate the validation summary for every capture under one or more directories.

    source ~/qnx800/qnxsdp-env.sh          # puts traceprinter on PATH
    python3 tools/validate_all.py tests/data --out validation-summary.txt

For each .kev or .kev.gz file it runs two checks and prints one line per check:

  compare   traceprinter -f TRACE -o TRACE.txt, then tools/compare_traceprinter.py: events only on one
            side, argument values compared and mismatches, values not compared
  validate  tracestudio validate against the workload logs in NAME.logs, when they exist: logged
            requests and how many matched (the cancel workload has no validate support; a NAME.logs
            directory without CSV files counts as no logs)

The exit status is 1 when any check fails or traceprinter is missing; --skip-traceprinter runs only
the validate checks. The summary has counts only, no timing.
"""
import argparse
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402
from tracestudio.validate import validate  # noqa: E402

NO_VALIDATE = {"cancel"}     # its log format is checked by test_cancellation_matches_the_workload_log instead


def captures(dirs):
    for d in dirs:
        for top, _, files in sorted(os.walk(d)):
            for f in sorted(files):
                if f.endswith((".kev", ".kev.gz")):
                    yield os.path.join(top, f)


def plain_copy(path, tmp):
    """traceprinter reads only uncompressed files."""
    if not path.endswith(".gz"):
        return path
    out = os.path.join(tmp, os.path.basename(path)[:-3])
    with gzip.open(path) as f, open(out, "wb") as g:
        shutil.copyfileobj(f, g)
    return out


def compare(path, traceprinter, tmp):
    kev = plain_copy(path, tmp)
    txt = kev + ".txt"
    subprocess.run([traceprinter, "-f", kev, "-o", txt], check=True, capture_output=True)
    r = subprocess.run([sys.executable, os.path.join(HERE, "compare_traceprinter.py"), kev, txt],
                       capture_output=True, text=True)
    summary = json.loads(r.stdout.rsplit("json: ", 1)[1])
    lines = r.stdout.splitlines()
    events = next(ln for ln in lines if ln.startswith("events: "))
    one_side = next(ln for ln in lines if ln.startswith("only in trace-studio: "))
    compared = sum(summary["compared"].values())
    text = "%s; %s; argument values compared %d, mismatches %d (sign only %d); not compared %d" % (
        events, one_side, compared, summary["mismatches"], summary["sign_only"], sum(summary["tp_only"].values()))
    return r.returncode == 0, text


def check_logs(path):
    logs = path[:-len(".gz")] if path.endswith(".gz") else path
    logs = logs[:-len(".kev")] + ".logs"
    name = os.path.basename(logs)[:-len(".logs")]
    if not os.path.isdir(logs) or name in NO_VALIDATE:
        return None
    if not any(f.endswith(".csv") for f in os.listdir(logs)):
        return None      # run_scenarios.sh makes NAME.logs for every scenario, empty for rm_traffic and rm_small
    res, _, _ = validate(Model(KevFile(path)), logs)
    return res["ok"], "logged requests %d, matched %d, failed checks %s" % (
        res["requests"], res["matched"], ", ".join(res["failed_checks"]) or "none")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+", help="directories with captures (searched recursively)")
    ap.add_argument("--out", help="also write the summary to this file")
    ap.add_argument("--skip-traceprinter", action="store_true", help="run only the validate checks")
    a = ap.parse_args(argv)
    traceprinter = None if a.skip_traceprinter else shutil.which("traceprinter")
    lines, ok = [], True
    if not a.skip_traceprinter and not traceprinter:
        lines.append("error: traceprinter not on PATH (source qnxsdp-env.sh), compare checks not run")
        ok = False
    found = list(captures(a.dirs))
    with tempfile.TemporaryDirectory(prefix="ts-validate-") as tmp:
        for path in found:
            results = []
            if traceprinter:
                results.append(("compare", compare(path, traceprinter, tmp)))
            v = check_logs(path)
            if v:
                results.append(("validate", v))
            for kind, (passed, text) in results:
                ok &= passed
                lines.append("%s %-8s %s: %s" % ("PASS" if passed else "FAIL", kind, path, text))
    lines.append("%d captures, %s" % (len(found), "all checks passed" if ok else "some checks failed"))
    print("\n".join(lines))
    if a.out:
        with open(a.out, "w") as f:
            f.write("\n".join(lines) + "\n")
    return 0 if ok and found else 1


if __name__ == "__main__":
    sys.exit(main())
