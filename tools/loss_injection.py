#!/usr/bin/env python3
# Copyright 2026 The trace-studio authors
# SPDX-License-Identifier: Apache-2.0
"""Loss injection: damage real captures the way a lossy trace pipeline does and count every answer trace-studio
gets wrong on what is left.

Each damaged copy is compared with the undamaged capture it came from (the reference):

- links: every exchange the damaged trace calls trustworthy must exist in the reference, with the same server
  thread, rcvid, coid, replier, parent, status and timestamps. One that differs is a wrong link.
- inversions: a request the damaged trace calls a suspected priority inversion must be one in the reference too,
  with the same preemptor; a request the damaged trace checked and found clean must be clean in the reference.
  "insufficient evidence" and "untrustworthy" are allowed answers. Anything else is a wrong verdict.
- mutex owners: an owner the damaged trace names must be the reference's owner.
- the workload logs (where the capture has them): no link check of `validate` may say WRONG.
- check: when the decoder reports lost events, `tracestudio check` must fail its trace integrity assertion.

Damage kinds. The first group leaves marks the decoder can see. The second group cannot be seen in principle (the
file is a valid trace of a shorter or reordered run); for those only the never-wrong rules above apply.

    detectable:   drop_buffers, drop_slots, kernel_dropped, truncate_mid_slot, truncate_mid_buffer, swap_same_cpu
    undetectable: drop_tail, truncate_at_buffer, reorder_cpus, reorder_slots

    python3 tools/loss_injection.py tests/data --seeds 8 --json out.json
"""
import argparse
import contextlib
import glob
import gzip
import io
import json
import os
import random
import struct
import sys
import tempfile
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tracestudio.analyses import Timeline, suspected_inversions  # noqa: E402
from tracestudio.cli import main as tracestudio_main  # noqa: E402
from tracestudio.kev import C_CONTROL, CTRL_BUFFER, CTRL_DROPPED, CTRL_TIME, KevFile  # noqa: E402
from tracestudio.model import Model  # noqa: E402
from tracestudio.validate import validate  # noqa: E402

DETECTABLE = ("drop_buffers", "drop_slots", "kernel_dropped", "truncate_mid_slot", "truncate_mid_buffer",
              "swap_same_cpu")
UNDETECTABLE = ("drop_tail", "truncate_at_buffer", "reorder_cpus", "reorder_slots")
KINDS = DETECTABLE + UNDETECTABLE
LINK_CHECKS = ("server_pid", "server_tid", "rcvid", "coid", "payload_id", "parent_link")


# ---- damage -------------------------------------------------------------------------------------------------
def _is_ctrl(h, code=None):
    return h >> 30 == 0 and (h >> 10) & 0x1F == C_CONTROL and (code is None or h & 0x3FF == code)


def _layout(kf):
    """(header bytes, slots [(h, d0, d1, d2)], buffers [(first, end, cpu, index of the BUFFER slot)])"""
    slots = [(h, d0, d1, d2) for _, h, d0, d1, d2 in kf.slots()]
    heads = []
    for i, (h, _, _, _) in enumerate(slots):
        if _is_ctrl(h, CTRL_BUFFER):
            cpu = (h >> 24) & 0x3F
            p = slots[i - 1][0] if i else 0
            first = i - 1 if i and _is_ctrl(p, CTRL_TIME) and (p >> 24) & 0x3F == cpu else i
            heads.append((first, cpu, i))
    bufs = [(a, heads[n + 1][0] if n + 1 < len(heads) else len(slots), cpu, bi)
            for n, (a, cpu, bi) in enumerate(heads)]
    return bytes(kf.data[:kf.events_offset]), slots, bufs


def _pack(head, slots):
    return head + b"".join(struct.pack("<4I", *s) for s in slots)


def _body(slots, buf):
    """Slot indexes of a buffer after its TIME and BUFFER slots."""
    return range(buf[3] + 1, buf[1])


def _simple_event(h):
    return h >> 30 == 0 and not _is_ctrl(h)


def damage(kf, kind, rng):
    """(damaged bytes, what was done) or (None, reason) when the capture has no room for this kind."""
    head, slots, bufs = _layout(kf)
    if len(bufs) < 4:
        return None, "fewer than 4 buffers"
    per_cpu = {}
    for n, b in enumerate(bufs):
        per_cpu.setdefault(b[2], []).append(n)
    inner = [n for n in range(len(bufs)) if per_cpu[bufs[n][2]][0] != n and per_cpu[bufs[n][2]][-1] != n]
    with_body = [n for n in inner if len(_body(slots, bufs[n])) >= 8]

    if kind in ("drop_buffers", "drop_tail"):
        if kind == "drop_buffers":
            drop = set(rng.sample(inner, min(len(inner), rng.randint(1, 4))))
        else:                        # the last 1 to 3 buffers of the file, as if the logger stopped early
            drop = set(range(len(bufs) - rng.randint(1, 3), len(bufs)))
        out = [s for n, b in enumerate(bufs) if n not in drop for s in slots[b[0]:b[1]]]
        return _pack(head, slots[:bufs[0][0]] + out), "dropped buffers %s of %d" % (sorted(drop), len(bufs))

    if kind == "drop_slots":         # slots missing inside 1 to 3 buffers; the BUFFER slot counts stay as written
        gone, what = set(), []
        for n in rng.sample(with_body, min(len(with_body), rng.randint(1, 3))):
            body = list(_body(slots, bufs[n]))
            k = rng.randint(1, min(20, len(body) // 2))
            gone.update(rng.sample(body, k))
            what.append("%d from buffer %d" % (k, n))
        return _pack(head, [s for i, s in enumerate(slots) if i not in gone]), "removed slots: " + ", ".join(what)

    if kind == "kernel_dropped":     # a run of events replaced by CONTROL/DROPPED, slot count patched, as the kernel
        n = rng.choice(with_body)
        a, b, cpu, bi = bufs[n]
        body = list(_body(slots, bufs[n]))
        k = rng.randint(1, min(50, len(body) - 1))
        s0 = rng.randint(body[0], body[-1] - k + 1)
        drop_slot = ((cpu << 24) | (C_CONTROL << 10) | CTRL_DROPPED, slots[s0][1], k, 0)
        h, d0, d1, d2 = slots[bi]
        out = slots[:bi] + [(h, d0, d1, d2 - k + 1)] + slots[bi + 1:s0] + [drop_slot] + slots[s0 + k:]
        return _pack(head, out), "buffer %d: %d slots from slot %d replaced by DROPPED" % (n, k, s0)

    if kind.startswith("truncate"):
        n = rng.choice(inner)
        a, b, _, bi = bufs[n]
        data = _pack(head, slots)
        if kind == "truncate_at_buffer":
            cut = len(head) + 16 * a
        elif kind == "truncate_mid_buffer":
            cut = len(head) + 16 * rng.randint(bi + 2, b - 1) if b - bi > 2 else None
        else:
            cut = len(head) + 16 * rng.randint(a, b - 1) + rng.randint(1, 15)
        if cut is None:
            return None, "buffer %d too short" % n
        return data[:cut], "cut at byte %d (buffer %d of %d)" % (cut, n, len(bufs))

    if kind == "reorder_cpus":       # buffers of different CPUs interleaved in another order; per-CPU order kept
        order = list(range(len(bufs)))
        swaps = 0
        for _ in range(rng.randint(1, 20)):
            i = rng.randrange(len(order) - 1)
            if bufs[order[i]][2] != bufs[order[i + 1]][2]:
                order[i], order[i + 1] = order[i + 1], order[i]
                swaps += 1
        if not swaps:
            return None, "no adjacent buffers of different CPUs picked"
        out = [s for n in order for s in slots[bufs[n][0]:bufs[n][1]]]
        return _pack(head, slots[:bufs[0][0]] + out), "%d swaps of adjacent buffers of different CPUs" % swaps

    if kind == "swap_same_cpu":      # two consecutive buffers of one CPU written in the wrong order
        cpus = [c for c, ns in per_cpu.items() if len(ns) >= 3]
        if not cpus:
            return None, "no CPU with 3 buffers"
        ns = per_cpu[rng.choice(cpus)]
        j = rng.randrange(len(ns) - 1)
        x, y = ns[j], ns[j + 1]
        order = list(range(len(bufs)))
        order[x], order[y] = y, x
        out = [s for n in order for s in slots[bufs[n][0]:bufs[n][1]]]
        return _pack(head, slots[:bufs[0][0]] + out), "buffers %d and %d of CPU %d swapped" % (x, y, bufs[x][2])

    if kind == "reorder_slots":      # windows of plain events shuffled inside a buffer (no control, no combine slots)
        out = list(slots)
        what = 0
        for n in rng.sample(with_body, min(len(with_body), rng.randint(1, 5))):
            body = list(_body(slots, bufs[n]))
            s0 = rng.choice(body)
            w = [i for i in range(s0, min(s0 + 8, body[-1] + 1)) if _simple_event(slots[i][0])]
            w = w[:next((k for k in range(1, len(w)) if w[k] != w[k - 1] + 1), len(w))]   # contiguous only
            if len(w) < 2:
                continue
            perm = w[:]
            rng.shuffle(perm)
            for i, p in zip(w, perm):
                out[i] = slots[p]
            what += 1
        if not what:
            return None, "no window of plain events"
        return _pack(head, out), "%d windows of up to 8 plain events shuffled" % what

    raise ValueError(kind)


# ---- the answers compared ------------------------------------------------------------------------------------
def answers(model, logs=None):
    tl = Timeline(model)
    inv = suspected_inversions(model, tl=tl)
    sus = {r["txn"]: r for r in inv["suspected"]}
    unclear = {r["txn"] for r in inv["insufficient_evidence"]}
    out = {"model": model, "suspected": sus, "unclear": unclear,
           "checked_clean": {t.idx for t in model.txns
                             if t.server is not None and t.t_call is not None and t.t_exit is not None and t.causal
                             and t.idx not in sus and t.idx not in unclear}}
    if logs:
        with contextlib.redirect_stdout(io.StringIO()):
            res, _, _ = validate(model, logs)
        out["validate"] = res
    return out


def _key(t):
    return (t.client, t.t_call)


def _link(t):
    return (t.server, t.server_pid, t.rcvid, t.coid, t.replier, t.client_block, t.t_rx_exit, t.t_reply_call, t.t_exit,
            t.status)


def compare(ref, dmg):
    """Counts of wrong answers in dmg against ref, with examples."""
    rm, dm = ref["model"], dmg["model"]
    r_by = {_key(t): t for t in rm.txns}
    d_by = {_key(t): t for t in dm.txns}
    r_key = {t.idx: _key(t) for t in rm.txns}
    d_key = {t.idx: _key(t) for t in dm.txns}
    c = Counter()
    ex = []

    def wrong(what, kind):
        c["wrong_link"] += 1
        c["wrong_link_" + kind] += 1
        ex.append(what)
    for t in dm.txns:
        if not t.trustworthy:
            continue
        c["trusted"] += 1
        o = r_by.get(_key(t))
        if o is None:
            wrong("txn %d: no such exchange in the reference" % t.idx, "absent")
            continue
        if not o.trustworthy:
            c["trusted_where_reference_is_not"] += 1
            ex.append("txn %d: reference txn %d is untrustworthy (%s)" % (t.idx, o.idx, o.warnings))
        if _link(t) != _link(o):
            wrong("txn %d: %s vs reference %s" % (t.idx, _link(t), _link(o)), "fields")
            continue
        dp = None if t.parent is None else d_key[t.parent]
        rp = None if o.parent is None else r_key[o.parent]
        if dp != rp:
            if dp is None:
                # the parent link was not made: an omission, harmless unless the parent is trusted in dmg too
                p = d_by.get(rp)
                c["parent_not_linked"] += 1
                if p is not None and p.trustworthy:
                    wrong("txn %d: no parent, reference parent %s is trusted here as txn %d" % (t.idx, rp, p.idx), "parent")
            else:
                trusted_parent = dm.txns[t.parent].trustworthy
                wrong("txn %d: parent %s (%s), reference %s" % (
                    t.idx, dp, "trusted" if trusted_parent else "untrusted", rp),
                    "parent" if trusted_parent else "parent_untrusted")
            continue
        if {d_key[x] for x in t.children} != {r_key[x] for x in o.children}:
            wrong("txn %d: nested requests %d, reference %d" % (t.idx, len(t.children), len(o.children)), "nested")
    d_lab = {dm.label(k): k for k in dm.threads}       # a thread unnamed in dmg is the same key as in ref

    def ref_label(lab):
        return rm.label(d_lab[lab]) if lab in d_lab else lab
    for i, row in dmg["suspected"].items():
        c["suspected"] += 1
        o = r_by.get(d_key[i])
        orow = ref["suspected"].get(o.idx) if o else None
        pre = {ref_label(p["thread"]) for p in row["preempted_by"]}
        if orow is None:
            c["wrong_inversion"] += 1
            ex.append("txn %d: suspected inversion, reference says %s" % (
                i, "no such exchange" if o is None else "clean" if o.idx in ref["checked_clean"]
                else "insufficient evidence" if o.idx in ref["unclear"] else "not checked"))
        elif not pre <= {p["thread"] for p in orow["preempted_by"]}:
            c["wrong_inversion"] += 1
            ex.append("txn %d: preempted by %s, reference %s" % (i, pre, [p["thread"] for p in orow["preempted_by"]]))
    for i in dmg["checked_clean"]:
        c["checked_clean"] += 1
        o = r_by.get(d_key[i])
        if o is not None and o.idx in ref["suspected"]:
            c["missed_inversion"] += 1
            ex.append("txn %d: checked clean, reference txn %d is a suspected inversion" % (i, o.idx))
    r_w = {(w.waiter, w.t_call): w for w in rm.mutex_waits}
    for w in dm.mutex_waits:
        if not w.owner_known:
            continue
        c["owners"] += 1
        o = r_w.get((w.waiter, w.t_call))
        if o is None or not o.owner_known or (o.owner, o.t_release) != (w.owner, w.t_release):
            c["wrong_owner"] += 1
            ex.append("wait %d: owner %s, reference %s" % (w.idx, w.owner, o and o.owner))
    if "validate" in dmg:
        bad = [k for k in dmg["validate"]["checks"] if k.startswith(LINK_CHECKS) and k.endswith(":WRONG")]
        bad += [k for k in dmg["validate"]["checks"] if k == "WRONG_unflagged"]
        c["validate_wrong"] += len(bad)
        ex += ["validate: " + k for k in bad]
    return c, ex


def check_exit(path, allow_loss=False):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return tracestudio_main(["check", path, "--no-inversions", "--max-insufficient", "1000000",
                                 "--min-checked", "0"] + (["--allow-loss"] if allow_loss else []))


def captures(root):
    for gz in sorted(glob.glob(os.path.join(root, "*", "*.kev.gz"))):
        logs = gz[:-len(".kev.gz")] + ".logs"
        yield os.path.relpath(gz[:-len(".kev.gz")], root), gz, logs if os.path.isdir(logs) else None


def run_capture(name, gz, logs, seeds, kinds, tmp):
    src = os.path.join(tmp, os.path.basename(name) + ".kev")
    with gzip.open(gz) as f, open(src, "wb") as g:
        g.write(f.read())
    kf = KevFile(src)
    ref_model = Model(kf)
    ref = answers(ref_model, logs)
    ref_losses = len(kf.losses)
    ref_exit = check_exit(src)
    rows = []
    for kind in kinds:
        for seed in range(seeds):
            rng = random.Random("%s/%s/%d" % (name, kind, seed))
            data, what = damage(kf, kind, rng)
            row = {"capture": name, "kind": kind, "seed": seed, "what": what}
            if data is None:
                row["skipped"] = True
                rows.append(row)
                continue
            path = os.path.join(tmp, "damaged.kev")
            with open(path, "wb") as f:
                f.write(data)
            dk = KevFile(path)
            try:
                dmg = answers(Model(dk), logs)
            except Exception as e:          # a crash is a failure of its own, reported as such
                row.update(crash="%s: %s" % (type(e).__name__, e))
                rows.append(row)
                continue
            c, ex = compare(ref, dmg)
            losses = Counter(l.kind for l in dk.losses)
            ex_code = check_exit(path)
            row.update(losses=dict(losses), detected=bool(dk.losses), counts=dict(c), examples=ex[:5],
                       check_exit=ex_code, reference_check_exit=ref_exit, reference_losses=ref_losses,
                       check_integrity_ok=(ex_code != 0) if dk.losses else True)
            rows.append(row)
    return rows


def summarise(rows):
    s = Counter()
    by_kind = {}
    for r in rows:
        k = by_kind.setdefault(r["kind"], Counter())
        if r.get("skipped"):
            k["skipped"] += 1
            continue
        k["damaged"] += 1
        if r.get("crash"):
            k["crashes"] += 1
            continue
        k["loss_reported"] += r["detected"]
        k["check_passed_with_loss"] += not r["check_integrity_ok"]
        for f in ("trusted", "wrong_link", "wrong_link_fields", "wrong_link_parent", "wrong_link_parent_untrusted",
                  "wrong_link_nested", "wrong_link_absent", "parent_not_linked", "trusted_where_reference_is_not", "suspected", "wrong_inversion",
                  "checked_clean", "missed_inversion", "owners", "wrong_owner", "validate_wrong"):
            k[f] += r["counts"].get(f, 0)
    for k in by_kind.values():
        s.update(k)
    return dict(s), {k: dict(v) for k, v in by_kind.items()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="a directory of RUN/NAME.kev.gz captures (tests/data)")
    ap.add_argument("--seeds", type=int, default=8, help="damaged copies per capture and kind (default 8)")
    ap.add_argument("--kinds", default=",".join(KINDS))
    ap.add_argument("--only", help="only captures whose name contains this text")
    ap.add_argument("--json", help="write every damaged copy with its counts (JSON)")
    a = ap.parse_args(argv)
    kinds = a.kinds.split(",")
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for name, gz, logs in captures(a.root):
            if a.only and a.only not in name:
                continue
            rs = run_capture(name, gz, logs, a.seeds, kinds, tmp)
            t, _ = summarise(rs)
            print("%-28s damaged %4d  loss reported %4d  wrong links %d  wrong inversions %d  missed inversions %d"
                  "  wrong owners %d  validate WRONG %d  check passed with loss %d  crashes %d" % (
                      name, t.get("damaged", 0), t.get("loss_reported", 0), t.get("wrong_link", 0),
                      t.get("wrong_inversion", 0), t.get("missed_inversion", 0), t.get("wrong_owner", 0),
                      t.get("validate_wrong", 0), t.get("check_passed_with_loss", 0), t.get("crashes", 0)),
                  flush=True)
            rows += rs
    total, by_kind = summarise(rows)
    print("\nby kind:")
    for k, v in by_kind.items():
        print("  %-20s %s" % (k, json.dumps(v, sort_keys=True)))
    groups = {g: summarise([r for r in rows if r["kind"] in ks])[0]
              for g, ks in (("detectable", DETECTABLE), ("undetectable", UNDETECTABLE))}
    for g, t in groups.items():
        print("%s: %s" % (g, json.dumps(t, sort_keys=True)))
    print("total: " + json.dumps(total, sort_keys=True))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"total": total, "groups": groups, "by_kind": by_kind, "rows": rows}, f, indent=1, default=str)
    return 0 if wrong_answers(total) == 0 else 1


def wrong_answers(t):
    return sum(t.get(f, 0) for f in ("wrong_link", "wrong_inversion", "missed_inversion", "wrong_owner",
                                     "validate_wrong", "check_passed_with_loss", "crashes"))


if __name__ == "__main__":
    sys.exit(main())
