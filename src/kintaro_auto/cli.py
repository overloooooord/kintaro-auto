from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from kintaro_auto.appeals import remove_account_restrictions
from kintaro_auto.client import AuthChallengeRequiredError, KintaroClient
from kintaro_auto.config import AppConfig, load_config
from kintaro_auto.models import AccountRecord, UploadTask, apply_device_geo_profile
from kintaro_auto.pipeline import AutomationPipeline
from kintaro_auto.response_adapters import extract_aweme_id, extract_feed_items
from kintaro_auto.storage import (
    StateStore,
    load_queue,
    parse_shop_account_line,
    save_queue,
)
from kintaro_auto.uniquifier import uniquify_photos, uniquify_video

app = typer.Typer(
    name="kintaro-auto",
    help="Полная автоматизация залива и анти-теневого прогрева аккаунтов через Kintaro API v1.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.INFO if verbose else logging.WARNING)


def _init_env(config_path: Path | None) -> tuple[AppConfig, StateStore]:
    cfg = load_config(config_path)
    store = StateStore(cfg.state_dir)
    return cfg, store


@app.command("import-accounts")
def import_accounts(
    accounts_file: Annotated[Path, typer.Argument(help="JSON-файл со списком аккаунтов")],
    config: Annotated[Path | None, typer.Option("--config", "-c", help="Путь к config.toml")] = None,
) -> None:
    """Импортировать или обновить список аккаунтов и их прокси в локальном хранилище."""
    _, store = _init_env(config)
    imported = store.import_accounts_file(accounts_file)
    console.print(f"[green]Импортировано аккаунтов:[/green] {len(imported)}")
    for acc in imported:
        console.print(
            f"  • [bold]{acc.account_id}[/bold] | region={acc.region} | method={acc.auth_method} | device={'есть' if acc.device else 'будет создан'}"
        )


@app.command("add-line")
def add_line(
    line: Annotated[str, typer.Argument(help="Строка username|password|email|totp ИЛИ login:pass:outlook:mail_pass:rec_mail:rec_pass:2fa")],
    proxy: Annotated[str, typer.Option("--proxy", "-p", help="Прокси protocol://user:pass@host:port")],
    region: Annotated[str, typer.Option("--region", "-r", help="Двухбуквенный код страны залива (например US, DE, GB)")] = "US",
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Быстро добавить аккаунт из шоп-строки (`|` TOTP или `:` Outlook-Hub)."""
    _, store = _init_env(config)
    acc = parse_shop_account_line(line, proxy=proxy, region=region)
    existing_path = store.account_path(acc.account_id)
    if existing_path.exists():
        old = store.load_account(acc.account_id)
        if old.device is not None:
            acc.device = old.device
            acc.authenticated = old.authenticated
            acc.first_authenticated_at = old.first_authenticated_at
            acc.upload_history = old.upload_history
    store.save_account(acc)
    console.print(
        f"[green]✓ Аккаунт \\[{acc.account_id}] сохранён[/green] (region={acc.region}, "
        f"auth={acc.auth_method}, outlook_hub={'да' if acc.use_outlook_hub else 'нет'}, totp={'да' if acc.totp_token else 'нет'})"
    )


@app.command("import-lines")
def import_lines(
    lines_file: Annotated[Path, typer.Argument(help="TXT-файл со строками аккаунтов (по одной на строку)")],
    proxy: Annotated[str, typer.Option("--proxy", "-p", help="Прокси protocol://user:pass@host:port")],
    region: Annotated[str, typer.Option("--region", "-r", help="Целевой регион залива (например US)")] = "US",
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Массовый импорт `.txt` пачки аккаунтов (`login|pass|email|totp` и `login:pass:outlook:...`)."""
    _, store = _init_env(config)
    imported = 0
    for raw_line in lines_file.read_text(encoding="utf-8").splitlines():
        s = raw_line.strip()
        if not s or s.startswith("#"):
            continue
        acc = parse_shop_account_line(s, proxy=proxy, region=region)
        existing_path = store.account_path(acc.account_id)
        if existing_path.exists():
            old = store.load_account(acc.account_id)
            if old.device is not None:
                acc.device = old.device
                acc.authenticated = old.authenticated
                acc.first_authenticated_at = old.first_authenticated_at
                acc.upload_history = old.upload_history
        store.save_account(acc)
        imported += 1
        console.print(
            f"  • [green]✓ \\[{acc.account_id}][/green] auth={acc.auth_method} | outlook_hub={'да' if acc.use_outlook_hub else 'нет'}"
        )
    console.print(f"[bold green]Импортировано строк: {imported}[/bold green]")


@app.command("import-sessions")
def import_sessions(
    sessions_dir: Annotated[Path, typer.Argument(help="Папка sessions/ из kintaro_auth")],
    target_region: Annotated[str, typer.Option("--region", "-r", help="Целевой регион залива (например US)")] = "US",
    proxy_override: Annotated[str | None, typer.Option("--proxy", "-p", help="Прокси (если нужно заменить прокси из файла сессии)")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Импортировать готовые авторизованные сессии `sessions/*.json` из `kintaro_auth`."""
    import json

    _, store = _init_env(config)
    count = 0
    for fp in sorted(sessions_dir.glob("*.json")):
        raw = json.loads(fp.read_text(encoding="utf-8"))
        dev = raw.get("device") or (json.loads(raw["x_device"]) if raw.get("x_device") else None)
        if not dev:
            continue
        uname = raw.get("username") or fp.stem
        reg = target_region.strip().upper()
        dev = apply_device_geo_profile(dev, reg)
        acc = AccountRecord(
            account_id=uname,
            proxy=proxy_override or raw.get("proxy", ""),
            region=reg,
            auth_region="DE",
            auth_method="device_only",
            username=uname,
            password=raw.get("tiktok_password") or "",
            email=raw.get("email") or "",
            totp_token=raw.get("totp_token") or "",
            device=dev,
            authenticated=True,
            first_authenticated_at=raw.get("logged_in_at"),
        )
        existing_path = store.account_path(uname)
        if existing_path.exists():
            old = store.load_account(uname)
            acc.upload_history = old.upload_history
            acc.last_warmup_at = old.last_warmup_at
        store.save_account(acc)
        count += 1
        console.print(f"  • [green]✓ {uname}[/green] (uid={raw.get('uid')}, target_region={reg})")
    console.print(f"[bold green]Импортировано готовых сессий: {count}[/bold green]")


@app.command("status")
def status(
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Показать состояние всех аккаунтов, кулдаунов и истории заливов."""
    cfg, store = _init_env(config)
    accounts = store.list_accounts()
    if not accounts:
        console.print("[yellow]Хранилище пусто. Сначала импортируй аккаунты через `import-accounts`.[/yellow]")
        return

    # Dummy pipeline without active HTTP client purely for local eligibility calculation
    table = Table(title="Состояние аккаунтов Kintaro")
    table.add_column("Account ID", style="bold cyan")
    table.add_column("Region")
    table.add_column("Auth")
    table.add_column("Device")
    table.add_column("Последний прогрев")
    table.add_column("Постов")
    table.add_column("Тени?")
    table.add_column("Готовность к заливу")

    dummy_pipeline = AutomationPipeline.__new__(AutomationPipeline)
    dummy_pipeline.config = cfg

    for acc in accounts:
        ok, reason = dummy_pipeline.check_posting_eligibility(acc)
        shadows = sum(1 for p in acc.upload_history if p.suspected_shadowban)
        auth_label = (
            "[green]Да[/green]"
            if acc.authenticated
            else ("[yellow]Ждёт код (403)[/yellow]" if acc.pending_verify else "[red]Нет[/red]")
        )
        table.add_row(
            acc.account_id,
            acc.region,
            auth_label,
            "Да" if acc.device else "Нет",
            (acc.last_warmup_at or "—")[:19],
            str(len(acc.upload_history)),
            f"[red]{shadows}[/red]" if shadows else "[green]0[/green]",
            "[green]Готов[/green]" if (acc.authenticated and ok) else reason,
        )

    console.print(table)


@app.command("auth")
def auth_accounts(
    account_id: Annotated[str | None, typer.Option("--account", "-a", help="Конкретный account_id (по умолчанию все)")] = None,
    interactive: Annotated[bool, typer.Option("--interactive/--no-interactive", help="Запрашивать код 2FA/почты/SMS в терминале при 403")] = True,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Зарегистрировать `x-device` (если нет), обновить хосты и войти в аккаунты."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)
    targets = [store.load_account(account_id)] if account_id else store.list_accounts()
    if not targets:
        raise typer.BadParameter("Нет аккаунтов для авторизации.")

    def ask_code(acc) -> str:
        return typer.prompt(f"[{acc.account_id}] Введи код подтверждения (403 challenge)")

    async def _run() -> None:
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            for acc in targets:
                try:
                    await pipeline.ensure_authenticated(
                        acc, code_provider=ask_code if interactive else None
                    )
                    console.print(f"[green]✓ [{acc.account_id}] Авторизован и сессия активна.[/green]")
                except AuthChallengeRequiredError:
                    console.print(
                        f"[yellow]! [{acc.account_id}] Требуется код подтверждения (403). "
                        f"Выполни: `kintaro-auto confirm-code {acc.account_id} <КОД>`[/yellow]"
                    )
                except Exception as exc:
                    console.print(f"[red]✗ [{acc.account_id}] Ошибка входа: {exc}[/red]")

    asyncio.run(_run())


@app.command("confirm-code")
def confirm_code(
    account_id: Annotated[str, typer.Argument(help="ID аккаунта")],
    code: Annotated[str, typer.Argument(help="Код из письма / SMS")],
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Подтвердить вход кодом после ответа HTTP 403 (`send_code`)."""
    _setup_logging()
    cfg, store = _init_env(config)
    acc = store.load_account(account_id)

    async def _run() -> None:
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            await pipeline.confirm_signin_code(acc, code)
            console.print(f"[green]✓ [{acc.account_id}] Код принят, аккаунт авторизован.[/green]")

    asyncio.run(_run())


@app.command("warmup")
def warmup(
    account_id: Annotated[str | None, typer.Option("--account", "-a", help="Конкретный account_id (по умолчанию все)")] = None,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Прогнать человекоподобный прогрев ленты рекомендаций на аккаунтах."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)
    targets = [store.load_account(account_id)] if account_id else store.list_accounts()
    if not targets:
        raise typer.BadParameter("Нет аккаунтов для прогрева.")

    async def _run() -> None:
        sem = asyncio.Semaphore(max(1, cfg.max_parallel_accounts))
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)

            async def _warm_one(acc) -> None:
                async with sem:
                    try:
                        stats = await pipeline.run_warmup(acc)
                        console.print(
                            f"[green]✓ [{acc.account_id}] Прогрев завершён:[/green] "
                            f"лент={stats['rounds']}, просмотров={stats['views']}, "
                            f"комментов={stats['comments_read']}, лайков={stats['likes']}, подписок={stats['follows']}"
                        )
                    except Exception as exc:
                        console.print(f"[red]✗ [{acc.account_id}] Ошибка прогрева: {exc}[/red]")

            await asyncio.gather(*(_warm_one(a) for a in targets))

    asyncio.run(_run())


@app.command("upload-video")
def upload_video(
    video: Annotated[Path, typer.Argument(help="Путь к MP4 файлу (<50 МБ)")],
    description: Annotated[str, typer.Option("--desc", "-d", help="Описание (поддерживает {спинтакс|варианты} и @mentions)")] = "",
    account_id: Annotated[str | None, typer.Option("--account", "-a", help="Конкретный аккаунт (иначе автовыбор самого отдохнувшего)")] = None,
    mention: Annotated[list[str] | None, typer.Option("--mention", "-m", help="Упоминания без @ (можно несколько)")] = None,
    music_id: Annotated[str, typer.Option("--music-id", help="Точный music_id")] = "",
    music_search: Annotated[str | None, typer.Option("--music-search", help="Найти музыку по ключу")] = None,
    force_cooldown: Annotated[bool, typer.Option("--force-cooldown", help="Игнорировать таймеры отлёжки/кулдауна")] = False,
    skip_warmup: Annotated[bool, typer.Option("--skip-warmup", help="Пропустить прогрев перед заливом")] = False,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Уникализировать видео, прогреть сессию и залить пост без теней."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)
    task = UploadTask(
        task_id=f"cli_{uuid.uuid4().hex[:8]}",
        account_id=account_id,
        media_type="video",
        video_path=str(video),
        description=description,
        mentions=mention or [],
        music_id=music_id,
        music_search_keyword=music_search,
    )

    async def _run() -> None:
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            acc, rec = await pipeline.execute_upload_task(
                task, force_cooldown=force_cooldown, skip_warmup=skip_warmup
            )
            console.print(
                f"[green]✓ Видео залито на [{acc.account_id}]![/green] aweme_id={rec.aweme_id or 'не распознан'}"
            )
            if rec.diagnostic_note:
                console.print(f"[yellow]Диагностика: {rec.diagnostic_note}[/yellow]")

    asyncio.run(_run())


@app.command("upload-photo")
def upload_photo(
    photos: Annotated[list[Path], typer.Argument(help="От 1 до 10 JPG/PNG файлов (<10 МБ каждый)")],
    description: Annotated[str, typer.Option("--desc", "-d")] = "",
    account_id: Annotated[str | None, typer.Option("--account", "-a")] = None,
    mention: Annotated[list[str] | None, typer.Option("--mention", "-m")] = None,
    music_id: Annotated[str, typer.Option("--music-id")] = "",
    music_search: Annotated[str | None, typer.Option("--music-search")] = None,
    force_cooldown: Annotated[bool, typer.Option("--force-cooldown")] = False,
    skip_warmup: Annotated[bool, typer.Option("--skip-warmup")] = False,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Уникализировать карусель фото (EXIF strip + pixel/crop jitter), прогреть и залить."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)
    task = UploadTask(
        task_id=f"cli_{uuid.uuid4().hex[:8]}",
        account_id=account_id,
        media_type="photo",
        photo_paths=[str(p) for p in photos],
        description=description,
        mentions=mention or [],
        music_id=music_id,
        music_search_keyword=music_search,
    )

    async def _run() -> None:
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            acc, rec = await pipeline.execute_upload_task(
                task, force_cooldown=force_cooldown, skip_warmup=skip_warmup
            )
            console.print(
                f"[green]✓ Фото-пост залит на [{acc.account_id}]![/green] aweme_id={rec.aweme_id or 'не распознан'}"
            )

    asyncio.run(_run())


@app.command("run-queue")
def run_queue(
    queue_file: Annotated[Path, typer.Argument(help="JSON-файл очереди задач")],
    daemon: Annotated[bool, typer.Option("--daemon", help="Ждать освобождения кулдаунов в цикле, пока вся очередь не выйдет")] = False,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", help="Интервал проверки кулдаунов в daemon-режиме")] = 60.0,
    force_cooldown: Annotated[bool, typer.Option("--force-cooldown")] = False,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Пакетный конвейер: распределяет очередь видео/фото по аккаунтам с соблюдением лимитов и прогревом."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)

    async def _run() -> None:
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            while True:
                tasks = load_queue(queue_file)
                pending = [t for t in tasks if not t.completed]
                if not pending:
                    console.print("[green]✓ Все задачи в очереди выполнены![/green]")
                    break

                progressed = False
                for task in pending:
                    try:
                        acc, rec = await pipeline.execute_upload_task(
                            task, force_cooldown=force_cooldown
                        )
                        save_queue(queue_file, tasks)
                        progressed = True
                        console.print(
                            f"[green]✓ Задача {task.task_id} залита на [{acc.account_id}] (aweme_id={rec.aweme_id})[/green]"
                        )
                    except RuntimeError as exc:
                        console.print(f"[yellow]⏳ Задача {task.task_id}: {exc}[/yellow]")
                    except Exception as exc:
                        console.print(f"[red]✗ Ошибка задачи {task.task_id}: {exc}[/red]")

                if not daemon:
                    break
                remaining = [t for t in load_queue(queue_file) if not t.completed]
                if not remaining:
                    console.print("[green]✓ Очередь полностью завершена![/green]")
                    break
                if not progressed:
                    console.print(
                        f"[dim]Ожидание кулдаунов ({poll_seconds:.0f} сек)... Осталось задач: {len(remaining)}[/dim]"
                    )
                await asyncio.sleep(poll_seconds)

    asyncio.run(_run())


@app.command("check-shadows")
def check_shadows(
    account_id: Annotated[str | None, typer.Option("--account", "-a")] = None,
    threshold_minutes: Annotated[float, typer.Option("--threshold-min", help="Порог возраста поста (мин) для детекта 0 просмотров")] = 45.0,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Проверить залитые посты на 0 просмотров / скрытие из публичной выдачи."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)
    targets = [store.load_account(account_id)] if account_id else store.list_accounts()

    table = Table(title="Диагностика постов и теневых банов")
    table.add_column("Account", style="bold cyan")
    table.add_column("Aweme ID")
    table.add_column("Возраст (мин)")
    table.add_column("Просмотры")
    table.add_column("В профиле")
    table.add_column("Публичен")
    table.add_column("Вердикт")

    async def _run() -> None:
        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            for acc in targets:
                if not acc.upload_history:
                    continue
                reports = await pipeline.check_account_shadows(
                    acc, zero_view_threshold_minutes=threshold_minutes
                )
                for r in reports:
                    verdict_str = (
                        f"[red]{r.verdict}[/red]"
                        if r.suspected_shadowban
                        else f"[green]{r.verdict}[/green]"
                    )
                    table.add_row(
                        r.account_id,
                        r.aweme_id,
                        f"{r.age_minutes:.1f}",
                        "—" if r.play_count is None else str(r.play_count),
                        "Да" if r.found_in_self_posts else "Нет",
                        "Да" if r.found_in_public_get else "Нет",
                        verdict_str,
                    )

    asyncio.run(_run())
    console.print(table)


@app.command("appeal")
def appeal_restrictions(
    account_id: Annotated[str | None, typer.Option("--account", "-a", help="Конкретный account_id (по умолчанию все авторизованные)")] = None,
    appeal_text: Annotated[
        str,
        typer.Option(
            "--reason",
            help="Текст обоснования апелляции (для форм с полем описания)",
        ),
    ] = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines.",
    loop: Annotated[bool, typer.Option("--loop", help="Крутить проверку и авто-снятие огранов в бесконечном цикле")] = False,
    interval_minutes: Annotated[float, typer.Option("--interval-min", help="Пауза между проходами в режиме --loop (мин)")] = 15.0,
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Автоматически найти и подать апелляции на все ограны аккаунта (`Account ineligible for recommendation`) и постов (`For You feed`)."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)

    async def _run_once() -> None:
        targets = (
            [store.load_account(account_id)]
            if account_id
            else [a for a in store.list_accounts() if a.authenticated and a.device]
        )
        if not targets:
            console.print("[yellow]Нет авторизованных аккаунтов с готовым `x-device`.[/yellow]")
            return

        table = Table(title="Снятие огранов (Account Updates / TNS & TikTok Studio)")
        table.add_column("Account", style="bold cyan")
        table.add_column("Уровень")
        table.add_column("Target / Aweme ID")
        table.add_column("Punish Key")
        table.add_column("Огран")
        table.add_column("Статус до")
        table.add_column("Результат")

        async with KintaroClient(cfg) as client:
            for acc in targets:
                post_ids: list[str] = []
                try:
                    posts_resp = await client.get_self_posts(acc, count=20)
                    store.save_account(acc)
                    for item in extract_feed_items(posts_resp, cfg.response_mapping):
                        aid = extract_aweme_id(item, cfg.response_mapping)
                        if aid and aid not in post_ids:
                            post_ids.append(aid)
                except Exception as exc:
                    console.print(f"[yellow][{acc.account_id}] Не удалось получить список постов через API ({exc}), проверяю уведомления...[/yellow]")

                try:
                    summary = await remove_account_restrictions(
                        acc, post_ids=post_ids, appeal_text=appeal_text
                    )
                except Exception as exc:
                    console.print(f"[red]✗ [{acc.account_id}] Ошибка модуля апелляций: {exc}[/red]")
                    continue

                if not summary.items:
                    table.add_row(
                        acc.account_id,
                        "—",
                        "—",
                        "—",
                        "Чисто (активных огранов не найдено)",
                        "ok",
                        "[green]Огранов нет[/green]",
                    )
                else:
                    for it in summary.items:
                        res_fmt = (
                            f"[bold green]✓ {it.detail}[/bold green]"
                            if it.appealed_now
                            else f"[yellow]{it.detail}[/yellow]"
                        )
                        table.add_row(
                            it.account_id,
                            "Аккаунт/Уведомления" if it.scope == "account_or_inbox" else "Пост (Studio)",
                            it.target_id,
                            it.punish_key or "—",
                            it.title,
                            it.previous_state,
                            res_fmt,
                        )

        console.print(table)

    async def _driver() -> None:
        while True:
            await _run_once()
            if not loop:
                break
            console.print(f"[dim]Следующая проверка огранов через {interval_minutes:.1f} мин...[/dim]")
            await asyncio.sleep(max(10.0, interval_minutes * 60.0))

    asyncio.run(_driver())


@app.command("unban")
def unban_one_or_file(
    target: Annotated[str, typer.Argument(help="Шоп-строка аккаунта, путь к .txt файлу строк, либо account_id из базы")],
    proxy: Annotated[str | None, typer.Option("--proxy", "-p", help="Прокси (обязателен для новых строк/.txt)")] = None,
    region: Annotated[str, typer.Option("--region", "-r", help="Целевой регион профиля (например US)")] = "US",
    appeal_text: Annotated[
        str,
        typer.Option("--reason", help="Текст обоснования апелляции"),
    ] = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines.",
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Всё-в-одном: подключить Outlook Hub / TOTP, войти в TikTok и сразу снять все ограны с аккаунта и постов."""
    _setup_logging(verbose)
    cfg, store = _init_env(config)

    accounts_to_process: list[AccountRecord] = []
    maybe_path = Path(target)
    if maybe_path.is_file():
        if not proxy:
            existing_any = store.list_accounts()
            proxy = existing_any[0].proxy if existing_any else None
        if not proxy:
            raise typer.BadParameter("Укажи прокси через `-p socks5://...` для импорта новых строк.")
        for raw_line in maybe_path.read_text(encoding="utf-8").splitlines():
            s = raw_line.strip()
            if not s or s.startswith("#"):
                continue
            acc = parse_shop_account_line(s, proxy=proxy, region=region)
            if store.account_path(acc.account_id).exists():
                old = store.load_account(acc.account_id)
                if old.device is not None:
                    acc.device = old.device
                    acc.authenticated = old.authenticated
                    acc.first_authenticated_at = old.first_authenticated_at
                    acc.upload_history = old.upload_history
            store.save_account(acc)
            accounts_to_process.append(acc)
    elif ":" in target or "|" in target:
        if not proxy:
            existing_any = store.list_accounts()
            proxy = existing_any[0].proxy if existing_any else None
        if not proxy:
            raise typer.BadParameter("Укажи прокси через `-p socks5://...` для новой строки.")
        acc = parse_shop_account_line(target, proxy=proxy, region=region)
        if store.account_path(acc.account_id).exists():
            old = store.load_account(acc.account_id)
            if old.device is not None:
                acc.device = old.device
                acc.authenticated = old.authenticated
                acc.first_authenticated_at = old.first_authenticated_at
                acc.upload_history = old.upload_history
        store.save_account(acc)
        accounts_to_process.append(acc)
    else:
        accounts_to_process.append(store.load_account(target))

    async def _run() -> None:
        table = Table(title="Авто-вход (Outlook Hub / TOTP) + Снятие огранов")
        table.add_column("Account", style="bold cyan")
        table.add_column("Уровень")
        table.add_column("Target / Aweme ID")
        table.add_column("Punish Key")
        table.add_column("Огран")
        table.add_column("Статус до")
        table.add_column("Результат")

        async with KintaroClient(cfg) as client:
            pipeline = AutomationPipeline(cfg, store, client)
            for acc in accounts_to_process:
                try:
                    if not acc.authenticated or not acc.device:
                        console.print(f"[cyan]→ Вход в \\[{acc.account_id}]...[/cyan]")
                        acc = await pipeline.ensure_authenticated(acc)
                        console.print(f"[green]✓ \\[{acc.account_id}] авторизован![/green]")
                except Exception as exc:
                    console.print(f"[red]✗ \\[{acc.account_id}] Ошибка входа: {exc}[/red]")
                    continue

                post_ids: list[str] = []
                try:
                    posts_resp = await client.get_self_posts(acc, count=20)
                    store.save_account(acc)
                    for item in extract_feed_items(posts_resp, cfg.response_mapping):
                        aid = extract_aweme_id(item, cfg.response_mapping)
                        if aid and aid not in post_ids:
                            post_ids.append(aid)
                except Exception as exc:
                    console.print(f"[yellow]\\[{acc.account_id}] Предупреждение списка постов: {exc}[/yellow]")

                try:
                    summary = await remove_account_restrictions(
                        acc, post_ids=post_ids, appeal_text=appeal_text
                    )
                except Exception as exc:
                    console.print(f"[red]✗ \\[{acc.account_id}] Ошибка модуля апелляций: {exc}[/red]")
                    continue

                if not summary.items:
                    table.add_row(acc.account_id, "—", "—", "—", "Чисто", "ok", "[green]Огранов нет[/green]")
                else:
                    for it in summary.items:
                        res_fmt = (
                            f"[bold green]✓ {it.detail}[/bold green]"
                            if it.appealed_now
                            else f"[yellow]{it.detail}[/yellow]"
                        )
                        table.add_row(
                            it.account_id,
                            "Аккаунт/Уведомления" if it.scope == "account_or_inbox" else "Пост (Studio)",
                            it.target_id,
                            it.punish_key or "—",
                            it.title,
                            it.previous_state,
                            res_fmt,
                        )
        console.print(table)

    asyncio.run(_run())


@app.command("outlook-code")
def outlook_code(
    line: Annotated[str, typer.Argument(help="Строка login:pass:outlook:mail_pass:rec_mail:rec_pass:2fa")],
    wait_new: Annotated[bool, typer.Option("--wait-new", help="Ждать строго новое письмо с кодом (до 90 сек)")] = False,
) -> None:
    """Проверить подключение почты через Outlook Hub (Microsoft Graph API) и прочитать 6-значный код TikTok."""
    import time
    from kintaro_auto.outlook_hub import prepare_outlook_hub_mailbox, wait_for_outlook_hub_code

    acc = parse_shop_account_line(line, proxy="http://127.0.0.1:8080", region="US")
    if not acc.email or not acc.mail_password:
        raise typer.BadParameter("В строке не найдены email и пароль почты Outlook.")

    tok, old_ids = prepare_outlook_hub_mailbox(
        outlook_email=acc.email,
        outlook_login=acc.mail_login or acc.email,
        outlook_password=acc.mail_password,
        recovery_email=acc.recovery_email,
        recovery_password=acc.recovery_password,
        hotmail_totp=acc.mail_totp,
        log_fn=lambda m: console.print(f"[cyan]{m}[/cyan]"),
    )
    console.print(f"[green]✓ Outlook Hub подключён к {acc.email}[/green] (писем в инбоксе: {len(old_ids)})")
    if wait_new:
        code = wait_for_outlook_hub_code(
            outlook_email=acc.email,
            outlook_login=acc.mail_login or acc.email,
            outlook_password=acc.mail_password,
            recovery_email=acc.recovery_email,
            recovery_password=acc.recovery_password,
            hotmail_totp=acc.mail_totp,
            min_timestamp=time.time(),
            pre_access_token=tok,
            known_old_ids=old_ids,
            timeout_sec=90,
            log_fn=lambda m: console.print(f"[cyan]{m}[/cyan]"),
        )
        console.print(f"[bold green]✓ Получен новый код: {code}[/bold green]")


@app.command("uniquify-test")
def uniquify_test(
    media_path: Annotated[Path, typer.Argument(help="Путь к тестовому MP4 или JPG/PNG")],
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
) -> None:
    """Локальная проверка уникализатора медиа (ffmpeg / Pillow) без запросов к API."""
    cfg = load_config(config)
    ext = media_path.suffix.lower()
    if ext == ".mp4":
        res = uniquify_video(media_path, cfg.uniquifier)
        console.print(
            f"[green]✓ Видео уникализировано:[/green] {res.path}\n"
            f"  Размер: {res.size_bytes / 1024:.1f} КБ | SHA-256: {res.sha256}"
        )
    elif ext in (".jpg", ".jpeg", ".png"):
        res_list = uniquify_photos([media_path], cfg.uniquifier)
        res = res_list[0]
        console.print(
            f"[green]✓ Фото уникализировано:[/green] {res.path}\n"
            f"  Размер: {res.size_bytes / 1024:.1f} КБ | SHA-256: {res.sha256}"
        )
    else:
        raise typer.BadParameter("Поддерживаются форматы .mp4, .jpg, .jpeg, .png")


if __name__ == "__main__":
    app()
