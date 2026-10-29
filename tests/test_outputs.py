"""Behavioral verification of the submitted recovery component."""
import copy
import json
import os
from pathlib import Path
import random
import resource
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from model import hand_cases, scenario, wander

HERE = Path(__file__).resolve().parent
IS_LINUX_ROOT = sys.platform.startswith('linux') and os.geteuid() == 0
OUTPUT_LIMIT = 16 * 1024 ** 2


def restrict(compile=False):
    def apply():
        if IS_LINUX_ROOT:
            os.setgroups([])
            os.setgid(65534)
            os.setuid(65534)
            memory = 1024 ** 3 if compile else 512 * 1024 ** 2
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        # The worker may not write files; its output goes through a pipe.
        size = 64 * 1024 ** 2 if compile else 0
        resource.setrlimit(resource.RLIMIT_FSIZE, (size, size))
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60) if compile else (10, 10))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    return apply


def execute(args, folder, text='', compile=False):
    env = {'PATH': '/usr/bin:/bin:/usr/local/bin', 'LC_ALL': 'C', 'HOME': str(folder), 'TMPDIR': str(folder)}
    limit = 60 if compile else 10
    deadline = time.monotonic() + limit
    p = subprocess.Popen(args, cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, start_new_session=True, preexec_fn=restrict(compile))

    def feed():
        try:
            p.stdin.write(text.encode())
            p.stdin.close()
        except OSError:
            pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    out, err, failure = bytearray(), bytearray(), None
    with selectors.DefaultSelector() as sel:
        sel.register(p.stdout, selectors.EVENT_READ, out)
        sel.register(p.stderr, selectors.EVENT_READ, err)
        try:
            while sel.get_map() and failure is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    failure = 'compile timeout (60 s)' if compile else 'execution timeout (10 s)'
                    break
                for key, _ in sel.select(left):
                    chunk = os.read(key.fileobj.fileno(), 1 << 16)
                    if not chunk:
                        sel.unregister(key.fileobj)
                    elif key.data is out:
                        out += chunk
                        if len(out) > OUTPUT_LIMIT:
                            failure = 'output limit'
                            break
                    elif len(err) < 1000:
                        err += chunk
            if failure is None:
                p.wait(max(.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failure = 'execution timeout (10 s)'
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
            writer.join(1)
            for stream in (p.stdin, p.stdout, p.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
    assert failure is None, failure
    assert p.returncode == 0, f'process exit {p.returncode}: {bytes(err[:1000])!r}'
    try:
        return out.decode('ascii')
    except UnicodeDecodeError:
        raise AssertionError('non-ASCII output')


class Runner:
    def __init__(self, source):
        self.temp = tempfile.TemporaryDirectory(prefix='recovery-build-', dir='/tmp')
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o755)
        if IS_LINUX_ROOT:
            os.chown(self.folder, 65534, 65534)
        for name in ['api.hpp', 'driver.cpp']:
            shutil.copyfile(HERE / name, self.folder / name)
            (self.folder / name).chmod(0o644)
        shutil.copyfile(source, self.folder / 'recovery.cpp')
        (self.folder / 'recovery.cpp').chmod(0o644)
        execute(['c++', '-std=c++17', '-O2', '-Wall', '-Wextra', '-Wpedantic', 'driver.cpp', 'recovery.cpp',
                 '-o', 'worker'], self.folder, compile=True)
        if IS_LINUX_ROOT:
            os.chown(self.folder, 0, 0)
        self.calls = 0

    def actions(self, state):
        self.calls += 1
        lines = execute([str(self.folder / 'worker')], self.folder, state.text()).splitlines()
        assert len(lines) <= 12 * len(state.log) + 8 * 128 + 2 * len(state.pages), 'action count limit'
        return lines

    def finish(self, state):
        d = copy.deepcopy(state)
        for line in self.actions(d):
            d.apply(line)
        d.complete()
        again = self.actions(d)
        assert not any(line.startswith('A') for line in again), 'recovery of a recovered state appended records'
        for line in again:
            d.apply(line)
        d.complete()
        return d


def generated_specs():
    modes = ['low', 'cross', 'high', 'top']
    specs = []
    for i in range(96):
        specs.append(dict(seed=5100 + i, pages=1 + (i * 5) % 9, txs=1 + (i * 7) % 37, steps=8 + (i * 29) % 170,
                          lsn=modes[i % 4], nested=(0, .1, .25)[i % 3], truncate=(0, .6, .95)[(i // 3) % 3],
                          warmup=(0, 2, 6)[(i // 2) % 3], equal=(.02, .2)[(i // 5) % 2],
                          swaps=(0, .08, .3)[(i // 4) % 3]))
    return specs


def prefix_specs():
    return [dict(seed=6100, pages=1, txs=3, steps=12, lsn='cross', nested=.3),
            dict(seed=6101, pages=2, txs=5, steps=20, lsn='top', nested=.2, truncate=.9, warmup=3),
            dict(seed=6102, pages=1, txs=6, steps=30, lsn='low', nested=.15, equal=.3),
            dict(seed=6103, pages=3, txs=9, steps=40, lsn='high', nested=.1, truncate=.6, warmup=2),
            dict(seed=6104, pages=2, txs=4, steps=25, lsn='low', nested=.25, warmup=1),
            dict(seed=6105, pages=4, txs=12, steps=60, lsn='top', nested=.12, truncate=.9, warmup=4)]


def foreign_specs():
    modes = ['low', 'cross', 'high', 'top']
    return [dict(seed=7100 + i, pages=1 + i % 5, txs=2 + (i * 3) % 23, steps=15 + (i * 17) % 90,
                 lsn=modes[i % 4], nested=(.1, .25)[i % 2], truncate=(0, .8)[(i // 2) % 2], warmup=i % 3,
                 swaps=(.05, .2)[(i // 4) % 2])
            for i in range(48)]


def scale_specs():
    return [dict(seed=8100, pages=64, txs=128, steps=9000, lsn='top', nested=.08, life=6),
            dict(seed=8101, pages=32, txs=128, steps=9000, lsn='high', nested=.15, truncate=1, warmup=40, life=14),
            dict(seed=8102, pages=64, txs=128, steps=9000, lsn='low', nested=.0, equal=.1, life=14)]


WATCHDOG = 16


def watchdog(runner, state):
    """Cut every attempt after WATCHDOG actions; each cut attempt must make progress."""
    for _ in range(5000):
        actions = runner.actions(state)
        if state.done() or len(actions) <= WATCHDOG:
            for line in actions:
                state.apply(line)
            state.complete()
            return
        durable, lsns = state.progress()
        for line in actions[:WATCHDOG]:
            state.apply(line)
        moved = state.durable > durable or any(n > m for (n, _), m in zip(state.pages, lsns))
        assert moved, f'no durable progress within the first {WATCHDOG} actions'
        state = state.crash()
    raise AssertionError('recovery did not finish under the watchdog')


def watchdog_specs():
    modes = ['low', 'cross', 'high', 'top']
    return [dict(seed=9100 + i, pages=1 + i % 6, txs=3 + (i * 5) % 30, steps=20 + (i * 23) % 140,
                 lsn=modes[i % 4], nested=(.1, .2)[i % 2], truncate=(0, .7)[(i // 2) % 2], warmup=i % 4,
                 swaps=(.05, .2)[(i // 3) % 2]) for i in range(24)]


def cut_points(n):
    return range(n + 1) if n <= 64 else sorted({i * n // 64 for i in range(65)})


def run_group(runner, group):
    if group == 'hand':
        for case in hand_cases():
            runner.finish(case)
        runner.finish(scenario(7, pages=2, txs=0, steps=0))
    elif group == 'generated':
        for spec in generated_specs():
            runner.finish(scenario(**spec))
    elif group == 'every_prefix':
        cases = list(hand_cases()) + [scenario(**spec) for spec in prefix_specs()]
        for case in cases:
            actions = runner.actions(case)
            for cut in cut_points(len(actions)):
                d = copy.deepcopy(case)
                for line in actions[:cut]:
                    d.apply(line)
                runner.finish(d.crash())
    elif group == 'foreign':
        # Durable states left by interrupted recovery attempts that made
        # different, but legal, choices.
        for spec in foreign_specs():
            rng = random.Random(spec['seed'] * 31)
            state = scenario(**spec)
            for _ in range(rng.randrange(1, 4)):
                state = wander(state, rng, rng.randrange(1, 60)).crash()
            runner.finish(state)
    elif group == 'watchdog':
        for spec in watchdog_specs():
            watchdog(runner, scenario(**spec))
        for case in hand_cases():
            watchdog(runner, case)
    elif group == 'repeated':
        for seed in range(12):
            state = scenario(930 + seed, pages=3, txs=9, steps=60, lsn='high', nested=.15,
                             truncate=.5, warmup=seed % 3)
            for depth in range(6):
                actions = runner.actions(state)
                # Depends only on seed, depth and action count.
                cut = (seed * 11 + depth * 7 + 1) % (len(actions) + 1)
                for line in actions[:cut]:
                    state.apply(line)
                state = state.crash()
            runner.finish(state)
    elif group == 'scale':
        for spec in scale_specs():
            runner.finish(scenario(**spec))
        big = scenario(8103, pages=48, txs=128, steps=9000, lsn='cross', nested=.1, truncate=1, warmup=16, life=14)
        actions = runner.actions(big)
        for cut in sorted({i * len(actions) // 16 for i in range(17)}):
            d = copy.deepcopy(big)
            for line in actions[:cut]:
                d.apply(line)
            runner.finish(d.crash())
    else:
        raise ValueError(group)


def test_hand_cases():
    """Hand-computed histories."""
    run_group(shared(), 'hand')


def test_generated_histories():
    """Generated histories."""
    run_group(shared(), 'generated')


def test_every_crash_prefix():
    """Restart after each prefix of the returned actions."""
    run_group(shared(), 'every_prefix')


def test_foreign_interrupted_states():
    """Restart from states left by other legal recovery attempts."""
    run_group(shared(), 'foreign')


def test_watchdog_progress():
    """Every attempt is cut short; recovery must still finish."""
    run_group(shared(), 'watchdog')


def test_repeated_crashes():
    """Several consecutive interrupted attempts."""
    run_group(shared(), 'repeated')


def test_scale_and_integer_width():
    """Large histories."""
    run_group(shared(), 'scale')


_shared = None


def shared():
    global _shared
    if _shared is None:
        _shared = Runner('/app/recovery.cpp')
    return _shared


if __name__ == '__main__':
    start = time.perf_counter()
    runner = Runner(Path(sys.argv[1]).resolve())
    groups = sys.argv[2:] or ['hand', 'generated', 'every_prefix', 'foreign', 'watchdog', 'repeated', 'scale']
    rows = []
    for group in groups:
        t = time.perf_counter()
        try:
            run_group(runner, group)
            row = {'group': group, 'pass': True}
        except Exception as e:
            row = {'group': group, 'pass': False, 'error': f'{type(e).__name__}: {e}'}
        row['seconds'] = round(time.perf_counter() - t, 2)
        rows.append(row)
    scale = 1024 ** 2 if sys.platform == 'darwin' else 1024
    print(json.dumps({'groups': rows, 'worker_invocations': runner.calls,
                      'seconds': round(time.perf_counter() - start, 2),
                      'verifier_peak_MiB': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale,
                      'native_children_peak_MiB': resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / scale,
                      'privilege_isolation_exercised': IS_LINUX_ROOT}, indent=2))
    sys.exit(0 if all(r['pass'] for r in rows) else 1)
