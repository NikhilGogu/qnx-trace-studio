# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""tracestudio check: real-time assertions over one trace, for CI.

Every assertion uses the analyses behind `analyze` and counts trustworthy exchanges only. Nothing passes
for lack of data: a trace that lost events, or a latency assertion whose requests include excluded or
unfinished ones, fails unless the user allows it (--allow-loss); a latency assertion needs at least
--min-requests trustworthy requests, and an inversion assertion at least --min-checked exchanges that the
inversion analysis actually checked, with no more than --max-insufficient inconclusive ones. Results can be
written as JUnit XML (one testcase per assertion; an error testcase when the trace cannot be read).
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field

from .analyses import role_label, selector_matches, split_selector, suspected_inversions, thread_key, us


@dataclass
class Result:
    name: str
    ok: bool
    summary: str
    evidence: list = field(default_factory=list)
    kind: str = "check"          # JUnit failure type


def _latency_assertion(model, name, budget_us, match, allow_loss, min_requests):
    """All trustworthy requests that `match(tx)` selects must finish their MsgSend within budget; at least
    min_requests of them must exist."""
    seen = excluded = unfinished = 0
    misses, worst, roles = [], None, Counter()
    for t in model.txns:
        if not match(t):
            continue
        if t.t_call is None or t.t_exit is None:
            unfinished += 1
            continue
        if not t.trustworthy:
            excluded += 1
            continue
        seen += 1
        roles["%s -> %s" % (role_label(thread_key(model, t.client, t.client_prio)), model.server_name(t))] += 1
        lat = us(model, t.t_exit - t.t_call)
        worst = lat if worst is None else max(worst, lat)
        if lat > budget_us:
            misses.append((lat, t))
    misses.sort(key=lambda x: -x[0])
    untrusted = "%d excluded as untrustworthy, %d unfinished" % (excluded, unfinished)
    matched = "matched " + (", ".join("%s (%d)" % kv for kv in sorted(roles.items())) if roles else "no role")
    if seen < min_requests:
        return Result(name, False, "%d trustworthy requests, at least %d required (%s); %s" % (
            seen, min_requests, untrusted, matched), kind="NoData")
    ev = ["txn %d: %s -> %s, %.1f us (budget %g us)" % (t.idx, model.label(t.client), model.server_name(t), lat,
                                                         budget_us) for lat, t in misses[:5]]
    if len(misses) > 5:
        ev.append("... %d more" % (len(misses) - 5))
    head = "%d of %d trustworthy requests over %g us, max %.1f us; %s" % (len(misses), seen, budget_us, worst, matched)
    if misses:
        return Result(name, False, head + ("; %s" % untrusted if excluded or unfinished else ""), ev, "DeadlineMissed")
    if (excluded or unfinished) and not allow_loss:
        return Result(name, False, head + "; %s, so not every request was checked (--allow-loss accepts this)"
                      % untrusted, kind="Untrustworthy")
    return Result(name, True, head + ("; %s, allowed by --allow-loss" % untrusted if excluded or unfinished else ""))


def _thread_selector(model, selector):
    def match(t):
        th = model.threads.get(t.client)
        return th is not None and selector_matches(model, selector, th.name, t)
    return match


def _label_selector(model, selector):
    client, server = split_selector(selector)
    return lambda t: client in model.label(t.client) and (server is None or model.server_name(t) == server)


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
    for selector, dl in a.deadline or []:
        out.append(_latency_assertion(model, "deadline %s=%g us" % (selector, dl), dl,
                                      _thread_selector(model, selector), a.allow_loss, a.min_requests))
    for selector, dl in a.max_latency or []:
        out.append(_latency_assertion(model, "max latency %s=%g us" % (selector, dl), dl,
                                      _label_selector(model, selector), a.allow_loss, a.min_requests))
    if not (a.no_inversions or a.max_insufficient is not None):
        return out
    client = getattr(a, "client", None)
    only = (lambda key: client in model.label(key)) if client else None
    inv = suspected_inversions(model, tl=tl, min_us=a.min_us, only=only)
    sus, ins, checked = inv["suspected"], inv["insufficient_evidence"], inv["checked"]
    allowed = a.max_insufficient or 0
    coverage = "checked %d exchanges%s (at least %d required), %d with insufficient evidence (at most %d allowed), " \
               "%d excluded as untrustworthy, %d with an ambiguous parent" % (
                   checked, " of clients matching %r" % client if client else "", a.min_checked, len(ins), allowed,
                   inv["excluded_untrustworthy"], inv["excluded_ambiguous_parent"])
    ins_ev = ["txn %d: %s -> %s, %.1f us unexplained: %s" % (
        r["txn"], r["client"], r["server"], r["unexplained_us"], next(iter(r["reasons"]), "")) for r in ins[:5]]
    if a.no_inversions:
        head = "%d suspected; %s" % (len(sus), coverage)
        if inv["suspected_using_assumptions"]:
            head += "; %d relying on supplied run masks" % inv["suspected_using_assumptions"]
        ev = ["txn %d: %s" % (r["txn"], r["evidence"]) for r in sus[:3]]
        if len(sus) > 3:
            ev.append("... %d more" % (len(sus) - 3))
        if sus:
            kind = "PriorityInversion"
        elif checked < a.min_checked:
            kind = "NoData"
        elif len(ins) > allowed:
            kind, ev = "InsufficientEvidence", ins_ev
        else:
            kind = None
        out.append(Result("no suspected priority inversions", kind is None, head, ev, kind or "check"))
    else:
        ok = checked >= a.min_checked and len(ins) <= allowed
        out.append(Result("at most %d with insufficient evidence" % allowed, ok, coverage,
                          [] if ok else ins_ev, "InsufficientEvidence" if checked >= a.min_checked else "NoData"))
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


_XML_FORBIDDEN = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")


def xml_text(s):
    """`s` with the characters XML 1.0 cannot carry (control characters, unpaired surrogates) replaced by '?'."""
    return _XML_FORBIDDEN.sub("?", str(s))


def _suite(root, stem, tests, failures, errors, seconds, trace, version):
    suite = ET.SubElement(root, "testsuite", name=xml_text("tracestudio check %s" % stem), tests=str(tests),
                          failures=str(failures), errors=str(errors), skipped="0", time="%.3f" % seconds,
                          timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"))
    props = ET.SubElement(suite, "properties")
    ET.SubElement(props, "property", name="trace", value=xml_text(trace))
    ET.SubElement(props, "property", name="tracestudio", value=version)
    return suite


def _stem(trace):
    stem = os.path.basename(trace)
    for ext in (".gz", ".kev"):
        if stem.endswith(ext):
            stem = stem[:-len(ext)]
    return stem


def _document(root):
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=True) + "\n"


def junit_xml(results, trace, seconds, version):
    """A JUnit XML report: testsuites > testsuite > testcase (with failure for each failed assertion)."""
    failed = sum(1 for r in results if not r.ok)
    stem = _stem(trace)
    root = ET.Element("testsuites", name="tracestudio check", tests=str(len(results)), failures=str(failed),
                      errors="0", time="%.3f" % seconds)
    suite = _suite(root, stem, len(results), failed, 0, seconds, trace, version)
    for r in results:
        case = ET.SubElement(suite, "testcase", classname=xml_text("tracestudio.check.%s" % stem),
                             name=xml_text(r.name), time="0")
        if r.ok:
            ET.SubElement(case, "system-out").text = xml_text(r.summary)
        else:
            f = ET.SubElement(case, "failure", message=xml_text(r.summary), type=r.kind)
            f.text = xml_text("\n".join([r.summary] + r.evidence))
    return _document(root)


def junit_error(trace, message, seconds, version):
    """A JUnit XML report for a trace that could not be checked: one testcase with an error."""
    stem = _stem(trace)
    root = ET.Element("testsuites", name="tracestudio check", tests="1", failures="0", errors="1",
                      time="%.3f" % seconds)
    suite = _suite(root, stem, 1, 0, 1, seconds, trace, version)
    case = ET.SubElement(suite, "testcase", classname=xml_text("tracestudio.check.%s" % stem), name="read trace",
                         time="0")
    e = ET.SubElement(case, "error", message=xml_text(message), type="TraceError")
    e.text = xml_text(message)
    return _document(root)


def write_report(path, text):
    """Write the report atomically, so that a CI job never reads a half-written or stale one."""
    from .serve import write_atomic
    write_atomic(os.path.abspath(path), text.encode("utf-8"))
