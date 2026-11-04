#Remote To Local Copy(RTLC) — очередь отложенной DICOM-отправки
#
# Файлы НЕ копируются в буфер: при недоступности DICOM-узла исследования
# продолжают ждать в папке-источнике, а в журнале (rtlc.state.json)
# ведётся запись о недоставленных файлах с планом повторов.
#
# - backoff per-file: retry_initial -> x2 -> retry_max (+-20% jitter)
# - circuit breaker: breaker_threshold транзиентных фейлов подряд ->
#   пауза breaker_cooldown с, затем проба C-ECHO каждые 30 с
import random
import threading
import time

import dicom_sender

RETRY_JITTER = 0.2
PROBE_INTERVAL = 30


class DicomQueue:
    """Фоновый поток, досылающий недоставленные файлы из источника."""

    def __init__(self, utility):
        self.u = utility
        self.log = utility.log
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._fail_streak = 0
        self._open_until = 0.0
        self._next_probe = 0.0

    # --- lifecycle ---------------------------------------------------------

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop,
                                        daemon=True,
                                        name='rtlc-dicom-queue')
        self._thread.start()
        self.log.info('DicomQueue started.')

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=15)
            self._thread = None
        self.log.info('DicomQueue stopped.')

    def wake(self):
        self._wake.set()

    # --- публичное API (webui) ---------------------------------------------

    def retry_now(self):
        """Сбросить backoff/timers и открыть шторку breaker'а для немедленной
        досылки. Счётчик фейлов НЕ обнуляем: если узел всё ещё лежит,
        breaker закроется снова — принудительный ретрай не должен
        превращаться в постоянный шторм."""
        with self.u.state_lock:
            for entry in self.u.state['files'].values():
                d = entry.get('dicom')
                if not d or d.get('sent') or d.get('permanent') or d.get(
                        'quarantine'):
                    continue
                d['next_attempt'] = 0.0
            self.u.saveState()
        self._open_until = 0.0
        self.log.info('DicomQueue: retry_now — timers reset.')
        self.wake()

    def breaker_state(self):
        return ('open' if self._open_until > time.time() else 'closed')

    def summary(self):
        now = time.time()
        counts = {
            'pending': 0,  # next_attempt наступил, ждут отправки
            'waiting': 0,  # в backoff
            'permanent': 0,
            'quarantine': 0,
            'sent': 0
        }
        oldest = None
        with self.u.state_lock:
            for entry in self.u.state['files'].values():
                d = entry.get('dicom')
                if not d:
                    continue
                if d.get('sent'):
                    counts['sent'] += 1
                    continue
                if d.get('permanent'):
                    counts['permanent'] += 1
                    continue
                if d.get('quarantine'):
                    counts['quarantine'] += 1
                    continue
                if d.get('next_attempt', 0) <= now:
                    counts['pending'] += 1
                else:
                    counts['waiting'] += 1
                q = d.get('queued_at') or 0
                if oldest is None or q < oldest:
                    oldest = q
        return {
            'breaker': self.breaker_state(),
            'breaker_until': self._open_until,
            'oldest_queued_at': oldest,
            **counts
        }

    def items(self, limit=500):
        now = time.time()
        out = []
        with self.u.state_lock:
            for path, entry in self.u.state['files'].items():
                d = entry.get('dicom')
                if not d or d.get('sent'):
                    continue
                state = ('permanent' if d.get('permanent') else
                         'quarantine' if d.get('quarantine') else
                         'pending' if d.get('next_attempt', 0) <= now else
                         'waiting')
                out.append({
                    'path': path,
                    'size': entry.get('size'),
                    'state': state,
                    'attempts': d.get('attempts', 0),
                    'queued_at': d.get('queued_at'),
                    'next_attempt': d.get('next_attempt'),
                    'last_error': d.get('last_error')
                })
        out.sort(key=lambda x: x['queued_at'] or 0)
        return out[:limit]

    # --- цикл --------------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._drain()
            except Exception as exc:
                self.log.exception(f'DicomQueue cycle error: {exc}')
            self._wake.wait(self.u.dicomQueueTick)
            self._wake.clear()

    def _drain(self):
        now = time.time()
        if self._open_until > now:
            if now >= self._next_probe:
                self._next_probe = now + PROBE_INTERVAL
                if self._probe():
                    self._open_until = 0.0
                    self.log.info(
                        'DICOM node is reachable again (C-ECHO ok), '
                        'draining queue.')
            return
        for path in self._candidates(now):
            if self._stop.is_set():
                break
            self._send(path)

    def _probe(self):
        try:
            ok, msg = dicom_sender.echo(self.u.dicomHost, self.u.dicomPort,
                                        self.u.dicomCalledAet,
                                        self.u.dicomCallingAet,
                                        self.u.dicomTimeout)
            if not ok:
                self.log.debug(f'C-ECHO while breaker open: {msg}')
            return ok
        except Exception as exc:
            self.log.debug(f'C-ECHO probe error: {exc}')
            return False

    def _candidates(self, now):
        out = []
        with self.u.state_lock:
            for path, entry in self.u.state['files'].items():
                d = entry.get('dicom')
                if not d or not d.get('queued'):
                    continue
                if d.get('sent') or d.get('permanent') or d.get(
                        'quarantine'):
                    continue
                if d.get('next_attempt', 0) > now:
                    continue
                out.append((d.get('queued_at', 0), path))
        out.sort()
        return [p for _, p in out]

    def _send(self, path):
        u = self.u
        if not u.sourceExists(path):
            with u.state_lock:
                entry = u.state['files'].get(path)
                if entry:
                    d = entry.setdefault('dicom', {})
                    if not d.get('sent') and not d.get('permanent'):
                        d['quarantine'] = True
                        d['last_error'] = 'source file no longer exists'
                        d.pop('queued', None)
                        d.pop('next_attempt', None)
                        u.saveState()
            self.log.error(
                f'Quarantine: "{path}" is queued for DICOM delivery but '
                'the source file is gone; the file cannot be delivered.')
            return

        ok, err, permanent = u.sendViaDicom(path)
        tripped = False
        with u.state_lock:
            entry = u.state['files'].get(path)
            if not entry:
                return
            d = entry.setdefault('dicom', {})
            if ok:
                d['sent'] = True
                d['sent_at'] = time.time()
                for k in ('last_error', 'permanent', 'next_attempt',
                          'queued'):
                    d.pop(k, None)
                self._fail_streak = 0
                u.stats['dicom_sent'] += 1
                self.log.info(
                    f'File "{path}" sent to DICOM node '
                    f'{u.dicomCalledAet}@{u.dicomHost}:{u.dicomPort}'
                    f'{f" ({err})" if err else ""}.')
            else:
                d['attempts'] = d.get('attempts', 0) + 1
                d['last_error'] = err
                if permanent:
                    d['permanent'] = True
                    d.pop('next_attempt', None)
                    d.pop('queued', None)
                    self.log.error(
                        f"File '{path}' not sent to DICOM node "
                        f'{u.dicomCalledAet}@{u.dicomHost}:{u.dicomPort}: '
                        f'{err} (permanent, will not retry)')
                else:
                    d['next_attempt'] = time.time() + self._backoff(
                        d['attempts'])
                    self._fail_streak += 1
                    self.log.error(
                        f"File '{path}' not sent to DICOM node "
                        f'{u.dicomCalledAet}@{u.dicomHost}:{u.dicomPort}: '
                        f'{err} (attempt {d["attempts"]}, retry in '
                        f'{d["next_attempt"] - time.time():.0f}s)')
                    if self._fail_streak >= u.dicomBreakerThreshold:
                        self._open_until = time.time() + \
                            u.dicomBreakerCooldown
                        self._next_probe = self._open_until
                        self._fail_streak = 0
                        tripped = True
                u.stats['dicom_failed'] += 1
            u.saveState()
        if tripped:
            self.log.error(
                f'Circuit breaker OPEN: DICOM node '
                f'{u.dicomCalledAet}@{u.dicomHost}:{u.dicomPort} is '
                f'unreachable, attempts paused for {u.dicomBreakerCooldown}s.')

    def _backoff(self, attempts):
        delay = min(self.u.dicomRetryInitial * (2**(attempts - 1)),
                    self.u.dicomRetryMax)
        return delay * (1 - RETRY_JITTER + random.random() * 2 * RETRY_JITTER)
