"""Pure rule parsing and evaluation for the MoviePilot seed workgroup plugin."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional, Tuple


BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class ContractRule:
    site_name: str
    official: bool
    official_keywords: Tuple[str, ...]
    min_size_bytes: int
    min_count: int
    duration_days: int
    start_date: date


@dataclass(frozen=True)
class SiteMetrics:
    total_count: int = 0
    total_size_bytes: int = 0
    official_count: int = 0
    official_size_bytes: int = 0
    error: Optional[str] = None


@dataclass(frozen=True)
class CheckResult:
    rule: ContractRule
    status: str
    current_count: int
    current_size_bytes: int
    size_gap_bytes: int
    count_gap: int
    days_elapsed: int
    days_remaining: int
    error: Optional[str] = None


def _parse_date(value: str) -> date:
    for pattern in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date()
        except ValueError:
            continue
    raise ValueError(f"日期格式无效：{value}，应为 YYYY/MM/DD")


def parse_rules(text: str) -> list[ContractRule]:
    """Parse one rule per line: site|official|keywords|GB|count|days|start."""
    rules: list[ContractRule] = []
    for line_number, raw_line in enumerate((text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split("|")]
        if len(fields) != 7:
            raise ValueError(f"第 {line_number} 行需要 7 个字段，实际为 {len(fields)} 个")
        site_name, official_text, keywords_text, size_text, count_text, duration_text, start_text = fields
        if not site_name:
            raise ValueError(f"第 {line_number} 行站点名称不能为空")
        official_value = official_text.lower()
        if official_value in {"是", "yes", "true", "1"}:
            official = True
        elif official_value in {"否", "no", "false", "0"}:
            official = False
        else:
            raise ValueError(f"第 {line_number} 行是否官种只能填写 是 或 否")
        try:
            min_size_gb = float(size_text)
            min_count = int(count_text)
            duration_days = int(duration_text)
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行体积、数量和周期必须是数字") from exc
        if min_size_gb < 0 or min_count < 0 or duration_days < 0:
            raise ValueError(f"第 {line_number} 行体积、数量和周期不能为负数")
        rules.append(
            ContractRule(
                site_name=site_name,
                official=official,
                official_keywords=tuple(
                    keyword.strip() for keyword in keywords_text.split(",") if keyword.strip()
                ),
                min_size_bytes=int(min_size_gb * BYTES_PER_GB),
                min_count=min_count,
                duration_days=duration_days,
                start_date=_parse_date(start_text),
            )
        )
    return rules


def evaluate_rule(rule: ContractRule, metrics: SiteMetrics, as_of: Optional[date] = None) -> CheckResult:
    """Evaluate one contract with inclusive size/count thresholds."""
    as_of = as_of or date.today()
    elapsed = max(0, (as_of - rule.start_date).days)
    remaining = max(0, rule.duration_days - elapsed)
    if metrics.error:
        return CheckResult(
            rule, "获取失败", 0, 0, rule.min_size_bytes, rule.min_count, elapsed, remaining, metrics.error
        )
    if rule.official and not rule.official_keywords:
        return CheckResult(
            rule, "未配置官组", metrics.official_count, metrics.official_size_bytes,
            max(0, rule.min_size_bytes - metrics.official_size_bytes),
            max(0, rule.min_count - metrics.official_count), elapsed, remaining,
        )
    if rule.official:
        current_count = metrics.official_count
        current_size = metrics.official_size_bytes
    else:
        current_count = metrics.total_count
        current_size = metrics.total_size_bytes
    size_gap = max(0, rule.min_size_bytes - current_size)
    count_gap = max(0, rule.min_count - current_count)
    size_ok = current_size >= rule.min_size_bytes
    count_ok = current_count >= rule.min_count
    if not size_ok or not count_ok:
        status = "未达标"
    elif remaining:
        status = "保种达标"
    else:
        status = "已完成"
    return CheckResult(rule, status, current_count, current_size, size_gap, count_gap, elapsed, remaining)


def format_size(size_bytes: int) -> str:
    value = max(0, size_bytes) / BYTES_PER_GB
    if value >= 1024:
        return f"{value / 1024:.2f} TB"
    return f"{value:.2f} GB"


def format_report(results: Iterable[CheckResult]) -> str:
    lines = ["【保种工作组检查】"]
    for result in results:
        rule = result.rule
        current_kind = "官种" if rule.official else "总做种"
        line = (
            f"{rule.site_name}：{result.status} | {current_kind} "
            f"{format_size(result.current_size_bytes)} / {format_size(rule.min_size_bytes)}，"
            f"{result.current_count} / {rule.min_count}，剩余 {result.days_remaining} 天"
        )
        if result.size_gap_bytes or result.count_gap:
            line += f"（还差 {format_size(result.size_gap_bytes)}、{result.count_gap} 个）"
        if result.error:
            line += f" | {result.error}"
        lines.append(line)
    return "\n".join(lines)
