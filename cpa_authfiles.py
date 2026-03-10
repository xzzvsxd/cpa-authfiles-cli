#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CPA auth-files 管理小工具（单文件脚本）。

包含两个核心能力：
1) 自动禁用「普通 free」账号，只保留必要 N 个（默认 5）
2) 对指定账号/文件一键启用或禁用（安全：默认拒绝禁用非 free，除非 --force）

Goal (as requested):
- Disable only normal/free accounts (plan_type == "free")
- Do NOT disable Plus / Team / other paid plans
- Keep the necessary X free accounts enabled (default: 5)

This script talks to CLI Proxy API (CPA) management endpoints:
- GET   /v0/management/auth-files
- PATCH /v0/management/auth-files/status   {"name": "...", "disabled": true/false}

Safe by default:
- 批量操作默认 dry-run（除非 --apply）
- 手动禁用默认只允许 free（非 free 需 --force）

Env vars (optional):
- CPA_MANAGEMENT_BASE_URL   (default: http://127.0.0.1:8317)
- CPA_MANAGEMENT_TOKEN      (required unless --token provided)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_RFC3339_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<frac>\.\d+)?(?P<tz>Z|[+-]\d{2}:\d{2})?$"
)


def _parse_rfc3339_ns(ts: str) -> datetime | None:
    text = str(ts or "").strip()
    if not text:
        return None
    match = _RFC3339_RE.match(text)
    if not match:
        return None
    base = match.group("base")
    frac = match.group("frac") or ""
    tz = match.group("tz") or ""
    if tz == "Z":
        tz = "+00:00"
    if frac:
        digits = frac[1:]
        micros = (digits + "000000")[:6]
        iso = f"{base}.{micros}{tz}"
    else:
        iso = f"{base}{tz}"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _json_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[int, dict[str, Any] | None, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body, method=method.upper())
    for k, v in headers.items():
        req.add_header(k, v)

    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read() or b""
            text = raw.decode("utf-8", errors="replace")
            try:
                data = json.loads(text) if text.strip() else None
            except Exception:
                data = None
            return int(getattr(resp, "status", 200)), (data if isinstance(data, dict) else None), text
    except HTTPError as exc:
        raw = exc.read() if hasattr(exc, "read") else b""
        text = (raw or b"").decode("utf-8", errors="replace")
        try:
            data = json.loads(text) if text.strip() else None
        except Exception:
            data = None
        return int(getattr(exc, "code", 0) or 0), (data if isinstance(data, dict) else None), text
    except URLError as exc:
        raise RuntimeError(f"request failed: {exc}") from exc


@dataclass(frozen=True)
class AuthFile:
    name: str
    disabled: bool
    plan_type: str
    email: str
    account: str
    label: str
    provider: str
    file_type: str
    modtime: str
    created_at: str
    raw: dict[str, Any]

    @property
    def plan_type_norm(self) -> str:
        return str(self.plan_type or "").strip().lower()

    @property
    def is_free(self) -> bool:
        return self.plan_type_norm == "free"

    def sort_ts(self, sort_by: str) -> float:
        sort_by = (sort_by or "modtime").strip().lower()
        if sort_by == "created_at":
            dt = _parse_rfc3339_ns(self.created_at)
        elif sort_by == "modtime":
            dt = _parse_rfc3339_ns(self.modtime)
        elif sort_by == "name":
            # name sort is handled elsewhere
            return 0.0
        else:
            dt = _parse_rfc3339_ns(self.modtime) or _parse_rfc3339_ns(self.created_at)
        if not dt:
            return 0.0
        return dt.timestamp()


def _coerce_auth_files(items: Iterable[Any]) -> list[AuthFile]:
    result: list[AuthFile] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("id") or "").strip()
        if not name:
            continue
        disabled = bool(item.get("disabled"))
        id_token = item.get("id_token") if isinstance(item.get("id_token"), dict) else {}
        plan_type = str(id_token.get("plan_type") or item.get("plan_type") or "").strip()
        email = str(item.get("email") or "").strip()
        account = str(item.get("account") or "").strip()
        label = str(item.get("label") or "").strip()
        provider = str(item.get("provider") or "").strip()
        file_type = str(item.get("type") or "").strip()
        modtime = str(item.get("modtime") or "").strip()
        created_at = str(item.get("created_at") or "").strip()
        result.append(
            AuthFile(
                name=name,
                disabled=disabled,
                plan_type=plan_type,
                email=email,
                account=account,
                label=label,
                provider=provider,
                file_type=file_type,
                modtime=modtime,
                created_at=created_at,
                raw=item,
            )
        )
    return result


def _select_free_to_disable(files: list[AuthFile], keep: int, sort_by: str) -> tuple[list[AuthFile], list[AuthFile]]:
    keep = max(0, int(keep))
    free_enabled = [f for f in files if f.is_free and not f.disabled]
    if not free_enabled:
        return [], []

    sort_by_norm = (sort_by or "modtime").strip().lower()
    if sort_by_norm == "name":
        free_enabled_sorted = sorted(free_enabled, key=lambda f: f.name)
    else:
        free_enabled_sorted = sorted(free_enabled, key=lambda f: f.sort_ts(sort_by_norm), reverse=True)

    keep_set = {f.name for f in free_enabled_sorted[:keep]}
    kept = [f for f in free_enabled_sorted if f.name in keep_set]
    to_disable = [f for f in free_enabled_sorted if f.name not in keep_set]
    return kept, to_disable


def _set_status_one(
    base_url: str,
    token: str,
    name: str,
    disabled: bool,
    timeout_seconds: float,
) -> tuple[str, bool, str]:
    url = f"{base_url}/v0/management/auth-files/status"
    status, data, text = _json_request(
        "PATCH",
        url,
        token=token,
        payload={"name": name, "disabled": bool(disabled)},
        timeout_seconds=timeout_seconds,
    )
    if status != 200:
        return name, False, f"http {status}: {text.strip()[:200]}"
    if isinstance(data, dict) and data.get("status") == "ok":
        return name, True, "ok"
    # Some versions may not return {"status":"ok"}, but still succeed with 200.
    return name, True, "ok(200)"


def _fetch_auth_files(base_url: str, token: str, timeout_seconds: float) -> list[AuthFile]:
    status, data, text = _json_request(
        "GET",
        f"{base_url}/v0/management/auth-files",
        token=token,
        payload=None,
        timeout_seconds=timeout_seconds,
    )
    if status != 200 or not isinstance(data, dict):
        preview = (text or "").strip().replace("\n", " ")[:240]
        raise RuntimeError(f"failed to list auth-files: http {status}: {preview}")
    files_raw = data.get("files")
    return _coerce_auth_files(files_raw if isinstance(files_raw, list) else [])


def _match_query(files: list[AuthFile], query: str, *, contains: bool) -> list[AuthFile]:
    q = str(query or "").strip()
    if not q:
        return []
    q_lower = q.lower()

    def _eq(text: str) -> bool:
        return str(text or "").strip().lower() == q_lower

    # 1) Exact name
    exact_name = [f for f in files if f.name == q]
    if exact_name:
        return exact_name

    # 2) Exact email/account/label (case-insensitive)
    exact_fields = [f for f in files if _eq(f.email) or _eq(f.account) or _eq(f.label)]
    if exact_fields:
        return exact_fields

    # 3) Common free naming: {email}.json
    if "@" in q and not q_lower.endswith(".json"):
        guess = f"{q}.json"
        guess_match = [f for f in files if f.name == guess]
        if guess_match:
            return guess_match

    # 4) Fuzzy contains
    if contains:
        hits: list[AuthFile] = []
        for f in files:
            hay = " ".join([f.name, f.email, f.account, f.label]).lower()
            if q_lower in hay:
                hits.append(f)
        return hits

    return []


def _resolve_targets(
    files: list[AuthFile],
    queries: list[str],
    *,
    contains: bool,
    all_matches: bool,
) -> tuple[list[AuthFile], list[str]]:
    selected: list[AuthFile] = []
    errors: list[str] = []
    seen: set[str] = set()

    for q in queries:
        matches = _match_query(files, q, contains=contains)
        if not matches:
            errors.append(f"no match for: {q}")
            continue
        if len(matches) > 1 and not all_matches:
            names = ", ".join(sorted({m.name for m in matches})[:10])
            suffix = "" if len(matches) <= 10 else f", ... (+{len(matches) - 10})"
            errors.append(f"multiple matches for: {q} -> {names}{suffix} (use --all-matches)")
            continue
        for m in matches:
            if m.name in seen:
                continue
            selected.append(m)
            seen.add(m.name)

    return selected, errors


def _print_auth_file_line(f: AuthFile) -> None:
    plan = f.plan_type_norm or "-"
    status = "disabled" if f.disabled else "enabled"
    ident = f.email or f.account or f.label or "-"
    print(f"{status:8}  plan={plan:6}  {ident:30}  name={f.name}")


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        files = _fetch_auth_files(args.base_url, args.token, args.timeout)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    plan = str(args.plan or "").strip().lower()
    contains_text = str(args.contains or "").strip()

    rows = files
    if plan:
        rows = [f for f in rows if f.plan_type_norm == plan]
    if args.enabled_only:
        rows = [f for f in rows if not f.disabled]
    if args.disabled_only:
        rows = [f for f in rows if f.disabled]
    if contains_text:
        key = contains_text.lower()
        rows = [f for f in rows if key in " ".join([f.name, f.email, f.account, f.label]).lower()]

    rows = sorted(rows, key=lambda f: f.sort_ts("modtime"), reverse=True)
    limit = max(0, int(args.limit))
    if limit:
        rows = rows[:limit]

    print(f"Total: {len(files)} | Matched: {len(rows)}")
    for f in rows:
        _print_auth_file_line(f)
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        files = _fetch_auth_files(args.base_url, args.token, args.timeout)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    targets, errors = _resolve_targets(
        files,
        list(args.query or []),
        contains=bool(args.contains),
        all_matches=bool(args.all_matches),
    )
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if not targets:
        return 1

    for t in targets:
        if args.raw:
            print(json.dumps(t.raw, ensure_ascii=False, indent=2))
        else:
            info = {
                "name": t.name,
                "email": t.email,
                "account": t.account,
                "label": t.label,
                "disabled": t.disabled,
                "plan_type": t.plan_type_norm or t.plan_type,
                "provider": t.provider,
                "type": t.file_type,
                "modtime": t.modtime,
                "created_at": t.created_at,
            }
            print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def _cmd_set_status(args: argparse.Namespace, *, disabled: bool) -> int:
    try:
        files = _fetch_auth_files(args.base_url, args.token, args.timeout)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    targets, errors = _resolve_targets(
        files,
        list(args.query or []),
        contains=bool(args.contains),
        all_matches=bool(args.all_matches),
    )
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if not targets:
        return 1

    if disabled and (not args.force):
        blocked = [t for t in targets if not t.is_free]
        if blocked:
            print("ERROR: refuse to disable non-free accounts (plus/team/etc) without --force.", file=sys.stderr)
            for t in blocked[:20]:
                _print_auth_file_line(t)
            if len(blocked) > 20:
                print(f"... ({len(blocked) - 20} more)", file=sys.stderr)
            return 2

    if args.dry_run:
        action = "DISABLE" if disabled else "ENABLE"
        print(f"Dry-run: would {action} {len(targets)} auth-files:")
        for t in targets:
            _print_auth_file_line(t)
        return 0

    workers = max(1, min(int(args.workers), 100))
    timeout_seconds = max(1.0, float(args.timeout))
    ok = 0
    failed = 0
    futures = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for t in targets:
            futures.append(executor.submit(_set_status_one, args.base_url, args.token, t.name, disabled, timeout_seconds))
        for fut in as_completed(futures):
            name, success, msg = fut.result()
            if success:
                ok += 1
                if args.verbose:
                    print(f"[OK] {name} -> {'disabled' if disabled else 'enabled'}")
            else:
                failed += 1
                print(f"[FAIL] {name}: {msg}", file=sys.stderr)

    print(f"Done. ok={ok}, failed={failed}.")
    return 0 if failed == 0 else 1


def _cmd_enable(args: argparse.Namespace) -> int:
    return _cmd_set_status(args, disabled=False)


def _cmd_disable(args: argparse.Namespace) -> int:
    return _cmd_set_status(args, disabled=True)


def _cmd_prune_free(args: argparse.Namespace) -> int:
    try:
        files = _fetch_auth_files(args.base_url, args.token, args.timeout)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    kept, to_disable = _select_free_to_disable(files, keep=args.keep, sort_by=args.sort_by)
    all_free = [f for f in files if f.is_free]
    free_enabled = [f for f in all_free if not f.disabled]
    non_free_enabled = [f for f in files if (not f.is_free) and (not f.disabled)]

    print(f"Total auth-files: {len(files)}")
    print(f"Free plan: {len(all_free)} (enabled: {len(free_enabled)})")
    print(f"Non-free plan (plus/team/etc): {len(files) - len(all_free)} (enabled: {len(non_free_enabled)})")
    print(f"Keep enabled free accounts: {max(0, int(args.keep))} (chosen from enabled: {len(kept)})")
    print(f"Will disable free accounts: {len(to_disable)}")
    if len(kept) < max(0, int(args.keep)):
        print("WARN: enabled free accounts < --keep, this command will not auto-enable accounts.", file=sys.stderr)
    if kept:
        print("Kept free accounts (name):")
        for f in kept[:20]:
            print(f"  - {f.name}")
        if len(kept) > 20:
            print(f"  ... ({len(kept) - 20} more)")

    if not to_disable:
        print("Nothing to disable.")
        return 0

    if args.max_disable and int(args.max_disable) > 0 and len(to_disable) > int(args.max_disable):
        to_disable = to_disable[: int(args.max_disable)]
        print(f"NOTE: --max-disable applied, only disabling first {len(to_disable)} accounts this run.")

    if not args.apply:
        print("Dry-run only. Re-run with --apply to actually disable these accounts.")
        return 0

    workers = max(1, min(int(args.workers), 200))
    timeout_seconds = max(1.0, float(args.timeout))
    ok = 0
    failed = 0
    futures = []
    print(f"Disabling {len(to_disable)} free accounts with workers={workers} ...")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for f in to_disable:
            futures.append(executor.submit(_set_status_one, args.base_url, args.token, f.name, True, timeout_seconds))
        for idx, fut in enumerate(as_completed(futures), start=1):
            name, success, msg = fut.result()
            if success:
                ok += 1
                if args.verbose:
                    print(f"[OK] {name} -> disabled")
            else:
                failed += 1
                print(f"[FAIL] {name}: {msg}", file=sys.stderr)

            if (not args.verbose) and (idx % 50 == 0 or idx == len(futures)):
                print(f"Progress: {idx}/{len(futures)} (ok={ok}, failed={failed})")

    print(f"Done. Disabled ok={ok}, failed={failed}.")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    epilog = """Examples:
  # 1) 列出所有 team 账号（只看 enabled）
  python3 cpa_authfiles.py list --plan team --enabled-only

  # 2) 查看某个账号/文件的详细信息（支持 email / name / 文件名）
  python3 cpa_authfiles.py show ACCOUNT_QUERY

  # 3) 一键禁用某个 free 账号（默认拒绝禁用非 free，除非 --force）
  python3 cpa_authfiles.py disable ACCOUNT_QUERY

  # 4) 一键启用
  python3 cpa_authfiles.py enable ACCOUNT_QUERY

  # 5) 批量：只保留 5 个 free 账号，其他 free 全部禁用（默认 dry-run）
  python3 cpa_authfiles.py prune-free --keep 5

  # 6) 批量：真正执行禁用（危险）
  python3 cpa_authfiles.py prune-free --keep 5 --apply
"""

    parser = argparse.ArgumentParser(
        prog="cpa_authfiles.py",
        description="CLIProxyAPI (CPA) auth-files 管理脚本：禁用 free 账号 / 启用禁用指定账号。",
        epilog=epilog,
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("CPA_MANAGEMENT_BASE_URL", "http://127.0.0.1:8317"),
        help="CPA base URL (env: CPA_MANAGEMENT_BASE_URL, default: http://127.0.0.1:8317)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("CPA_MANAGEMENT_TOKEN", ""),
        help="CPA management token (env: CPA_MANAGEMENT_TOKEN)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout seconds (default: 30)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出 auth-files（可筛选 plan/状态）")
    p_list.add_argument("--plan", default="", help="过滤 plan_type，例如: free / team / plus")
    p_list.add_argument("--enabled-only", action="store_true", help="只显示 enabled 项")
    p_list.add_argument("--disabled-only", action="store_true", help="只显示 disabled 项")
    p_list.add_argument("--contains", default="", help="模糊筛选（name/email/label 包含）")
    p_list.add_argument("--limit", type=int, default=50, help="最多显示 N 条（0=不限制，默认 50）")
    p_list.set_defaults(func=_cmd_list)

    p_show = sub.add_parser("show", help="查看指定账号/文件的详细信息")
    p_show.add_argument("query", nargs="+", help="支持：email 或 name（也可配合 --contains 模糊）")
    p_show.add_argument("--contains", action="store_true", help="使用 contains 模糊匹配（命中多条需 --all-matches）")
    p_show.add_argument("--all-matches", action="store_true", help="当同一个 query 命中多条时，对全部输出")
    p_show.add_argument("--raw", action="store_true", help="输出原始 JSON（来自 /auth-files 列表项）")
    p_show.set_defaults(func=_cmd_show)

    p_enable = sub.add_parser("enable", help="一键启用（disabled=false）")
    p_enable.add_argument("query", nargs="+", help="email 或 name")
    p_enable.add_argument("--contains", action="store_true", help="contains 模糊匹配（命中多条需 --all-matches）")
    p_enable.add_argument("--all-matches", action="store_true", help="对 query 的所有命中项执行")
    p_enable.add_argument("--dry-run", action="store_true", help="只预览，不执行 PATCH")
    p_enable.add_argument("--workers", type=int, default=8, help="并发（默认 8）")
    p_enable.add_argument("--verbose", action="store_true", help="打印每条的执行结果")
    p_enable.set_defaults(func=_cmd_enable)

    p_disable = sub.add_parser("disable", help="一键禁用（disabled=true）")
    p_disable.add_argument("query", nargs="+", help="email 或 name")
    p_disable.add_argument("--contains", action="store_true", help="contains 模糊匹配（命中多条需 --all-matches）")
    p_disable.add_argument("--all-matches", action="store_true", help="对 query 的所有命中项执行")
    p_disable.add_argument("--dry-run", action="store_true", help="只预览，不执行 PATCH")
    p_disable.add_argument("--force", action="store_true", help="允许禁用非 free（plus/team 等），默认拒绝")
    p_disable.add_argument("--workers", type=int, default=8, help="并发（默认 8）")
    p_disable.add_argument("--verbose", action="store_true", help="打印每条的执行结果")
    p_disable.set_defaults(func=_cmd_disable)

    p_prune = sub.add_parser("prune-free", help="批量禁用 free：只保留 N 个 enabled")
    p_prune.add_argument("--keep", type=int, default=5, help="保留 enabled 的 free 数量（默认 5）")
    p_prune.add_argument(
        "--sort-by",
        choices=["modtime", "created_at", "name"],
        default="modtime",
        help="选择保留的依据（默认 modtime=最近修改优先）",
    )
    p_prune.add_argument("--workers", type=int, default=16, help="并发（默认 16）")
    p_prune.add_argument(
        "--max-disable",
        type=int,
        default=0,
        help="安全阈值：本次最多禁用多少个（0=不限制）",
    )
    p_prune.add_argument("--apply", action="store_true", help="真正执行禁用（默认 dry-run）")
    p_prune.add_argument("--verbose", action="store_true", help="打印每条的执行结果")
    p_prune.set_defaults(func=_cmd_prune_free)

    args = parser.parse_args(argv)

    args.base_url = str(args.base_url or "").rstrip("/")
    args.token = str(args.token or "").strip()
    args.timeout = max(1.0, float(args.timeout))

    if not args.token:
        print("ERROR: missing token. Provide --token or set CPA_MANAGEMENT_TOKEN.", file=sys.stderr)
        return 2

    # Unify positional argument name for commands
    if getattr(args, "query", None) is not None:
        args.query = list(args.query)

    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
