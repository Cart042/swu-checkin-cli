"""Bounded, offline-testable account runner used by the CLI."""

import logging
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import config


def _configured_max_workers(account_count, logger=None, *, options=None):
    """Bound the configured worker count to the number of supplied accounts."""

    runtime_options = options or config.parse_runtime_options(logger=logger)
    configured = runtime_options.max_workers
    return max(1, min(configured, max(1, account_count)))


def _run_one_account(
    index,
    account,
    total_accounts,
    force_login,
    checkin_func,
    deadline,
    status_messages,
    logger,
    deadline_exception,
):
    username = account["username"]
    password = account["password"]
    logger.info("[%s/%s] 开始为账号 %s 执行签到...", index, total_accounts, username)
    try:
        result = checkin_func(username, password, force_login=force_login, deadline=deadline)
        logger.info(
            "[%s/%s] 账号 %s 签到结果: %s",
            index,
            total_accounts,
            username,
            status_messages.get(result, "未知状态"),
        )
        return index, username, result, None
    except deadline_exception as exc:
        logger.error("[%s/%s] 账号 %s 达到 deadline：%s", index, total_accounts, username, exc)
        return index, username, 4, str(exc)
    except Exception as exc:
        logger.error("[%s/%s] 账号 %s 签到执行异常：%s", index, total_accounts, username, exc)
        return index, username, 10, str(exc)


def _run_batch(
    batch_accounts,
    *,
    executor,
    total_accounts,
    force_login,
    checkin_func,
    deadline,
    status_messages,
    retryable_statuses,
    terminal_success_statuses,
    deadline_exception,
    max_rounds,
    final_results,
    attempt,
    clock,
    logger,
):
    """Submit one round to a caller-owned executor and collect its results."""
    pending_accounts = []
    futures = {}
    for index, account in enumerate(batch_accounts, 1):
        if clock() >= deadline:
            pending_accounts.append(account)
            continue
        future = executor.submit(
            _run_one_account,
            index,
            account,
            total_accounts,
            force_login,
            checkin_func,
            deadline,
            status_messages,
            logger,
            deadline_exception,
        )
        futures[future] = account

    while futures:
        remaining = deadline - clock()
        if remaining <= 0:
            pending_accounts.extend(futures.values())
            # Do not leave queued work to start after the run deadline.  A
            # running call still receives the same deadline and is drained by
            # the executor's final shutdown.
            for future in futures:
                future.cancel()
            break
        done, _ = wait(futures, timeout=remaining, return_when=FIRST_COMPLETED)
        if not done:
            pending_accounts.extend(futures.values())
            for future in futures:
                future.cancel()
            break
        for future in done:
            account = futures.pop(future)
            username = account["username"]
            try:
                _, user, result, error = future.result()
            except Exception as exc:
                user, result, error = username, 10, str(exc)
                logger.error("线程执行异常 (%s): %s", username, exc)

            status_message = f"执行异常：{error}" if error else status_messages.get(result, "未知状态")
            is_ok = result in terminal_success_statuses and not error
            final_results[user] = (status_message, is_ok, attempt)
            if not is_ok and result in retryable_statuses and attempt < max_rounds and clock() < deadline:
                pending_accounts.append(account)
            elif not is_ok and result not in retryable_statuses:
                logger.error("账号 %s 返回不可自动重试状态 %s。", username, result)

    return pending_accounts


def _run_rounds(
    pending_accounts,
    *,
    executor,
    force_login,
    checkin_func,
    overall_deadline,
    max_rounds,
    retry_interval,
    status_messages,
    retryable_statuses,
    terminal_success_statuses,
    deadline_exception,
    sleep_func,
    clock,
    final_results,
    logger,
):
    """Run retry rounds while reusing one bounded executor."""
    attempt = 1
    while pending_accounts and attempt <= max_rounds and clock() < overall_deadline:
        batch_accounts = pending_accounts
        pending_accounts = []
        logger.info("开始第 %s 轮打卡，本轮账号数：%s", attempt, len(batch_accounts))
        pending_accounts = _run_batch(
            batch_accounts,
            executor=executor,
            total_accounts=len(batch_accounts),
            force_login=force_login,
            checkin_func=checkin_func,
            deadline=overall_deadline,
            status_messages=status_messages,
            retryable_statuses=retryable_statuses,
            terminal_success_statuses=terminal_success_statuses,
            deadline_exception=deadline_exception,
            max_rounds=max_rounds,
            final_results=final_results,
            attempt=attempt,
            clock=clock,
            logger=logger,
        )

        if pending_accounts and clock() < overall_deadline and attempt < max_rounds:
            retry_users = ", ".join(account["username"] for account in pending_accounts)
            logger.warning("仍有 %s 个账号可重试：%s", len(pending_accounts), retry_users)
            sleep_for = min(retry_interval, max(0, overall_deadline - clock()))
            if sleep_for > 0:
                logger.warning("将在 %s 秒后重试未完成账号。", sleep_for)
                sleep_func(sleep_for)
            attempt += 1
        else:
            break
    return pending_accounts, attempt


def run_accounts(
    accounts,
    *,
    force_login=False,
    checkin_func,
    validate_accounts,
    status_messages,
    retryable_statuses,
    terminal_success_statuses,
    deadline_exception=Exception,
    sleep_func=time.sleep,
    clock=time.monotonic,
    logger=None,
):
    """Run accounts with bounded rounds and an overall deadline.

    ``checkin_func`` is injected so offline tests never log in or call the
    school APIs.
    """
    logger = logger or logging.getLogger(__name__)
    accounts = validate_accounts(accounts)
    if not accounts:
        return "没有可执行的账号。", 1, {}

    runtime_options = config.parse_runtime_options(logger=logger)
    max_workers = _configured_max_workers(
        len(accounts),
        logger,
        options=runtime_options,
    )
    retry_interval = runtime_options.retry_interval_seconds
    max_rounds = runtime_options.max_rounds
    deadline_seconds = runtime_options.run_deadline_seconds
    overall_deadline = clock() + deadline_seconds
    pending_accounts = list(accounts)
    final_results: dict[str, tuple[str, bool, int]] = {}

    logger.info("并发执行：最大线程数 = %s", max_workers)
    logger.info("失败账号最多重试 %s 轮，运行 deadline=%s 秒。", max_rounds, deadline_seconds)

    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        pending_accounts, attempt = _run_rounds(
            pending_accounts,
            executor=executor,
            force_login=force_login,
            checkin_func=checkin_func,
            overall_deadline=overall_deadline,
            max_rounds=max_rounds,
            retry_interval=retry_interval,
            status_messages=status_messages,
            retryable_statuses=retryable_statuses,
            terminal_success_statuses=terminal_success_statuses,
            deadline_exception=deadline_exception,
            sleep_func=sleep_func,
            clock=clock,
            final_results=final_results,
            logger=logger,
        )
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    for account in pending_accounts:
        username = account["username"]
        if clock() >= overall_deadline:
            reason = "达到本次运行 deadline，未完成"
        else:
            reason = f"达到最大重试轮数 {max_rounds}，未完成"
        final_results[username] = (reason, False, min(attempt, max_rounds))

    success_count = sum(1 for _, is_ok, _ in final_results.values() if is_ok)
    failed_count = len(accounts) - success_count
    summary_content = f"打卡执行完毕！成功: {success_count} 个，失败: {failed_count} 个。"
    summary_content += f"\n总轮次: {min(attempt, max_rounds)} 轮。\n\n打卡详情:"
    for username in sorted(final_results):
        status, is_ok, done_attempt = final_results[username]
        icon = "✅" if is_ok else "❌"
        retry_note = f"（第 {done_attempt} 轮完成）" if done_attempt > 1 else ""
        summary_content += f"\n{icon} 账号 {username}: {status}{retry_note}"
    logger.info("\n%s", summary_content)
    return summary_content, (0 if failed_count == 0 else 1), final_results
