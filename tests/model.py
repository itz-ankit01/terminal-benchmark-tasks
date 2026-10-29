"""Trusted virtual device and forward history generator for the recovery verifier.

The generator tracks the committed value of every cell while it writes a
history, so expected results never come from a recovery algorithm. The device
checks every recovery action against the contract.
"""
import bisect
import copy
import random

UPDATE, CLR, COMMIT, ABORT, END, PREPARE, NESTED, SWAP = range(1, 9)
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1
LSN_CEILING = (1 << 64) - 1000000
# Record tuple: lsn,kind,tx,prev,undo,page,slot,before,after,target,page2,slot2.
CHANGES = (UPDATE, CLR, SWAP)


def _norm(r):
    r = tuple(r)
    return r + (0, 0) if len(r) == 10 else r


class Device:
    def __init__(self, base, initial, pages, log, decisions, expected, prepared, discarded=()):
        self.base = base
        self.initial = copy.deepcopy(initial)
        self.pages = [(n, list(v)) for n, v in pages]
        self.discarded = [_norm(r) for r in discarded]
        self.decisions = dict(decisions)
        self.expected = copy.deepcopy(expected)
        self.prepared = set(prepared)
        self.versions = {}
        self.latest = {}
        self.history = {}
        for p, v in enumerate(self.initial):
            self.versions[p, base] = list(v)
            self.latest[p] = (base, list(v))
            self.history[p] = [base]
        self.floor = base
        self.swap_lsns, self.swap_cells = [], []
        for r in self.discarded:
            self._version(r)
            self.floor = r[0]
        self.log, self.index, self.position, self.states = [], {}, {}, {}
        for r in log:
            self._append(_norm(r))
        self.durable = len(self.log)

    def _version(self, r):
        if r[1] in (UPDATE, CLR):
            writes = {r[5]: [(r[6], r[8])]}
        elif r[1] == SWAP:
            writes = {r[5]: [(r[6], r[8])]}
            writes.setdefault(r[10], []).append((r[11], r[7]))
            self.swap_lsns.append(r[0])
            self.swap_cells.append(((r[5], r[6]), (r[10], r[11])))
        else:
            return
        for p, cells in writes.items():
            v = list(self.latest[p][1])
            for s, value in cells:
                v[s] = value
            self.versions[p, r[0]] = v
            self.latest[p] = (r[0], v)
            self.history[p].append(r[0])

    def where(self, update):
        """Cell that holds the item an UPDATE changed, after every later SWAP."""
        cell = (update[5], update[6])
        for a, b in self.swap_cells[bisect.bisect_right(self.swap_lsns, update[0]):]:
            cell = b if cell == a else a if cell == b else cell
        return cell

    def _append(self, r):
        n, k, t = r[0], r[1], r[2]
        self.log.append(r)
        self.index[n] = r
        self.position[n] = len(self.log) - 1
        self._version(r)
        if k == SWAP:
            return
        if k == END:
            self.states.pop(t, None)
        else:
            status = self.states.get(t, (0, 0))[1]
            self.states[t] = (n, k if k in (COMMIT, ABORT, PREPARE) else status)

    def frontier(self, tx):
        n = self.states.get(tx, (0, 0))[0]
        while n:
            r = self.index[n]
            if r[1] == UPDATE:
                return r
            n = r[4] if r[1] in (CLR, NESTED) else r[3]
        return None

    def durable_lsn(self):
        return self.log[self.durable - 1][0] if self.durable else self.floor

    def text(self):
        lines = [f'{self.base} {len(self.pages)} {len(self.log)} {len(self.decisions)}']
        lines += [' '.join(map(str, [lsn, *v])) for lsn, v in self.pages]
        lines += [' '.join(map(str, r)) for r in self.log]
        lines += [f'{t} {d}' for t, d in sorted(self.decisions.items())]
        return '\n'.join(lines) + '\n'

    def append_line(self, kind, tx):
        tail = self.states[tx][0]
        n = self.log[-1][0] + 1
        if kind == CLR:
            o = self.frontier(tx)
            r = (n, CLR, tx, tail, o[3], *self.where(o), 0, o[7], o[0], 0, 0)
        else:
            r = (n, kind, tx, tail, 0, 0, 0, 0, 0, 0, 0, 0)
        return 'A ' + ' '.join(map(str, r))

    def page_line(self, p, n):
        return 'P ' + ' '.join(map(str, [p, n, *self.versions[p, n]]))

    def apply(self, line):
        items = line.split()
        assert items, 'empty action'
        op = items[0]
        try:
            x = list(map(int, items[1:]))
        except ValueError:
            raise AssertionError('non-integer action field')
        if op == 'A':
            assert len(x) == 12, 'append shape'
            n, k, t, prev, undo, p, s, b, a, target, p2, s2 = x
            assert t in self.states, 'append for an unknown or ended transaction'
            tail, status = self.states[t]
            assert n == self.log[-1][0] + 1, 'LSN allocation'
            assert prev == tail, 'transaction chain'
            if k == CLR:
                assert status == ABORT, 'compensation outside abort state'
                orig = self.frontier(t)
                assert orig is not None, 'compensation with no rollback work left'
                assert (undo, p, s, b, a, target, p2, s2) == (orig[3], *self.where(orig), 0, orig[7], orig[0], 0, 0), \
                    'wrong compensation record'
            else:
                assert (undo, p, s, b, a, target, p2, s2) == (0,) * 8, 'nonzero unused fields'
                if k == ABORT:
                    assert status == 0 or (status == PREPARE and self.decisions.get(t) == 2), 'unauthorized abort'
                elif k == COMMIT:
                    assert status == PREPARE and self.decisions.get(t) == 1, 'unauthorized commit'
                elif k == END:
                    assert status == COMMIT or (status == ABORT and self.frontier(t) is None), 'premature end'
                else:
                    raise AssertionError('record kind not allowed during recovery')
            self._append(tuple(x))
        elif op == 'F':
            assert len(x) == 1, 'flush shape'
            i = self.position.get(x[0])
            assert i is not None and i + 1 >= self.durable, 'invalid synchronization point'
            self.durable = i + 1
        elif op == 'P':
            assert len(x) == 10, 'page shape'
            p, n, *v = x
            assert 0 <= p < len(self.pages), 'page id'
            current = self.pages[p][0]
            assert n >= current, 'page LSN regression'
            assert n == current or n <= self.durable_lsn(), 'page written ahead of the durable log'
            assert self.versions.get((p, n)) == v, 'page is not a logged version'
            self.pages[p] = (n, v)
        else:
            raise AssertionError('unknown action')

    def crash(self):
        return Device(self.base, self.initial, self.pages, self.log[:self.durable], self.decisions,
                      self.expected, self.prepared, self.discarded)

    def done(self):
        try:
            self.complete()
            return True
        except AssertionError:
            return False

    def progress(self):
        return self.durable, [n for n, _ in self.pages]

    def complete(self):
        assert self.durable == len(self.log), 'returned with unsynchronized records'
        assert [v for _, v in self.pages] == self.expected, 'incorrect recovered values'
        assert set(self.states) == self.prepared, 'transaction lifecycle incomplete'
        assert all(st == PREPARE for _, st in self.states.values()), 'in-doubt state lost'
        for p, (n, _) in enumerate(self.pages):
            assert n == self.latest[p][0], 'stable page missing logged changes'


def wander(device, rng, steps):
    """Advance a device with random legal recovery actions, then return it.

    This is not a recovery algorithm: it picks any move the contract allows,
    which produces durable states unlike those of any particular implementation.
    """
    for _ in range(steps):
        moves = []
        for t, (_, st) in device.states.items():
            d = device.decisions.get(t)
            if st == 0 or (st == PREPARE and d == 2):
                moves.append((ABORT, t))
            elif st == PREPARE and d == 1:
                moves.append((COMMIT, t))
            elif st == COMMIT:
                moves.append((END, t))
            elif st == ABORT:
                moves.append((CLR if device.frontier(t) else END, t))
        r = rng.random()
        if moves and r < .6:
            line = device.append_line(*rng.choice(moves))
        elif r < .8 and device.durable < len(device.log):
            line = f'F {device.log[rng.randrange(device.durable, len(device.log))][0]}'
        else:
            p = rng.randrange(len(device.pages))
            h = device.history[p]
            lo = bisect.bisect_left(h, device.pages[p][0])
            hi = bisect.bisect_right(h, device.durable_lsn())
            n = h[hi - 1] if rng.random() < .5 else h[rng.randrange(lo, hi)]
            line = device.page_line(p, n)
        device.apply(line)
    return device


def _value(rng):
    if rng.random() < .04:
        return rng.choice([INT64_MIN, INT64_MAX, 0, -1, 1])
    return rng.randrange(INT64_MIN, INT64_MAX + 1)


class _Tx:
    def __init__(self, tid):
        self.id, self.tail, self.stack, self.owned, self.state, self.nta = tid, 0, [], set(), 0, None


MOVES = ['update', 'nested', 'partial', 'commit', 'abort', 'prepare', 'idle']


def scenario(seed, pages=3, txs=7, steps=60, lsn='low', nested=.12, truncate=.0, equal=.04,
             warmup=0, life=1.0, swaps=.08, cap=4096):
    """Generate a valid history, stable pages and the expected recovered cells.

    life > 1 makes transactions run longer before they commit, abort or prepare.
    """
    rng = random.Random(seed)
    weights = [.45, nested, .15, .08 / life, .07 / life, .06 / life, .05]
    initial = [[_value(rng) for _ in range(8)] for _ in range(pages)]
    values, committed = copy.deepcopy(initial), copy.deepcopy(initial)
    cells = [(p, s) for p in range(pages) for s in range(8)]
    txs = min(txs, 128)
    if rng.random() < .5:
        unused = rng.sample(range(1, 1 << 32), txs)
    else:
        unused = rng.sample(range(1, 4 * txs + 1), txs)
    owner, running, log, decided, decisions = {}, {}, [], {}, {}
    snaps = [[(0, list(v))] for v in initial]
    clock = 0

    def tick():
        nonlocal clock
        clock += rng.randrange(1, 8) if rng.random() < .97 else rng.randrange(8, 5000)
        return clock

    def emit(k, t, undo=0, p=0, s=0, b=0, a=0, target=0):
        r = (tick(), k, t.id, t.tail, undo, p, s, b, a, target, 0, 0)
        log.append(r)
        t.tail = r[0]
        if k in (UPDATE, CLR):
            values[p][s] = a
            snaps[p].append((r[0], list(values[p])))
        return r

    def swap():
        blocked = set()
        for t in running.values():
            if t.nta is not None:
                blocked |= t.nta['cells']
        pool = [c for c in cells if c not in blocked]
        if len(pool) < 2:
            return
        a = rng.choice(pool)
        near = [c for c in pool if c[0] == a[0] and c != a]
        b = rng.choice(near) if near and rng.random() < .4 else rng.choice([c for c in pool if c != a])
        (pa, sa), (pb, sb) = a, b
        log.append((tick(), SWAP, 0, 0, 0, pa, sa, values[pa][sa], values[pb][sb], 0, pb, sb))
        for grid in (values, committed):
            grid[pa][sa], grid[pb][sb] = grid[pb][sb], grid[pa][sa]
        for p in {pa, pb}:
            snaps[p].append((clock, list(values[p])))
        oa, ob = owner.pop(a, None), owner.pop(b, None)
        if oa is not None:
            owner[b] = oa
        if ob is not None:
            owner[a] = ob
        moved = lambda c: b if c == a else a if c == b else c
        for t in running.values():
            t.owned = {moved(c) for c in t.owned}
            for entry in t.stack:
                entry[1] = moved(entry[1])

    def free():
        return [c for c in cells if c not in owner]

    def update(t, cell, inside):
        p, s = cell
        b = values[p][s]
        t.stack.append([emit(UPDATE, t, p=p, s=s, b=b, a=b if rng.random() < equal else _value(rng)), cell])
        owner[cell] = t.id
        if inside:
            t.nta['cells'].add(cell)
            t.nta['count'] += 1
        else:
            t.owned.add(cell)

    def compensate(t):
        old, (p, s) = t.stack.pop()
        emit(CLR, t, old[3], p, s, 0, old[7], old[0])

    def open_nta(t, choices):
        t.nta = {'start': t.tail, 'cells': set(), 'count': 0}
        update(t, rng.choice(choices), True)

    def close_nta(t):
        emit(NESTED, t, undo=t.nta['start'])
        del t.stack[len(t.stack) - t.nta['count']:]
        for p, s in t.nta['cells']:
            committed[p][s] = values[p][s]
            del owner[p, s]
        t.nta = None

    def settle_nta(t):
        if t.nta is not None:
            if rng.random() < .5:
                close_nta(t)
            else:
                t.owned |= t.nta['cells']
                t.nta = None

    def start(t_id):
        t = _Tx(t_id)
        running[t.id] = t
        if nested and rng.random() < nested:
            open_nta(t, free())
        else:
            update(t, rng.choice(free()), False)
        return t

    def commit(t):
        emit(COMMIT, t)
        t.state = COMMIT
        for p, s in t.owned:
            committed[p][s] = values[p][s]

    def abort(t):
        emit(ABORT, t)
        t.state = ABORT

    def finish(t):
        emit(END, t)
        for c in t.owned:
            del owner[c]
        del running[t.id]

    # Short transactions that finish before the main history; they give the
    # log a prefix that may later be truncated.
    for _ in range(warmup):
        if not unused or not free():
            break
        t = start(unused.pop(0))
        for _ in range(rng.randrange(0, 4)):
            if t.nta is not None:
                if rng.random() < .5 or not free():
                    close_nta(t)
                    continue
                update(t, rng.choice(free() + list(t.nta['cells'])), True)
            elif free() or t.owned:
                update(t, rng.choice(free() + list(t.owned)), False)
        settle_nta(t)
        if rng.random() < .6:
            commit(t)
        else:
            abort(t)
            while t.stack:
                compensate(t)
        finish(t)

    for _ in range(steps):
        reserve = sum(len(t.stack) for t in running.values()) + 4 * len(running) + 8
        if len(log) + reserve >= cap:
            break
        cells_free = free()
        if unused and cells_free and (not running or rng.random() < .22):
            start(unused.pop(0))
            continue
        if not running:
            break
        if swaps and rng.random() < swaps:
            swap()
            continue
        t = running[rng.choice(list(running))]
        if t.state == 0:
            if t.nta is not None:
                choices = cells_free + list(t.nta['cells'])
                if rng.random() < .4 or not choices:
                    close_nta(t)
                else:
                    update(t, rng.choice(choices), True)
                continue
            move = rng.choices(MOVES, weights)[0]
            if move == 'update':
                pool = list(t.owned) + cells_free
                if pool:
                    update(t, rng.choice(pool), False)
            elif move == 'nested':
                if cells_free:
                    open_nta(t, cells_free)
            elif move == 'partial':
                if t.stack:
                    for _ in range(rng.randrange(1, min(len(t.stack), 6) + 1)):
                        compensate(t)
            elif move == 'commit':
                commit(t)
            elif move == 'abort':
                abort(t)
            elif move == 'prepare':
                emit(PREPARE, t)
                t.state = PREPARE
        elif t.state == COMMIT:
            if rng.random() < .6:
                finish(t)
        elif t.state == ABORT:
            if t.stack:
                compensate(t)
            else:
                finish(t)
        elif t.state == PREPARE:
            r = rng.random()
            if r < .12:
                commit(t)
                decided[t.id] = 1
            elif r < .24:
                abort(t)
                decided[t.id] = 2

    for t in list(running.values()):
        if t.state == 0:
            r = rng.random()
            if r < .25:
                continue
            settle_nta(t)
            if r < .5:
                commit(t)
            elif r < .75:
                abort(t)
            else:
                emit(PREPARE, t)
                t.state = PREPARE
        if t.state == PREPARE:
            decided[t.id] = rng.choice([0, 1, 2])
        elif t.state == COMMIT:
            if rng.random() < .5:
                finish(t)
        elif t.state == ABORT:
            for _ in range(rng.randrange(len(t.stack) + 1)):
                compensate(t)
            if not t.stack and rng.random() < .5:
                finish(t)

    prepared = set()
    for t_id, d in decided.items():
        t = running.get(t_id)
        if t is not None and t.state == PREPARE:
            if d:
                decisions[t_id] = d
            else:
                prepared.add(t_id)
            if d != 2:
                for p, s in t.owned:
                    committed[p][s] = values[p][s]
        elif d and rng.random() < .5:
            decisions[t_id] = d

    ended = {r[2] for r in log if r[1] == END} | {0}
    limit = 0
    while limit < len(log) and log[limit][2] in ended:
        limit += 1
    drop = rng.randint(1, limit) if limit and rng.random() < truncate else 0
    need = [0] * pages
    for r in log[:drop]:
        if r[1] in CHANGES:
            need[r[5]] = r[0]
        if r[1] == SWAP:
            need[r[10]] = r[0]
    stable = []
    for p in range(pages):
        options = [x for x in snaps[p] if x[0] >= need[p]]
        r = rng.random()
        stable.append(options[0] if r < .2 else options[-1] if r < .45 else rng.choice(options))

    span = clock
    if lsn == 'low':
        base = rng.randrange(1, 100000)
    elif lsn == 'cross':
        base = (1 << 63) - 1 - rng.randrange(0, max(1, span))
    elif lsn == 'high':
        base = (1 << 63) + rng.randrange(0, 1 << 40)
    elif lsn == 'top':
        base = LSN_CEILING - 2 - span - rng.randrange(0, 1000)
    else:
        raise ValueError(lsn)
    assert 0 < base and base + span < LSN_CEILING

    def shift(r):
        n, k, t, prev, undo, p, s, b, a, target, p2, s2 = r
        return (n + base, k, t, prev and prev + base, undo and undo + base, p, s, b, a,
                target and target + base, p2, s2)

    log = [shift(r) for r in log]
    pages_out = [(n + base, list(v)) for n, v in stable]
    return Device(base, initial, pages_out, log[drop:], decisions, committed, prepared, log[:drop])


def _device(initial, base, pages, log, decisions, expected, prepared=(), discarded=()):
    return Device(base, initial, pages, log, decisions, expected, set(prepared), discarded)


def hand_cases():
    """Hand-computed histories. Each expected image was worked out by hand."""
    I = [[10, 20, 30, 40, 50, 60, 70, 80]]
    U, C, K, A, E, R, N, S = UPDATE, CLR, COMMIT, ABORT, END, PREPARE, NESTED, SWAP

    # Winner and loser on the same page; the page already holds the loser's change.
    yield _device(I, 100, [(104, [111, 222, 30, 40, 50, 60, 70, 80])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 111, 0), (104, U, 2, 0, 0, 0, 1, 20, 222, 0),
                   (105, K, 2, 104, 0, 0, 0, 0, 0, 0)],
                  {}, [[10, 222, 30, 40, 50, 60, 70, 80]])
    # Partial rollback, more work, then an abort that is half finished.
    yield _device(I, 100, [(102, [222, 20, 30, 40, 50, 60, 70, 80])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 111, 0), (102, U, 1, 101, 0, 0, 0, 111, 222, 0),
                   (103, C, 1, 102, 101, 0, 0, 0, 111, 102), (104, U, 1, 103, 0, 0, 2, 30, 333, 0),
                   (105, A, 1, 104, 0, 0, 0, 0, 0, 0), (106, C, 1, 105, 103, 0, 2, 0, 30, 104)],
                  {}, I)
    # A prepared transaction with no decision, a commit decision and an abort decision.
    for decision in [None, 1, 2]:
        yield _device(I, 100, [(100, I[0])],
                      [(101, U, 1, 0, 0, 0, 0, 10, 111, 0), (102, R, 1, 101, 0, 0, 0, 0, 0, 0)],
                      {} if decision is None else {1: decision},
                      I if decision == 2 else [[111, 20, 30, 40, 50, 60, 70, 80]],
                      {1} if decision is None else ())
    # A loser must restore the value committed before it, not the checkpoint value.
    yield _device(I, 100, [(104, [INT64_MIN, 20, 30, 40, 50, 60, 70, 80])],
                  [(101, U, 1, 0, 0, 0, 0, 10, INT64_MAX, 0), (102, K, 1, 101, 0, 0, 0, 0, 0, 0),
                   (103, E, 1, 102, 0, 0, 0, 0, 0, 0), (104, U, 2, 0, 0, 0, 0, INT64_MAX, INT64_MIN, 0)],
                  {}, [[INT64_MAX, 20, 30, 40, 50, 60, 70, 80]])
    # LSNs cross the signed 64-bit boundary.
    b = (1 << 63) - 4
    yield _device(I, b, [(b + 1, [111, 20, 30, 40, 50, 60, 70, 80])],
                  [(b + 1, U, 1, 0, 0, 0, 0, 10, 111, 0), (b + 5, U, 2, 0, 0, 0, 1, 20, -44, 0),
                   (b + 6, K, 2, b + 5, 0, 0, 0, 0, 0, 0)],
                  {}, [[10, -44, 30, 40, 50, 60, 70, 80]])
    # An assignment that keeps the value still advances the page LSN.
    yield _device(I, 100, [(100, I[0])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 10, 0), (102, K, 1, 101, 0, 0, 0, 0, 0, 0),
                   (103, E, 1, 102, 0, 0, 0, 0, 0, 0)],
                  {}, I)
    # A closed nested top action inside a loser survives its rollback.
    yield _device(I, 100, [(102, [11, 21, 30, 40, 50, 60, 70, 80])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 11, 0), (102, U, 1, 101, 0, 0, 1, 20, 21, 0),
                   (103, N, 1, 102, 101, 0, 0, 0, 0, 0), (104, U, 1, 103, 0, 0, 0, 11, 12, 0)],
                  {}, [[10, 21, 30, 40, 50, 60, 70, 80]])
    # A cell released by a nested top action is changed and committed by another
    # transaction before the first one aborts.
    yield _device(I, 100, [(105, [11, 22, 30, 40, 50, 60, 70, 80])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 11, 0), (102, U, 1, 101, 0, 0, 1, 20, 21, 0),
                   (103, N, 1, 102, 101, 0, 0, 0, 0, 0), (105, U, 2, 0, 0, 0, 1, 21, 22, 0),
                   (106, K, 2, 105, 0, 0, 0, 0, 0, 0), (107, E, 2, 106, 0, 0, 0, 0, 0, 0),
                   (108, A, 1, 103, 0, 0, 0, 0, 0, 0)],
                  {}, [[10, 22, 30, 40, 50, 60, 70, 80]])
    # Nested top action at the start of a transaction, then a partial rollback
    # whose CLR points at the NESTED record, then PREPARE with an abort decision.
    yield _device(I, 100, [(104, [10, 20, 31, 40, 50, 60, 70, 80])],
                  [(101, U, 1, 0, 0, 0, 2, 30, 31, 0), (102, N, 1, 101, 0, 0, 0, 0, 0, 0),
                   (103, U, 1, 102, 0, 0, 3, 40, 41, 0), (104, C, 1, 103, 102, 0, 3, 0, 40, 103),
                   (105, U, 1, 104, 0, 0, 4, 50, 55, 0), (106, R, 1, 105, 0, 0, 0, 0, 0, 0)],
                  {1: 2}, [[10, 20, 31, 40, 50, 60, 70, 80]])
    # Truncated log: the page LSN and an END's prev name records that are gone.
    yield _device(I, 100, [(101, [11, 20, 30, 40, 50, 60, 70, 80])],
                  [(105, U, 2, 0, 0, 0, 1, 20, 22, 0), (107, E, 1, 102, 0, 0, 0, 0, 0, 0)],
                  {}, [[11, 20, 30, 40, 50, 60, 70, 80]],
                  discarded=[(101, U, 1, 0, 0, 0, 0, 10, 11, 0), (102, K, 1, 101, 0, 0, 0, 0, 0, 0)])
    # Everything truncated: nothing to do.
    yield _device(I, 100, [(101, [10, 20, 30, 40, 50, 66, 70, 80])], [], {},
                  [[10, 20, 30, 40, 50, 66, 70, 80]],
                  discarded=[(101, U, 1, 0, 0, 0, 5, 60, 66, 0), (102, K, 1, 101, 0, 0, 0, 0, 0, 0),
                             (103, E, 1, 102, 0, 0, 0, 0, 0, 0)])
    # Large transaction IDs, extreme values, LSNs near the top of the range and a
    # CLR that is missing from its page.
    b = (1 << 64) - 1000100
    big, mid = (1 << 32) - 1, 1 << 31
    J = [[10, 20, 30, 40, 50, 60, 70, 80], [-1, 0, 1, INT64_MIN, INT64_MAX, 5, 6, 7]]
    yield _device(J, b, [(b, J[0]), (b + 4, [-1, 0, 1, INT64_MAX, INT64_MIN, 5, 6, 7])],
                  [(b + 1, U, big, 0, 0, 1, 3, INT64_MIN, INT64_MAX, 0),
                   (b + 2, U, mid, 0, 0, 0, 0, 10, INT64_MIN, 0),
                   (b + 3, U, 1, 0, 0, 1, 0, -1, -1, 0),
                   (b + 4, U, mid, b + 2, 0, 1, 4, INT64_MAX, INT64_MIN, 0),
                   (b + 5, R, big, b + 1, 0, 0, 0, 0, 0, 0),
                   (b + 6, K, 1, b + 3, 0, 0, 0, 0, 0, 0),
                   (b + 7, A, mid, b + 4, 0, 0, 0, 0, 0, 0),
                   (b + 8, C, mid, b + 7, b + 2, 1, 4, 0, INT64_MAX, b + 4),
                   (b + 9, E, 1, b + 6, 0, 0, 0, 0, 0, 0)],
                  {big: 1}, [[10, 20, 30, 40, 50, 60, 70, 80], [-1, 0, 1, INT64_MAX, INT64_MAX, 5, 6, 7]])
    # A commit decision that an earlier attempt already acted on.
    yield _device(I, 100, [(100, I[0])],
                  [(101, U, 7, 0, 0, 0, 0, 10, 77, 0), (102, R, 7, 101, 0, 0, 0, 0, 0, 0),
                   (103, K, 7, 102, 0, 0, 0, 0, 0, 0)],
                  {7: 1}, [[77, 20, 30, 40, 50, 60, 70, 80]])
    # An abort decision that an earlier attempt partly carried out.
    yield _device(I, 100, [(105, [10, 20, 30, 40, 50, 60, 1, 80])],
                  [(101, U, 9, 0, 0, 0, 6, 70, 1, 0), (102, U, 9, 101, 0, 0, 7, 80, 2, 0),
                   (103, R, 9, 102, 0, 0, 0, 0, 0, 0), (104, A, 9, 103, 0, 0, 0, 0, 0, 0),
                   (105, C, 9, 104, 101, 0, 7, 0, 80, 102)],
                  {9: 2}, I)
    # One cell passes through an aborted, a committed and a losing transaction.
    yield _device(I, 100, [(103, [10, 20, 30, 40, 50, 60, 70, 80])],
                  [(101, U, 3, 0, 0, 0, 0, 10, 13, 0), (102, A, 3, 101, 0, 0, 0, 0, 0, 0),
                   (103, C, 3, 102, 0, 0, 0, 0, 10, 101), (104, E, 3, 103, 0, 0, 0, 0, 0, 0),
                   (105, U, 4, 0, 0, 0, 0, 10, 14, 0), (106, K, 4, 105, 0, 0, 0, 0, 0, 0),
                   (107, E, 4, 106, 0, 0, 0, 0, 0, 0), (108, U, 5, 0, 0, 0, 0, 14, 15, 0)],
                  {}, [[14, 20, 30, 40, 50, 60, 70, 80]])
    # A loser's item is moved by a SWAP before recovery; its rollback follows it.
    yield _device(I, 100, [(100, I[0])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 11, 0), (102, S, 0, 0, 0, 0, 0, 11, 20, 0, 0, 1)],
                  {}, [[20, 10, 30, 40, 50, 60, 70, 80]])
    # A SWAP across two pages, only one of which holds it; a committed item and
    # a losing item trade places, and the loser has an earlier partial rollback.
    J = [[10, 20, 30, 40, 50, 60, 70, 80], [1, 2, 3, 4, 5, 6, 7, 8]]
    yield _device(J, 100, [(103, [2, 20, 30, 40, 50, 60, 70, 80]), (102, [2, 2, 3, 4, 5, 6, 7, 8])],
                  [(101, U, 1, 0, 0, 0, 0, 10, 11, 0), (102, U, 2, 0, 0, 1, 0, 1, 2, 0),
                   (103, S, 0, 0, 0, 0, 0, 11, 2, 0, 1, 0), (104, K, 2, 102, 0, 0, 0, 0, 0, 0),
                   (105, E, 2, 104, 0, 0, 0, 0, 0, 0), (106, U, 1, 101, 0, 1, 1, 2, 22, 0),
                   (107, C, 1, 106, 101, 1, 1, 0, 2, 106)],
                  {}, [[2, 20, 30, 40, 50, 60, 70, 80], [10, 2, 3, 4, 5, 6, 7, 8]])
    # Items of a prepared transaction and a loser move through several SWAPs.
    yield _device(I, 100, [(104, [10, 20, 46, 35, 50, 60, 70, 80])],
                  [(101, U, 5, 0, 0, 0, 2, 30, 35, 0), (102, U, 6, 0, 0, 0, 3, 40, 46, 0),
                   (103, R, 5, 101, 0, 0, 0, 0, 0, 0), (104, S, 0, 0, 0, 0, 2, 35, 46, 0, 0, 3),
                   (105, S, 0, 0, 0, 0, 3, 35, 50, 0, 0, 4), (106, S, 0, 0, 0, 0, 2, 46, 60, 0, 0, 5)],
                  {}, [[10, 20, 60, 50, 35, 40, 70, 80]], {5})
