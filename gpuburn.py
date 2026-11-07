#!/usr/bin/python3
"""Load every GPU with floating-point work, to tune a fan curve under real heat.

Step 5 of INSTALL.md asks you to load the GPUs and watch where the temperature settles.
This does the loading. It needs no CUDA toolkit, no compiler and no pip packages: it
talks to libcuda.so.1 through ctypes and hands it hand-written PTX, which the driver
assembles itself.

  gpuburn.py                      load every GPU for 4 minutes, abort at 78 C
  gpuburn.py --seconds 600        longer, for a slow-moving chassis
  gpuburn.py --abort-temp 70      stop earlier
  gpuburn.py --fp64               double precision instead of single
  gpuburn.py --interval 5         report temperatures more often

It changes nothing: no fan duty, no power limit, no settings file. Run it as yourself,
not as root.

Three things stop it: the GPUs reaching --abort-temp, the --seconds deadline, or Ctrl+C.
The workers also carry the deadline themselves, so losing the terminal cannot leave the
GPUs loaded. --abort-temp defaults to two degrees below gpu-thermal-guard's warn_temp,
so a tuning run ends before the guard starts capping power and muddies the measurement.

What it does NOT do is reach the board's full power limit. A dependent chain of FMAs
saturates the FP32 pipes but not the whole card: on a 150 W-capped V100 it draws about
118 W. Treat the temperature it reaches as a firm lower bound, not the worst case, and
confirm against your real workload before trusting a curve's headroom.
"""
import argparse
import ctypes
import os
import signal
import subprocess
import sys
import time

# One kernel, two flavours. Each thread walks four independent FMA chains so there is
# enough instruction-level parallelism to keep the pipes busy, then stores the result so
# the whole loop cannot be optimised away.
_BODY = """
.version 6.4
.target sm_70
.address_size 64

.visible .entry burn(
\t.param .u64 burn_param_0,
\t.param .u32 burn_param_1
)
{{
\t.reg .pred \t%p<2>;
\t.reg .b32 \t%r<8>;
\t.reg .{reg} \t%{f}<10>;
\t.reg .b64 \t%rd<5>;

\tld.param.u64 \t%rd1, [burn_param_0];
\tld.param.u32 \t%r2, [burn_param_1];
\tcvta.to.global.u64 \t%rd2, %rd1;
\tmov.u32 \t%r3, %ctaid.x;
\tmov.u32 \t%r4, %ntid.x;
\tmov.u32 \t%r5, %tid.x;
\tmad.lo.s32 \t%r6, %r3, %r4, %r5;
\tcvt.rn.{reg}.s32 \t%{f}1, %r6;
\tmov.{reg} \t%{f}2, {one};
\tmov.{reg} \t%{f}3, {half};
\tmov.{reg} \t%{f}4, {one2};
\tmov.{reg} \t%{f}5, {one3};
\tmov.{reg} \t%{f}6, {one4};
\tmov.u32 \t%r7, 0;
$BURNLOOP:
\tfma.rn.{reg} \t%{f}1, %{f}1, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}4, %{f}4, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}5, %{f}5, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}6, %{f}6, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}1, %{f}1, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}4, %{f}4, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}5, %{f}5, %{f}2, %{f}3;
\tfma.rn.{reg} \t%{f}6, %{f}6, %{f}2, %{f}3;
\tadd.s32 \t%r7, %r7, 1;
\tsetp.lt.s32 \t%p1, %r7, %r2;
\t@%p1 bra \t$BURNLOOP;
\tadd.{reg} \t%{f}1, %{f}1, %{f}4;
\tadd.{reg} \t%{f}1, %{f}1, %{f}5;
\tadd.{reg} \t%{f}1, %{f}1, %{f}6;
\tmul.wide.s32 \t%rd3, %r6, {width};
\tadd.s64 \t%rd4, %rd2, %rd3;
\tst.global.{reg} \t[%rd4], %{f}1;
\tret;
}}
"""

PTX32 = _BODY.format(reg="f32", f="f", width=4, one="0f3F800001", half="0f3F000000",
                     one2="0f3F800002", one3="0f3F800003", one4="0f3F800004").encode()
PTX64 = _BODY.format(reg="f64", f="fd", width=8, one="0d3FF0000000000001",
                     half="0d3FE0000000000000", one2="0d3FF0000000000002",
                     one3="0d3FF0000000000003", one4="0d3FF0000000000004").encode()


def check(cu, rc, what):
    """Turn a CUDA driver return code into an exception, with the driver's own name for it."""
    if rc != 0:
        name = ctypes.c_char_p()
        try:
            cu.cuGetErrorName(rc, ctypes.byref(name))
            detail = (name.value or b"?").decode()
        except Exception:                                       # noqa: BLE001
            detail = "?"
        raise RuntimeError(f"{what} failed: rc={rc} ({detail})")


def worker(index, deadline, blocks, threads, iters, ptx):
    """Keep one GPU busy until the deadline. Runs in a forked child, and never returns.

    The deadline is passed in rather than signalled, so a worker stops on its own even if
    the parent is killed outright."""
    cu = ctypes.CDLL("libcuda.so.1")
    check(cu, cu.cuInit(0), "cuInit")
    dev = ctypes.c_int()
    check(cu, cu.cuDeviceGet(ctypes.byref(dev), index), "cuDeviceGet")
    ctx = ctypes.c_void_p()
    check(cu, cu.cuCtxCreate_v2(ctypes.byref(ctx), 0, dev), "cuCtxCreate")
    mod = ctypes.c_void_p()
    check(cu, cu.cuModuleLoadData(ctypes.byref(mod), ptx), "cuModuleLoadData")
    fn = ctypes.c_void_p()
    check(cu, cu.cuModuleGetFunction(ctypes.byref(fn), mod, b"burn"), "cuModuleGetFunction")
    out = ctypes.c_void_p()
    check(cu, cu.cuMemAlloc_v2(ctypes.byref(out), ctypes.c_size_t(blocks * threads * 8)),
          "cuMemAlloc")

    n = ctypes.c_int(iters)
    params = (ctypes.c_void_p * 2)(ctypes.cast(ctypes.byref(out), ctypes.c_void_p),
                                   ctypes.cast(ctypes.byref(n), ctypes.c_void_p))
    launches = 0
    while time.time() < deadline:
        check(cu, cu.cuLaunchKernel(fn, blocks, 1, 1, threads, 1, 1, 0, None, params, None),
              "cuLaunchKernel")
        check(cu, cu.cuCtxSynchronize(), "cuCtxSynchronize")
        launches += 1
    print(f"  GPU{index}: {launches} kernel launches", flush=True)
    os._exit(0)


def gpu_rows():
    """(index, temp C, draw W, utilization %, sm clock MHz) for every GPU."""
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,temperature.gpu,power.draw,"
                          "utilization.gpu,clocks.sm", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, timeout=20, check=True).stdout
    rows = []
    for line in out.strip().splitlines():
        idx, temp, draw, util, clk = (f.strip() for f in line.split(","))
        rows.append((int(idx), int(temp), float(draw), int(util), clk))
    return rows


def main():
    ap = argparse.ArgumentParser(description="Load the GPUs, to tune a fan curve under heat.")
    ap.add_argument("--seconds", type=float, default=240, help="how long to load (default 240)")
    ap.add_argument("--abort-temp", type=int, default=78, metavar="C",
                    help="stop if any GPU reaches this (default 78)")
    ap.add_argument("--interval", type=float, default=15, metavar="SECS",
                    help="seconds between temperature reports (default 15)")
    ap.add_argument("--fp64", action="store_true", help="double-precision chains")
    ap.add_argument("--blocks", type=int, default=2560, help="blocks per launch (default 2560)")
    ap.add_argument("--threads", type=int, default=512, help="threads per block (default 512)")
    ap.add_argument("--iters", type=int, default=200000, help="loop trips per launch")
    args = ap.parse_args()

    try:
        rows = gpu_rows()
    except (OSError, subprocess.SubprocessError) as e:
        sys.exit(f"Can't read the GPUs with nvidia-smi: {e}")
    if not rows:
        sys.exit("nvidia-smi reported no GPUs")
    hot = [(i, t) for i, t, *_ in rows if t >= args.abort_temp]
    if hot:
        sys.exit(f"GPU{hot[0][0]} is already at {hot[0][1]} C, at or above the "
                 f"{args.abort_temp} C abort temperature. Let it cool first.")

    print(f"Loading {len(rows)} GPU(s) with {'FP64' if args.fp64 else 'FP32'} for "
          f"{args.seconds:.0f} s, aborting at {args.abort_temp} C. Ctrl+C stops it.")
    deadline = time.time() + args.seconds
    live = set()
    for i, *_ in rows:
        pid = os.fork()
        if pid == 0:
            try:
                worker(i, deadline, args.blocks, args.threads, args.iters,
                       PTX64 if args.fp64 else PTX32)
            except Exception as e:                              # noqa: BLE001
                print(f"  GPU{i}: {e}", flush=True)
                os._exit(1)
        live.add(pid)

    def reap():
        for pid in list(live):
            try:
                if os.waitpid(pid, os.WNOHANG)[0]:
                    live.discard(pid)
            except ChildProcessError:
                live.discard(pid)

    def kill_live(why):
        if live:
            print(f"\nStopping: {why}", flush=True)
            for pid in list(live):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    aborted, started, last = None, time.time(), 0.0
    try:
        while time.time() < deadline:
            reap()
            if not live:
                break                                   # every worker gave up early
            time.sleep(1)
            if time.time() - last < args.interval:
                continue
            last = time.time()
            rows = gpu_rows()
            print(f"t+{last - started:4.0f}s  " + "  ".join(
                f"GPU{i} {t}C {d:.0f}W {u}% {clk}MHz" for i, t, d, u, clk in rows), flush=True)
            hot = [(i, t) for i, t, *_ in rows if t >= args.abort_temp]
            if hot:
                aborted = f"GPU{hot[0][0]} reached {hot[0][1]} C (abort at {args.abort_temp} C)"
                kill_live(aborted)
                break
    except KeyboardInterrupt:
        aborted = "interrupted"
        kill_live(aborted)
    finally:
        grace = time.time() + 5                         # they carry the deadline themselves
        while live and time.time() < grace:
            reap()
            if live:
                time.sleep(0.2)
        kill_live("workers did not exit on their own")
        for pid in list(live):
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    if aborted:
        print(f"\nABORTED: {aborted}")
        print("The curve did not hold the temperature. Raise the middle points and retry.")
        return 1
    print(f"\nHeld the full {args.seconds:.0f} s without reaching {args.abort_temp} C.")
    print("Check where the hottest GPU settled: aim for at least 5 C below its maximum\n"
          "operating temperature (83 C on a V100, so 76-78 C is comfortable).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
