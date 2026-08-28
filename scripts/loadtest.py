#!/usr/bin/env python
"""Нагрузочная проверка вебхука — критерий приёмки №12.

Держит заданную частоту запросов в течение заданного времени и проверяет
четыре вещи:

* все ответы 200 OK;
* ни одного HTTP-таймаута;
* очередь не растёт бесконечно;
* backpressure отрабатывает, если ёмкость всё-таки исчерпана.

Запросы отправляются по расписанию, а не «следующий после предыдущего»:
иначе замер выродился бы в измерение задержки, а нагрузку мы бы не создали.
Отставание от расписания само по себе показательно — оно означает, что
сервис не успевает принимать.

Пример:

    python scripts/loadtest.py \\
        --url https://bot.up.railway.app \\
        --secret "$TELEGRAM_WEBHOOK_SECRET" \\
        --rps 30 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

#: Имя HTTP-заголовка, а не значение секрета.
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105


@dataclass
class Results:
    """Собранные за прогон измерения."""

    statuses: dict[int, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    timeouts: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    queue_depths: list[int] = field(default_factory=list)
    lag: list[float] = field(default_factory=list)

    def record_status(self, status: int, latency: float) -> None:
        self.statuses[status] = self.statuses.get(status, 0) + 1
        self.latencies.append(latency)

    def record_error(self, error: BaseException) -> None:
        name = type(error).__name__
        self.errors[name] = self.errors.get(name, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.statuses.values()) + self.timeouts + sum(self.errors.values())


def percentile(values: list[float], fraction: float) -> float:
    """Перцентиль без numpy: значений тут тысячи, а не миллионы."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


async def _fire(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    update_id: int,
    results: Results,
) -> None:
    """Один запрос вебхука.

    update_id у каждого свой: одинаковые отсеялись бы дедупликацией, и
    вместо нагрузки на очередь мы бы мерили скорость словаря.
    """
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": int(time.time()),
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 1, "is_bot": False, "first_name": "loadtest"},
            "text": "нагрузочная проверка",
        },
    }
    started = time.perf_counter()
    try:
        response = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException:
        results.timeouts += 1
    except Exception as error:
        results.record_error(error)
    else:
        results.record_status(response.status_code, time.perf_counter() - started)


async def _watch_queue(
    client: httpx.AsyncClient, base_url: str, results: Results, stop: asyncio.Event
) -> None:
    """Раз в секунду снимает глубину очереди с /health."""
    while not stop.is_set():
        try:
            response = await client.get(f"{base_url}/health")
            payload = response.json()
            pending = sum(
                queue.get("pending", 0) for queue in payload.get("queues", [])
            )
            results.queue_depths.append(pending)
        except Exception as error:
            # Сбой опроса /health не должен ронять прогон: мы меряем вебхук,
            # а глубина очереди — сведения вспомогательные.
            print(f"не удалось прочитать /health: {error!r}")
        with_timeout = asyncio.wait_for(stop.wait(), timeout=1.0)
        try:
            await with_timeout
        except TimeoutError:
            continue


async def run_load(
    base_url: str,
    secret: str,
    rps: int,
    duration: int,
    timeout: float,  # noqa: ASYNC109 — таймаут HTTP-клиента, а не ожидания корутины
) -> Results:
    """Держит заданную частоту в течение заданного времени."""
    results = Results()
    webhook_url = f"{base_url.rstrip('/')}/webhook/telegram"
    headers = {SECRET_HEADER: secret}
    stop = asyncio.Event()

    limits = httpx.Limits(max_connections=rps * 4, max_keepalive_connections=rps)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        watcher = asyncio.create_task(_watch_queue(client, base_url, results, stop))

        interval = 1.0 / rps
        started_at = time.perf_counter()
        tasks: list[asyncio.Task[None]] = []

        for index in range(rps * duration):
            due_at = started_at + index * interval
            delay = due_at - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            else:
                # Не успеваем по расписанию — это уже симптом.
                results.lag.append(-delay)
            tasks.append(
                asyncio.create_task(
                    _fire(client, webhook_url, headers, 10_000_000 + index, results)
                )
            )

        await asyncio.gather(*tasks)
        stop.set()
        await watcher

    return results


def report(results: Results, rps: int, duration: int) -> bool:
    """Печатает отчёт и говорит, пройдена ли проверка."""
    expected = rps * duration
    print(f"\nЗапросов отправлено: {results.total} из {expected}")
    print(f"Коды ответов: {dict(sorted(results.statuses.items()))}")
    print(f"Таймауты: {results.timeouts}")
    if results.errors:
        print(f"Сетевые ошибки: {results.errors}")

    if results.latencies:
        print(
            "Задержка, мс: "
            f"медиана {statistics.median(results.latencies) * 1000:.1f}, "
            f"p95 {percentile(results.latencies, 0.95) * 1000:.1f}, "
            f"максимум {max(results.latencies) * 1000:.1f}"
        )
    if results.queue_depths:
        print(
            f"Глубина очереди: максимум {max(results.queue_depths)}, "
            f"в конце {results.queue_depths[-1]}"
        )
    if results.lag:
        print(
            f"Отставание от расписания: {len(results.lag)} раз, "
            f"максимум {max(results.lag) * 1000:.0f} мс"
        )

    ok = results.statuses.get(200, 0)
    overloaded = results.statuses.get(503, 0)
    other = {code: n for code, n in results.statuses.items() if code not in (200, 503)}

    print()
    passed = True
    if results.timeouts:
        print(f"ПРОВАЛ: {results.timeouts} HTTP-таймаутов, допускается 0")
        passed = False
    if other:
        print(f"ПРОВАЛ: неожиданные коды ответов {other}")
        passed = False
    if results.errors:
        print(f"ПРОВАЛ: сетевые ошибки {results.errors}")
        passed = False
    if ok + overloaded < expected:
        print(f"ПРОВАЛ: получено ответов {ok + overloaded}, ожидалось {expected}")
        passed = False
    if overloaded:
        # Не провал: это и есть backpressure. Но знать об этом надо —
        # значит, ёмкости или воркеров не хватает под целевую нагрузку.
        print(
            f"ВНИМАНИЕ: {overloaded} раз сработал backpressure "
            "(честный отказ, мессенджер повторит доставку)"
        )
    if results.queue_depths and results.queue_depths[-1] > max(rps, 10):
        print(f"ПРОВАЛ: очередь не рассосалась, в конце {results.queue_depths[-1]}")
        passed = False

    print("ПРОВЕРКА ПРОЙДЕНА" if passed else "ПРОВЕРКА НЕ ПРОЙДЕНА")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="базовый адрес сервиса")
    parser.add_argument("--secret", required=True, help="TELEGRAM_WEBHOOK_SECRET")
    parser.add_argument("--rps", type=int, default=30, help="запросов в секунду")
    parser.add_argument("--duration", type=int, default=60, help="секунд")
    parser.add_argument(
        "--timeout", type=float, default=10.0, help="таймаут запроса, секунд"
    )
    args = parser.parse_args()

    print(
        f"Нагрузка: {args.rps} rps в течение {args.duration} с "
        f"на {args.url} (всего {args.rps * args.duration} запросов)"
    )
    results = asyncio.run(
        run_load(args.url, args.secret, args.rps, args.duration, args.timeout)
    )
    return 0 if report(results, args.rps, args.duration) else 1


if __name__ == "__main__":
    sys.exit(main())
