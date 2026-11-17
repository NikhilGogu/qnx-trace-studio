# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Regression checks on real QNX 8.0 traces (tests/data/run1, captured with
workload/run_scenarios.sh). Run with pytest or directly:  python3 tests/test_tracestudio.py
The traceprinter comparison needs the QNX SDP host tools on PATH and the Perfetto checks
need the perfetto package; both are skipped when absent unless TS_REQUIRE_ALL=1, which
makes a missing tool a failure (use it for release checks).
"""
import gzip
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
from drop_buffer import drop_buffers  # noqa: E402
from tracestudio.kev import KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402
from tracestudio.validate import load_logs, validate  # noqa: E402
from tracestudio.analyses import deadline_misses, suspected_inversions  # noqa: E402

DATA = os.path.join(HERE, "data", "run1")
DATA3 = os.path.join(HERE, "data", "run3")
DATA4 = os.path.join(HERE, "data", "run4")
DATA5 = os.path.join(HERE, "data", "run5")
DATA6 = os.path.join(HERE, "data", "run6")      # thread-pool captures (workload/ts_pool.c), not captured yet
TMP = tempfile.mkdtemp(prefix="ts-test-")
SCEN = ["ipc_wide", "ipc_fast", "prio_inherit", "prio_fixed"]


def skip(why):
    if os.environ.get("TS_REQUIRE_ALL"):
        raise AssertionError("required for release checks: " + why)
    print("skip:", why)


def kev(name, data=DATA):
    out = os.path.join(TMP, "%s_%s.kev" % (os.path.basename(data), name))      # fixtures of several runs share names
    if not os.path.exists(out):
        with gzip.open(os.path.join(data, name + ".kev.gz")) as f, open(out, "wb") as g:
            shutil.copyfileobj(f, g)
    return out


def test_parser_invariants():
    for s in SCEN:
        k = KevFile(kev(s))
        evs = k.events()
        assert k.stats.orphan_slots == 0 and k.stats.partial_combines == 0, s
        assert not k.stats.lost_buffers and not k.losses and not k.stats.short_buffers, s
        assert all(a.ts <= b.ts for a, b in zip(evs, evs[1:])), s
        assert k.stats.rollovers >= 2, s    # 2.7 GHz TSC wraps 32 bits every 1.6 s


def test_traceprinter_event_by_event():
    tp = shutil.which("traceprinter")
    if not tp:
        return skip("traceprinter not on PATH (source qnxsdp-env.sh)")
    for s in SCEN:
        txt = os.path.join(TMP, s + ".txt")
        subprocess.run([tp, "-f", kev(s), "-o", txt], check=True)
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "compare_traceprinter.py"), kev(s), txt],
                           capture_output=True, text=True)
        assert r.returncode == 0, s + "\n" + r.stdout


def test_ground_truth_links():
    for s in SCEN:
        res, gt, _ = validate(Model(KevFile(kev(s))), os.path.join(DATA, s + ".logs"))
        assert res["requests"] == (200 if s.startswith("ipc") else 40), s
        assert res["matched"] == res["requests"], (s, res["examples"])
        assert not any(":WRONG" in k or ":missing" in k for k in res["checks"]), (s, res["checks"])


def test_priority_story():
    fixed = Model(KevFile(kev("prio_fixed")))
    inherit = Model(KevFile(kev("prio_inherit")))
    f = deadline_misses(fixed, "hi", 5000)["ts_psrv"]
    i = deadline_misses(inherit, "hi", 5000)["ts_psrv"]
    assert (f["requests"], f["missed"], f["excluded"]) == (20, 20, 0), f   # all miss with a fixed-priority channel
    assert (i["requests"], i["missed"], i["excluded"]) == (20, 0, 0), i    # none with priority inheritance
    inv_f, inv_i = suspected_inversions(fixed), suspected_inversions(inherit)
    sus = inv_f["suspected"]
    assert len(sus) == 20 and all(x["client"].endswith("/hi") for x in sus), sus
    assert all(x["preempted_by"][0]["thread"].endswith("/hog") for x in sus)
    assert all(x["server_run_mask"] == "0x1" and x["server_prio_while_ready"] == 10 for x in sus)
    assert all(x["while"].get("SEND", 0) > 7000 for x in sus)       # hog runs while hi waits for the receive
    for x in inv_f["insufficient_evidence"] + inv_i["insufficient_evidence"]:   # only requests to procnto
        assert list(x["reasons"]) == [k for k in x["reasons"] if k.startswith("request handed off")], x
        assert x["server"].startswith("procnto-smp-instr/"), x
    assert inv_i["suspected"] == []
    assert any(v["flags"] & 1 for v in fixed.channels.values())         # _NTO_CHF_FIXED_PRIORITY seen
    assert not any(v["flags"] & 1 for v in inherit.channels.values())


def test_perfetto_loads():
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        return skip("perfetto package not installed")
    for s in ("ipc_wide", "prio_fixed"):
        out = os.path.join(TMP, s + ".perfetto-trace")
        write(Model(KevFile(kev(s))), out)
        tp = TraceProcessor(trace=out)
        n = {r.t: r.n for r in tp.query(
            "SELECT 'sched' t, COUNT(*) n FROM sched UNION ALL SELECT 'thread_state', COUNT(*) FROM thread_state "
            "UNION ALL SELECT 'flow', COUNT(*) FROM flow UNION ALL SELECT 'msgsend', COUNT(*) FROM slice "
            "WHERE name LIKE 'MsgSend -> ts_%'")}
        assert n["sched"] > 1000 and n["thread_state"] > 1000 and n["flow"] > 500, n
        assert n["msgsend"] == (202 if s == "ipc_wide" else 41), n   # requests + forwards + QUIT
        errs = [(r.name, r.value) for r in tp.query(
            "SELECT name, value FROM stats WHERE value > 0 AND severity IN ('error', 'data_loss')")]
        assert not errs, errs
        bad_flows = list(tp.query(
            "SELECT COUNT(*) n FROM flow f JOIN slice a ON a.id = f.slice_out JOIN slice b ON b.id = f.slice_in "
            "WHERE extract_arg(a.arg_set_id, 'debug.txn') IS NOT extract_arg(b.arg_set_id, 'debug.txn')"))[0].n
        assert bad_flows == 0
        assert list(tp.query("SELECT COUNT(*) n FROM slice WHERE dur < 0"))[0].n == 0
        if s == "prio_fixed":
            m = Model(KevFile(kev(s)))
            want = sorted(m.kev.cycles_to_ns(t.t_exit) - m.kev.cycles_to_ns(t.t_call) for t in m.txns
                          if m.threads[t.client].name == "hi" and m.server_name(t) == "ts_psrv")
            got = sorted(r.dur for r in tp.query(
                "SELECT s.dur FROM slice s JOIN thread_track tt ON s.track_id = tt.id JOIN thread t USING (utid) "
                "WHERE t.name = 'hi' AND s.name = 'MsgSend -> ts_psrv'"))
            assert len(want) == 20 and got == want, (got, want)
            cp = list(tp.query(
                "SELECT COUNT(*) n FROM slice s JOIN track tr ON s.track_id = tr.id "
                "WHERE tr.name = 'hi critical path' AND s.depth = 0 AND s.name LIKE 'txn % -> ts_psrv: %'"))[0].n
            assert cp == 20, cp
        tp.close()



def test_every_checked_in_capture_converts_and_loads():
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        return skip("perfetto package not installed")
    found = sorted(os.path.relpath(os.path.join(d, f), os.path.join(HERE, "data"))
                   for d, _, fs in os.walk(os.path.join(HERE, "data")) for f in fs if f.endswith(".kev.gz"))
    assert len(found) == 11, found                          # README and docs/VALIDATION.md name this count
    for rel in found:
        run, name = os.path.split(rel[:-len(".kev.gz")])
        out = os.path.join(TMP, "%s_%s.perfetto-trace" % (run, name))
        write(Model(KevFile(kev(name, os.path.join(HERE, "data", run)))), out)
        tp = TraceProcessor(trace=out)
        errs = [(r.name, r.value) for r in tp.query(
            "SELECT name, value FROM stats WHERE value > 0 AND severity IN ('error', 'data_loss')")]
        assert not errs, (rel, errs)
        assert list(tp.query("SELECT COUNT(*) n FROM slice WHERE dur < 0"))[0].n == 0, rel
        bad_flows = list(tp.query(
            "SELECT COUNT(*) n FROM flow f JOIN slice a ON a.id = f.slice_out JOIN slice b ON b.id = f.slice_in "
            "WHERE extract_arg(a.arg_set_id, 'debug.txn') IS NOT extract_arg(b.arg_set_id, 'debug.txn')"))[0].n
        assert bad_flows == 0, rel
        assert list(tp.query("SELECT COUNT(*) n FROM thread_state"))[0].n > 0, rel
        tp.close()


def test_sql_examples_run_on_converted_captures():
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        return skip("perfetto package not installed")
    sql_dir = os.path.join(HERE, "..", "docs", "sql")
    queries = {f[:-4]: open(os.path.join(sql_dir, f)).read() for f in sorted(os.listdir(sql_dir)) if f.endswith(".sql")}
    assert set(queries) == {"slowest_requests", "critical_path_by_kind", "server_priority_while_handling"}, queries
    for name, hi_prio, preempted_first in (("prio_fixed", 10, True), ("prio_inherit", 30, False)):
        m = Model(KevFile(kev(name)))
        out = os.path.join(TMP, name + "_sql.perfetto-trace")
        write(m, out)
        tp = TraceProcessor(trace=out)
        res = {q: list(tp.query(text)) for q, text in queries.items()}
        tp.close()
        slowest = max((t for t in m.txns if t.trustworthy), key=lambda t: t.t_exit - t.t_call)
        assert len(res["slowest_requests"]) == 20 and res["slowest_requests"][0].txn == slowest.idx, name
        kinds = [r.kind for r in res["critical_path_by_kind"] if r.client_thread == "hi"]
        assert (kinds[0] == "server preempted") == preempted_first, (name, kinds)
        prios = {r.server_prio for r in res["server_priority_while_handling"]
                 if r.client_thread == "hi" and r.server_thread == "psrv-rx"}
        assert prios == {hi_prio}, (name, prios)


def test_readme_capabilities_point_at_real_code():
    import re
    with open(os.path.join(HERE, "..", "README.md")) as f:
        text = f.read()
    sec = text.split("## What trace-studio adds", 1)[1].split("\n## ", 1)[0]
    root = os.path.join(HERE, "..")
    for path in re.findall(r"\]\(([^)#]+)\)", sec) + re.findall(r"`(tests/data/[^`]+)`", sec):
        assert os.path.exists(os.path.join(root, path)), path
    src = "".join(open(os.path.join(root, "tracestudio", f)).read() for f in os.listdir(os.path.join(root, "tracestudio"))
                  if f.endswith(".py"))
    tests = open(os.path.join(HERE, "test_synthetic.py")).read() + open(os.path.join(HERE, "test_tracestudio.py")).read()
    names = re.findall(r"`(\w+)`", sec)
    assert len(names) > 15, names
    for n in names:
        if n.startswith("test_"):
            assert re.search(r"^def %s\(" % n, tests, re.M), n
        else:
            assert re.search(r"^def %s\(" % n, src, re.M), n


def test_examples_page_withholds_timing():
    import contextlib
    import io
    import re
    try:
        import perfetto  # noqa: F401
    except ImportError:
        return skip("perfetto package not installed")
    from build_examples import TIMING, main as build
    only = ["--only", "run1/prio_fixed", "--only", "run1/prio_inherit"]
    pages = {}
    for mode in ("withheld", "with-timing"):
        out = os.path.join(TMP, "site-" + mode)
        with contextlib.redirect_stdout(io.StringIO()):
            assert build(["--out", out] + only + (["--with-timing"] if mode == "with-timing" else [])) == 0
        assert sorted(os.listdir(out)) == ["index.html", "run1_prio_fixed.perfetto-trace",
                                           "run1_prio_inherit.perfetto-trace"]
        with open(os.path.join(out, "index.html")) as f:
            pages[mode] = f.read()
    text = pages["withheld"]
    assert not TIMING.search(text.split("<script>")[0]), TIMING.search(text).group(0)
    assert text.count("<section ") == 2 and "[timing withheld]" in text
    assert "suspected priority inversions: 20" in text and "suspected priority inversions: 0;" in text
    assert "ts_pload/hog (prio 20) on CPU 0" in text and "fails (exit 1)" in text and "passes (exit 0)" in text
    assert "startupCommands=" in text and "PinTracksByRegex" in text
    assert re.search(r"MsgSend latency \d+\.\d us", pages["with-timing"])
    assert pages["with-timing"].replace("[timing withheld]", "") == pages["with-timing"]
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            build(["--out", os.path.join(TMP, "site-x"), "--only", "run9/nothing"])
        raise AssertionError("an unknown example was accepted")
    except SystemExit as e:
        assert e.code == 2

def test_inversion_after_receive_on_a_real_capture():
    # run 3: lo sends 0.5 ms of work at t+0, hi at t+4 ms, the hog wakes at t+4.2 ms, while
    # the server already handles hi's request
    for name, n_inv, missed in (("prio_fixed_rx", 20, 20), ("prio_inherit_rx", 0, 0)):
        m = Model(KevFile(kev(name, DATA3)))
        res, _, _ = validate(m, os.path.join(DATA3, name + ".logs"))
        assert res["ok"] and res["matched"] == res["requests"] == 40, (name, res["checks"])
        d = deadline_misses(m, "hi", 5000)["ts_psrv"]
        assert (d["requests"], d["missed"]) == (20, missed), (name, d)
        sus = suspected_inversions(m)["suspected"]
        assert len(sus) == n_inv, (name, len(sus))
        assert all(set(x["while"]) == {"REPLY"} and x["preempted_by"][0]["thread"] == "ts_pload/hog" for x in sus)


def test_cancellation_matches_the_workload_log():
    m = Model(KevFile(kev("cancel", DATA3)))
    rows = load_logs(os.path.join(DATA3, "cancel.logs"))
    sends = {(r["tid"], r["id"]): r for r in rows if r["ev"] == "SEND"}
    seen = 0
    for r in rows:
        if r["ev"] != "SEND_DONE":
            continue
        s = sends[(r["tid"], r["id"])]
        (tx,) = [t for t in m.txns if t.client == (r["pid"], r["tid"]) and t.t_call is not None
                 and s["cycles"] <= t.t_call <= r["cycles"]]
        if r["b"] == -1:      # ETIMEDOUT (tmo) or EINTR (sig): MsgSendv exits with -1, no INT_CALL is traced
            assert r["c"] in (4, 260) and tx.outcome == "cancelled" and tx.status == -1 and not tx.trustworthy
            assert tx.errno == r["c"]                  # EINTR or ETIMEDOUT, from the failed exit's errno word
        else:
            assert tx.outcome == "replied" and tx.trustworthy, tx
        seen += 1
    assert seen == 15
    late = sum(1 for r in rows if r["ev"] == "REPLY" and r["b"] == -1)
    assert late == 10 and m.counters["reply_without_txn"] == late     # late replies never touch other exchanges


def test_resource_manager_names_and_paths():
    m = Model(KevFile(kev("rm_small", DATA3)))
    read = {}
    for t in m.txns:
        if t.msg_type == "_IO_READ" and t.path and m.label(t.client).startswith("cat/") and t.status and t.status > 0:
            read[t.path] = read.get(t.path, 0) + t.status
    assert read == {"/system/lib/libxo.so.0": 132400, "/data/ts_rm.bin": 65536}   # sizes from wc -c on the target
    to_disk = {t.msg_type for t in m.txns if m.server_name(t) == "devb-eide"}
    assert {"_IO_CONNECT_OPEN", "_IO_READ", "_IO_WRITE", "_IO_CLOSE", "_IO_CONNECT_UNLINK", "_IO_LSEEK"} <= to_disk
    try:
        from perfetto.trace_processor import TraceProcessor
        from tracestudio.perfetto_out import write
    except ImportError:
        return skip("perfetto package not installed")
    out = os.path.join(TMP, "rm_small.perfetto-trace")
    write(m, out)
    tp = TraceProcessor(trace=out)
    r = list(tp.query("SELECT COUNT(*) n, SUM(extract_arg(arg_set_id, 'debug.path_candidate') = "
                      "'/system/lib/libxo.so.0') p FROM slice WHERE name = 'MsgSend _IO_READ (candidate) -> devb-eide'"))[0]
    assert r.n >= 30 and r.p >= 30, (r.n, r.p)
    tp.close()


def test_cli_exit_codes():
    from tracestudio.cli import main
    empty = tempfile.mkdtemp(prefix="ts-empty-")
    assert main(["validate", kev("prio_fixed"), "--logs", empty]) == 1             # no logged requests
    assert main(["validate", kev("prio_fixed"), "--logs", os.path.join(DATA, "prio_fixed.logs")]) == 0
    junk = os.path.join(TMP, "not-a-trace.kev")
    with open(junk, "w") as f:
        f.write("not a trace")
    assert main(["info", junk]) == 1 and main(["analyze", junk]) == 1



def test_min_us_zero_from_the_command_line():
    import contextlib
    import io
    from tracestudio.cli import main
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["analyze", kev("ipc_wide"), "--min-us", "0"]) == 0
        assert main(["check", kev("prio_inherit"), "--no-inversions", "--client", "ts_pload/", "--min-us", "0"]) == 0
    assert "suspected priority inversions: 0" in out.getvalue(), out.getvalue()


def test_readme_quickstart_commands():
    """Every tracestudio command in the README's no-QNX quickstart runs on the checked-in captures with the exit
    status the README promises (open needs a browser and is covered by its own test)."""
    import contextlib
    import io
    import shlex
    from tracestudio.cli import main
    with open(os.path.join(HERE, "..", "README.md")) as f:
        text = f.read()
    block = text.split("## Try it without a QNX target", 1)[1].split("```", 2)[1]
    cmds = [shlex.split(ln.split("#", 1)[0])[1:] for ln in block.splitlines() if ln.startswith("venv/bin/tracestudio ")]
    assert [c[0] for c in cmds] == ["analyze", "diff", "open", "check", "check", "analyze"], cmds
    for c in cmds:
        if c[0] == "open":
            continue
        args = [os.path.join(HERE, "..", a) if a.startswith("tests/data/") else a for a in c]
        want = 1 if c[0] == "check" and "prio_fixed" in c[1] else 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(args) == want, (c, out.getvalue()[-2000:])
        if c[0] == "analyze" and "mutex_chain_nopi" in c[1]:
            assert "MUTEX-blocked on mutex" in out.getvalue() and "owned by ts_mutex/holder" in out.getvalue()
            assert "held by ts_mutex/hog (prio 20)" in out.getvalue()


def _json_doc_keys():
    """{section heading: set of keys} from the tables of docs/JSON.md (first column, in backticks)."""
    import re
    keys, head = {}, None
    with open(os.path.join(HERE, "..", "docs", "JSON.md")) as f:
        for ln in f:
            if ln.startswith("### "):
                head = ln[4:].strip()
                keys[head] = set()
            m = re.match(r"\| `(\w+)` \|", ln)
            if m and head:
                keys[head].add(m.group(1))
    return keys


def test_analyze_json_contract():
    import contextlib
    import io
    import json
    from tracestudio.cli import main

    def run(*args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(list(args)) == 0
        return json.loads(out.getvalue())
    doc = _json_doc_keys()
    a = run("analyze", kev("prio_fixed"), "--json", "--deadline", "hi=5000", "--slowest", "1")
    m = run("analyze", kev("mutex_chain_nopi", DATA4), "--json", "--slowest", "1")
    assert set(a) == set(m) == doc["Top level"], set(a) ^ doc["Top level"]
    assert set(a["ipc_latency"][0]) == doc["ipc_latency[]"]
    assert set(a["inversions"]) == doc["inversions"]
    assert set(a["inversions"]["suspected"][0]) == doc["inversions.suspected[]"]
    assert set(a["inversions"]["insufficient_evidence"][0]) == doc["inversions.insufficient_evidence[]"]
    assert set(a["deadlines"]["hi"]["by_server"]["ts_psrv"]) == doc["deadlines.NAME.by_server.SERVER"]
    known = [w for w in m["mutex_waits"] if w["owner"]]
    assert known and all(set(w) == doc["mutex_waits[]"] - {"owner_unknown"} for w in known)
    cp = a["critical_paths"][0]
    assert set(cp) == doc["critical_paths[]"]
    seen = set().union(*(set(sg) for c in (cp, m["critical_paths"][0]) for sg in c["segments"]))
    assert seen <= doc["critical_paths[].segments[]"] and {"start_us", "us", "kind", "what", "txn"} <= seen, seen
    d = run("diff", kev("prio_fixed"), kev("prio_inherit"), "--json", "--deadline", "hi=5000")
    assert set(d) == {"before", "after", "comparison"} and set(d["comparison"]) == {"latency", "server_priority"}
    assert set(d["before"]) == set(d["after"]) == doc["Top level"] - {"critical_paths"}


def test_agent_skill_commands_parse():
    """skills/trace-studio/SKILL.md names only real subcommands and options, covers every analysis command, and
    quotes reasons the analysis really gives."""
    import re
    import shlex
    from tracestudio import __version__
    from tracestudio.cli import build_parser
    with open(os.path.join(HERE, "..", "skills", "trace-studio", "SKILL.md")) as f:
        text = f.read()
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert fm, "SKILL.md must start with YAML frontmatter"
    fields = dict((k.strip(), v.strip().strip('"')) for k, _, v in (ln.partition(":") for ln in fm.group(1).splitlines()))
    assert fields["name"] == "trace-studio" and fields["license"] == "Apache-2.0"
    assert 0 < len(fields["description"]) <= 1024
    assert "trace-studio v%s" % __version__ in fields["description"]
    cmds = re.findall(r"`(tracestudio [^`]+\.kev[^`]*)`", text)      # commands with a trace, not bare names
    assert len(cmds) >= 12, cmds
    parser = build_parser()
    for c in cmds:
        argv = shlex.split(c)[1:]
        try:
            parser.parse_args(argv)
        except SystemExit:
            raise AssertionError("does not parse: " + c)
    used = {shlex.split(c)[1] for c in cmds}
    assert used == {"info", "analyze", "diff", "convert", "open", "check"}, used
    with open(os.path.join(HERE, "..", "tracestudio", "analyses.py")) as f:
        src = f.read()
    for reason in ("run mask of %s not traced", "request handed off: received by"):
        assert reason in src, reason


def test_version_agrees_everywhere():
    import re
    from tracestudio import __version__
    root = os.path.join(HERE, "..")
    with open(os.path.join(root, "pyproject.toml")) as f:
        project = re.search(r'^version = "([^"]+)"', f.read(), re.M).group(1)
    with open(os.path.join(root, "CHANGELOG.md")) as f:
        latest = re.search(r"^## (\S+)", f.read(), re.M).group(1)
    assert __version__ == project == latest, (__version__, project, latest)


def test_docs_links_resolve_and_test_counts_match():
    """Relative links and #anchors in the README and docs/ point at files and headings that exist, and every
    statement of the suite's size names the real number of tests."""
    import re
    root = os.path.join(HERE, "..")
    suites = {"test_synthetic.py": 0, "test_tracestudio.py": 0}
    for name in suites:
        with open(os.path.join(HERE, name)) as f:
            suites[name] = len(re.findall(r"^def test_", f.read(), re.M))
    total, captures = sum(suites.values()), suites["test_tracestudio.py"]

    def slug(heading):
        return re.sub(r"[^a-z0-9 _-]", "", heading.lower()).replace(" ", "-")
    for doc in ("README.md", "docs/DESIGN.md", "docs/VALIDATION.md", "docs/JSON.md", "CHANGELOG.md"):
        with open(os.path.join(root, doc)) as f:
            text = f.read()
        anchors = {slug(h) for h in re.findall(r"^#+ (.+)$", text, re.M)}
        for target in re.findall(r"\]\(([^)\s]+)\)", text):
            if target.startswith(("http://", "https://")):
                continue
            path, _, anchor = target.partition("#")
            if path:
                assert os.path.exists(os.path.join(root, os.path.dirname(doc), path)), (doc, target)
            elif anchor:
                assert anchor in anchors, (doc, target)
        for n in re.findall(r"(?:runs|has) (\d+) tests", text) + re.findall(r"(\d+) tests in \[tests", text):
            assert int(n) == total, (doc, n, total)
        for n in re.findall(r"(\d+) on (?:the checked-in captures|the captures in)", text):
            assert int(n) == captures, (doc, n, captures)


def test_validate_all_summarises_every_capture():
    import contextlib
    import io
    from validate_all import main as validate_all
    out_file = os.path.join(TMP, "validation-summary.txt")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert validate_all([DATA, DATA3, "--skip-traceprinter", "--out", out_file]) == 0
    lines = open(out_file).read().splitlines()
    assert lines == out.getvalue().splitlines()
    assert len([ln for ln in lines if ln.startswith("PASS validate ")]) == 6, lines     # cancel and rm_small: no
    assert "logged requests 200, matched 200" in lines[0] and lines[-1] == "8 captures, all checks passed"
    if not shutil.which("traceprinter"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert validate_all([DATA]) == 1
        assert out.getvalue().startswith("error: traceprinter not on PATH")
    # run_scenarios.sh leaves an empty NAME.logs for scenarios without workload logs (rm_small, rm_traffic)
    d = os.path.join(TMP, "empty-logs")
    os.makedirs(os.path.join(d, "rm_small.logs"), exist_ok=True)
    shutil.copy(os.path.join(DATA3, "rm_small.kev.gz"), d)
    with contextlib.redirect_stdout(io.StringIO()) as out:
        assert validate_all([d, "--skip-traceprinter"]) == 0, out.getvalue()
    assert out.getvalue().splitlines() == ["1 captures, all checks passed"], out.getvalue()


def test_pool_capture_when_present():
    """pool_fixed (workload/run_scenarios.sh): both receive threads busy with lo's and lo2's requests when hi
    sends, all READY at 10 behind the hog. Expected: every hi request a suspected SEND-phase inversion that names
    both receive threads, and every logged request matched. Skipped until the capture is checked in."""
    if not os.path.exists(os.path.join(DATA6, "pool_fixed.kev.gz")):
        print("skip: tests/data/run6/pool_fixed.kev.gz not captured yet")
        return
    m = Model(KevFile(kev("pool_fixed", DATA6)))
    res, _, _ = validate(m, os.path.join(DATA6, "pool_fixed.logs"))
    assert res["ok"] and res["matched"] == res["requests"] == 60, res["examples"]
    sus = suspected_inversions(m)["suspected"]
    hi = [x for x in sus if x["client"].endswith("/hi")]
    assert len(hi) == 20 and all(x["while"].get("SEND", 0) > 0 for x in hi), hi[:1]
    assert all({r["thread"].split("/")[1] for r in x["receivers"]} >= {"pool-rx0", "pool-rx1"} for x in hi)
    assert all(x["preempted_by"][0]["thread"].endswith("/hog") for x in hi)

def test_fault_injection_never_links_wrong():
    k = KevFile(kev("ipc_wide"))
    data, nbuf, removed = drop_buffers(k, {79, 80, 83, 84})   # two buffers per CPU inside the workload window
    k2 = KevFile("dropped.kev", data)
    assert [(l.kind, l.count) for l in k2.losses] == [("buffers", 1)] * 4
    res, _, _ = validate(Model(k2), os.path.join(DATA, "ipc_wide.logs"))
    links = ("server_pid", "server_tid", "rcvid", "coid", "payload_id", "parent_link")
    assert not any(c.startswith(links) and c.endswith(":WRONG") for c in res["checks"]), res["checks"]
    assert "WRONG_unflagged" not in res["checks"]          # outcomes contradict the log only on flagged exchanges
    assert res["matched"] < res["requests"] == 200 and not res["ok"]



def test_check_fails_prio_fixed_and_passes_prio_inherit():
    import contextlib
    import io
    import xml.etree.ElementTree as ET
    from tracestudio.cli import main
    opts = ["--deadline", "hi=5000", "--no-inversions", "--max-insufficient", "2"]
    for name, code in (("prio_fixed", 1), ("prio_inherit", 0)):
        xml = os.path.join(TMP, name + ".junit.xml")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert main(["check", kev(name)] + opts + ["--junit", xml]) == code
        text = out.getvalue()
        root = ET.parse(xml).getroot()
        (suite,) = root.findall("testsuite")
        cases = suite.findall("testcase")
        assert root.tag == "testsuites" and suite.get("tests") == "3" and suite.get("errors") == "0"
        assert [c.get("name") for c in cases] == ["trace integrity", "deadline hi=5000 us",
                                                  "no suspected priority inversions"]
        failures = [c.find("failure") for c in cases]
        assert "matched ts_pload/hi@30 -> procnto-smp-instr (1), ts_pload/hi@30 -> ts_psrv (20)" in text
        assert "2 with insufficient evidence (at most 2 allowed)" in text
        if code:
            assert "FAIL deadline hi=5000 us: 20 of 21 trustworthy requests over 5000 us, max 11429.5 us" in text
            assert "FAIL no suspected priority inversions: 20 suspected; checked 672 exchanges" in text
            assert "txn 520: ts_pload/hi (prio 30)" in text
            assert suite.get("failures") == "2" and failures[0] is None
            assert failures[1].get("type") == "DeadlineMissed" and "txn 524: ts_pload/hi -> ts_psrv" in failures[1].text
            assert failures[2].get("type") == "PriorityInversion" and "ts_pload/hog (prio 20)" in failures[2].text
        else:
            assert "check: 3 assertions, 0 failed" in text and "FAIL" not in text
            assert suite.get("failures") == "0" and failures == [None, None, None]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):                       # inconclusive requests count against --no-inversions
        assert main(["check", kev("prio_inherit"), "--no-inversions"]) == 1
    assert "2 with insufficient evidence (at most 0 allowed)" in out.getvalue()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):                       # both are requests to procnto from other clients
        assert main(["check", kev("prio_inherit"), "--no-inversions", "--client", "ts_pload/"]) == 0
    assert "checked 118 exchanges of clients matching 'ts_pload/'" in out.getvalue(), out.getvalue()


def test_open_converts_once_and_pins_the_slowest_clients():
    import contextlib
    import io
    import shutil
    from tracestudio import __version__
    from tracestudio.cli import main
    from tracestudio.serve import cache_path, default_pins
    pins, labels = default_pins(Model(KevFile(kev("prio_fixed"))))
    assert pins == ["^lo (critical path|QNX state)", "^hi (critical path|QNX state)"]
    assert labels == ["ts_pload/lo", "ts_pload/hi"]
    assert default_pins(Model(KevFile(kev("prio_fixed"))), [("hi@ts_psrv", 5000.0)])[1] == ["ts_pload/hi"]
    old = os.environ.get("XDG_CACHE_HOME")
    os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp(prefix="ts-cache-")
    try:
        path = os.path.join(TMP, "open_me.kev")
        shutil.copy(kev("prio_fixed"), path)
        cached = cache_path(path, __version__)
        for step in ("converted ", "using ", "incomplete; converting again"):
            if step.startswith("incomplete"):
                with open(cached, "r+b") as f:                # an interrupted write must not be reused
                    f.truncate(1000)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                assert main(["open", path, "--no-browser", "--port", "0", "--timeout", "1", "--pin", "^hi "]) == 1
            assert step in out.getvalue() and "pinning tracks matching ^hi " in out.getvalue()
        assert os.path.getsize(cached) > 1000 and not os.path.exists(os.path.join(TMP, "open_me.perfetto-trace"))
    finally:
        if old is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = old



def test_mutex_chain_captures():
    import csv
    from tracestudio.analyses import Timeline, mutex_waits
    from tracestudio.critical import critical_path
    tp = shutil.which("traceprinter")
    for name, inherit in (("mutex_chain_hog", True), ("mutex_chain_nopi", False)):
        path, logs = kev(name, DATA4), os.path.join(DATA4, name + ".logs")
        m = Model(KevFile(path))
        res, _, _ = validate(m, logs)
        assert res["ok"] and res["requests"] == res["matched"] == 20 and res["mutex_waits_logged"] == 20, res
        for c in ("mutex_address", "mutex_owner", "mutex_owner_value", "mutex_release", "mutex_owner_request"):
            assert res["checks"][c + ":ok"] == 20, (name, c, res["checks"])
        tl = Timeline(m)
        rows = [r for r in mutex_waits(m, tl, 100.0) if r["waiter"] == "ts_mutex/mrx"]
        assert len(rows) == 20 and all(r["owner"] == "ts_mutex/holder" for r in rows)
        assert all((r["owner_prio_max"] == 30) == inherit for r in rows)      # mutex priority inheritance, or not
        hi = [t for t in m.txns if m.label(t.client) == "ts_mutex/hi" and m.server_name(t) == "ts_mutex" and t.causal]
        worst = max(hi, key=lambda t: t.t_exit - t.t_call)
        segs = critical_path(m, worst, tl)
        assert sum(sg.t1 - sg.t0 for sg in segs) == worst.t_exit - worst.t_call
        (n,) = [sg for sg in segs if sg.kind == "nested request"]
        assert "MUTEX-blocked on mutex" in n.label and "owned by ts_mutex/holder, which waits for ts_mslow" in n.label
        kids = " | ".join(k.label for k in n.children)
        assert ("hog (prio 20)" in kids) == (not inherit), kids               # without inheritance the hog cuts in
        if tp:
            txt = os.path.join(TMP, name + ".txt")
            subprocess.run([tp, "-f", path, "-o", txt], check=True)
            r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "compare_traceprinter.py"), path, txt],
                               capture_output=True, text=True)
            assert r.returncode == 0 and "mismatches: 0 (sign only: 0)" in r.stdout, r.stdout
        elif os.environ.get("TS_REQUIRE_ALL"):
            raise AssertionError("traceprinter not found")
    bad = tempfile.mkdtemp(prefix="ts-mutex-logs-")                 # a log that names another owner value
    for f in os.listdir(os.path.join(DATA4, "mutex_chain_hog.logs")):
        with open(os.path.join(DATA4, "mutex_chain_hog.logs", f)) as src, open(os.path.join(bad, f), "w") as dst:
            for line in src:
                parts = line.rstrip("\n").split(",")
                if len(parts) == 10 and parts[4] == "LOCKED" and parts[5] == "1":
                    parts[7] = str(int(parts[7]) + 1)
                dst.write(",".join(parts) + "\n")
    res, _, _ = validate(Model(KevFile(kev("mutex_chain_hog", DATA4))), bad)
    assert not res["ok"] and "mutex_owner_value:WRONG" in res["failed_checks"]



def test_aarch64le_capture():
    """prio_fixed captured on an aarch64le QEMU virt target (TCG, 2 CPUs, 62.5 MHz ClockCycles)."""
    from tracestudio.analyses import Timeline, deadline_misses, suspected_inversions
    path = kev("prio_fixed", DATA5)
    k = KevFile(path)
    assert (k.attrs.get("MACHINE"), k.ncpu, k.cycles_per_sec, k.losses) == ("QEMU_virt", 2, 62500000, [])
    m = Model(k)
    res, _, _ = validate(m, os.path.join(DATA5, "prio_fixed.logs"))
    assert res["ok"] and res["requests"] == res["matched"] == 40, res
    inv = suspected_inversions(m, tl=Timeline(m))
    sus = inv["suspected"]
    assert len(sus) == 20 and all(x["client"] == "ts_pload/hi" and x["server_run_mask"] == "0x1" for x in sus)
    assert all(set(x["while"]) == {"SEND"} for x in sus)
    d = deadline_misses(m, "hi@ts_psrv", 5000)["ts_psrv"]
    assert (d["requests"], d["missed"]) == (20, 20)
    tp = shutil.which("traceprinter")
    if tp:
        txt = os.path.join(TMP, "a64_prio_fixed.txt")
        subprocess.run([tp, "-f", path, "-o", txt], check=True)
        r = subprocess.run([sys.executable, os.path.join(HERE, "..", "tools", "compare_traceprinter.py"), path, txt],
                           capture_output=True, text=True)
        assert r.returncode == 0 and "mismatches: 0 (sign only: 0)" in r.stdout, r.stdout
    elif os.environ.get("TS_REQUIRE_ALL"):
        raise AssertionError("traceprinter not found")


def test_inversion_gate_follows_the_mutex_chain():
    """mutex_chain_nopi: hi (30) waits on mrx, MUTEX-blocked on holder (10, no inheritance), REPLY-blocked on
    slow-rx (10), READY while the hog (20) holds CPU 0: the gate fails and names the chain. mutex_chain_hog:
    inheritance lifts holder and slow-rx to 30, so the hog cannot get in and the gate passes."""
    import contextlib
    import io
    import xml.etree.ElementTree as ET
    from tracestudio.cli import main
    for name, want in (("mutex_chain_nopi", 1), ("mutex_chain_hog", 0)):
        report = os.path.join(TMP, name + "-junit.xml")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["check", kev(name, DATA4), "--no-inversions", "--client", "ts_mutex/hi", "--junit", report])
        assert code == want, (name, out.getvalue())
        kinds = [f.get("type") for f in ET.parse(report).iter("failure")]
        assert kinds == (["PriorityInversion"] if want else []), (name, kinds)
    m = Model(KevFile(kev("mutex_chain_nopi", DATA4)))
    inv = suspected_inversions(m, only=lambda k: m.label(k) == "ts_mutex/hi")
    sus = [x for x in inv["suspected"] if x["server"] == "ts_mutex/mrx"]
    assert len(sus) == 20 and inv["insufficient_evidence"] == [], (len(sus), inv["insufficient_evidence"])
    for x in sus:
        assert x["preempted_by"][0]["thread"] == "ts_mutex/hog" and x["preempted_by"][0]["prio"] == 20
        top = x["held_up"][0]
        assert top["thread"] == "ts_mslow/slow-rx" and top["prio"] == 10, top
        assert top["chain"].startswith("ts_mutex/mrx MUTEX-blocked on mutex ") and \
            "(owner ts_mutex/holder) -> ts_mutex/holder REPLY-blocked on ts_mslow (txn " in top["chain"], top
        assert "ts_mutex/holder" in x["evidence"] and "ts_mutex/hog (prio 20) on CPU 0" in x["evidence"]


def test_action_args_split_like_a_shell():
    """action.yml passes `args` to tracestudio check split as a shell would: quotes group words, wildcards stay
    literal, and an unbalanced quote fails the step before check runs."""
    import re
    with open(os.path.join(HERE, "..", "action.yml")) as f:
        text = f.read()
    step = text[text.index("- name: tracestudio check"):]
    head, body = re.search(r"\n( *)run: \|\n((?:\1  .*\n|\n)+)", step).groups()
    script = "\n".join(ln[len(head) + 2:] for ln in body.splitlines())
    work = tempfile.mkdtemp(prefix="ts-action-")
    shim = os.path.join(work, "bin")
    os.mkdir(shim)
    with open(os.path.join(shim, "tracestudio"), "w") as f:
        f.write("#!/bin/sh\nfor a in \"$@\"; do printf '%s\\n' \"$a\"; done\n")
    os.chmod(os.path.join(shim, "tracestudio"), 0o755)
    open(os.path.join(work, "ts_x"), "w").close()                 # what an unquoted ts_* would expand to
    os.symlink(sys.executable, os.path.join(shim, "python"))       # the action runs `python` from setup-python

    def run(args):
        env = dict(os.environ, PATH=os.pathsep.join([shim, os.environ.get("PATH", "")]), TS_TRACE="t.kev",
                   TS_ARGS=args, TS_JUNIT="r.xml", RUNNER_TEMP=work)
        return subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], cwd=work, env=env,
                              capture_output=True, text=True)
    r = run('--client "ts pload/" --no-inversions')
    assert r.returncode == 0 and r.stdout.splitlines() == [
        "check", "t.kev", "--client", "ts pload/", "--no-inversions", "--junit", "r.xml"], (r.stdout, r.stderr)
    r = run("--client ts_*")
    assert r.returncode == 0 and r.stdout.splitlines()[2:4] == ["--client", "ts_*"], r.stdout
    r = run('--client "ts pload/')
    assert r.returncode != 0 and r.stdout == "", (r.returncode, r.stdout)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    import test_synthetic   # noqa: E402  (runs the unit tests too when started directly)
    for name, fn in list(vars(test_synthetic).items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
