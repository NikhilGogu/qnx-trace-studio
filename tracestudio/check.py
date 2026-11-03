# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""tracestudio check: real-time assertions over one trace, for CI.

Every assertion uses the analyses behind `analyze` and counts trustworthy exchanges only. A trace that
lost events, or an assertion whose requests include excluded or unfinished ones, fails unless the user
allows it (--allow-loss); an assertion with no trustworthy request to check always fails, so nothing
passes silently. Results can be written as JUnit XML (one testcase per assertion).
"""
from __future__ import annotations

import os
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .analyses import suspected_inversions, us


@dataclass
class Result:
    name: str
    ok: bool
    summary: str
    evidence: list = field(default_factory=list)
    kind: str = "check"          # JUnit failure type


def _latency_assertion(model, name, budget_us, match, allow_loss):
    """All trustworthy requests of the client threads `match` accepts must finish their MsgSend within budget."""
    seen = excluded = unfinished = 0
    misses, worst = [], None
    for t in model.txns:
        if not match(t.client):
            continue
        if t.t_call is None or t.t_exit is None:
            unfinished += 1
            continue
        if not t.trustworthy:
            excluded += 1
            continue
        seen += 1
        lat = us(model, t.t_exit - t.t_call)
        worst = lat if worst is None else max(worst, lat)
        if lat > budget_us:
            misses.append((lat, t))
    misses.sort(key=lambda x: -x[0])
    untrusted = "%d excluded as untrustworthy, %d unfinished" % (excluded, unfinished)
    if not seen:
        return Result(name, False, "no trustworthy request to check (%s)" % untrusted, kind="NoData")
    ev = ["txn %d: %s -> %s, %.1f us (budget %g us)" % (t.idx, model.label(t.client), model.server_name(t), lat,
                                                         budget_us) for lat, t in misses[:5]]
    if len(misses) > 5:
        ev.append("... %d more" % (len(misses) - 5))
    head = "%d of %d trustworthy requests over %g us, max %.1f us" % (len(misses), seen, budget_us, worst)
    if misses:
        return Result(name, False, head + ("; %s" % untrusted if excluded or unfinished else ""), ev, "DeadlineMissed")
    if (excluded or unfinished) and not allow_loss:
        return Result(name, False, head + "; %s, so not every request was checked (--allow-loss accepts this)"
                      % untrusted, kind="Untrustworthy")
    return Result(name, True, head + ("; %s, allowed by --allow-loss" % untrusted if excluded or unfinished else ""))


def run_checks(model, tl, a):
    """[Result] for the assertions in the parsed arguments `a` (see cli.py, check)."""
    out = []
    n, good = len(model.txns), model.counters["txns_trustworthy"]
    losses = model.kev.losses
    name = "trace integrity"
    if not good:
        out.append(Result(name, False, "no trustworthy message exchange in %d" % n, kind="NoData"))
    elif losses and not a.allow_loss:
        out.append(Result(name, False, "%d loss records (lost events make exchanges untrustworthy); "
                          "--allow-loss accepts this" % len(losses),
                          ["CPU %d: %s %s" % (l.cpu, l.kind, l.detail) for l in losses[:5]], "LostEvents"))
    else:
        out.append(Result(name, True, "%s, %d of %d message exchanges trustworthy" % (
            "%d loss records, allowed by --allow-loss" % len(losses) if losses else "no lost events", good, n)))
    for thread, dl in a.deadline or []:
        out.append(_latency_assertion(
            model, "deadline %s=%g us" % (thread, dl), dl,
            lambda key, th=thread: (model.threads.get(key) is not None and model.threads[key].name == th),
            a.allow_loss))
    for client, dl in a.max_latency or []:
        out.append(_latency_assertion(model, "max latency %s=%g us" % (client, dl), dl,
                                      lambda key, c=client: c in model.label(key), a.allow_loss))
    inv = None
    if a.no_inversions or a.max_insufficient is not None:
        inv = suspected_inversions(model, tl=tl, min_us=a.min_us)
    if a.no_inversions:
        sus = inv["suspected"]
        head = "%d suspected (checked %d exchanges, %d excluded as untrustworthy, %d with an ambiguous parent)" % (
            len(sus), inv["checked"], inv["excluded_untrustworthy"], inv["excluded_ambiguous_parent"])
        if inv["suspected_using_assumptions"]:
            head += ", %d relying on supplied run masks" % inv["suspected_using_assumptions"]
        ev = ["txn %d: %s" % (r["txn"], r["evidence"]) for r in sus[:3]]
        if len(sus) > 3:
            ev.append("... %d more" % (len(sus) - 3))
        out.append(Result("no suspected priority inversions", not sus, head, ev, "PriorityInversion"))
    if a.max_insufficient is not None:
        ins = inv["insufficient_evidence"]
        ev = ["txn %d: %s -> %s, %.1f us unexplained: %s" % (
            r["txn"], r["client"], r["server"], r["unexplained_us"], next(iter(r["reasons"]), "")) for r in ins[:5]]
        out.append(Result("at most %d with insufficient evidence" % a.max_insufficient, len(ins) <= a.max_insufficient,
                          "%d requests with insufficient evidence for the inversion analysis" % len(ins),
                          ev if len(ins) > a.max_insufficient else [], "InsufficientEvidence"))
    return out


def render(results):
    lines = []
    for r in results:
        lines.append("%s %s: %s" % ("PASS" if r.ok else "FAIL", r.name, r.summary))
        if not r.ok:
            lines.extend("  " + e for e in r.evidence)
    failed = sum(1 for r in results if not r.ok)
    lines.append("check: %d assertions, %d failed" % (len(results), failed))
    return lines


def junit_xml(results, trace, seconds, version):
    """A JUnit XML report: testsuites > testsuite > testcase (with failure for each failed assertion)."""
    failed = sum(1 for r in results if not r.ok)
    stem = os.path.basename(trace)
    for ext in (".gz", ".kev"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
    root = ET.Element("testsuites", name="tracestudio check", tests=str(len(results)), failures=str(failed),
                      errors="0", time="%.3f" % seconds)
    suite = ET.SubElement(root, "testsuite", name="tracestudio check %s" % stem, tests=str(len(results)),
                          failures=str(failed), errors="0", skipped="0", time="%.3f" % seconds,
                          timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"))
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", name="trace", value=trace)
    ET.SubElement(props, "property", name="tracestudio", value=version)
    for r in results:
        case = ET.SubElement(suite, "testcase", classname="tracestudio.check.%s" % stem, name=r.name, time="0")
        if r.ok:
            ET.SubElement(case, "system-out").text = r.summary
        else:
            f = ET.SubElement(case, "failure", message=r.summary, type=r.kind)
            f.text = "\n".join([r.summary] + r.evidence)
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"
