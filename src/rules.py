"""Deterministic name normalization driven by rules.json."""
from __future__ import annotations

import json
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

CJK = re.compile(r"[\u4e00-\u9fff]")

_FLAG_MAP = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "x": re.VERBOSE,
}
_MAX_PATTERN_LENGTH = 500


class RulesError(ValueError):
    pass


def _compile(pattern: str, flags: list[str]) -> re.Pattern[str]:
    if len(pattern) > _MAX_PATTERN_LENGTH:
        raise RulesError(
            f"正则过长（{len(pattern)} > {_MAX_PATTERN_LENGTH}），可能触发 ReDoS"
        )
    flag = 0
    for name in flags:
        if name not in _FLAG_MAP:
            raise RulesError(f"unknown regex flag: {name!r}")
        flag |= _FLAG_MAP[name]
    try:
        return re.compile(pattern, flag)
    except re.error as exc:
        raise RulesError(f"invalid regex {pattern!r}: {exc}") from exc


class OperationSpec(BaseModel):
    pattern: str
    replacement: str
    flags: list[str] = Field(default_factory=list)


class RuleSpec(BaseModel):
    enabled: bool = True
    mode: Literal["review", "auto"] = "review"
    operations: list[OperationSpec]
    preconditions: dict = Field(default_factory=dict)


class RuleSetSpec(BaseModel):
    schema_version: int
    execution: dict
    rules: dict[str, RuleSpec]


@dataclass
class Rule:
    rid: str
    enabled: bool
    mode: str
    operations: list[tuple[re.Pattern[str], str]]
    contains_cjk: bool = False
    min_cjk_chars: int = 0


@dataclass
class RuleSet:
    order: list[str]
    rules: dict[str, Rule]
    default_mode: str = "review"


def load_rules(path: str | Path) -> RuleSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        RuleSetSpec.model_validate(data)
    except ValidationError as exc:
        raise RulesError(f"rules.json 校验失败: {exc}") from exc
    if data.get("schema_version") != 1:
        raise RulesError("unsupported rules schema_version")
    execution = data["execution"]
    order = list(execution["order"])
    raw_rules = data["rules"]
    missing = [rid for rid in order if rid not in raw_rules]
    if missing:
        raise RulesError(
            f"rules.json: execution.order 引用了不存在的规则 {missing}"
        )
    unused = [rid for rid in raw_rules if rid not in order]
    if unused:
        warnings.warn(
            f"rules.json: 规则 {unused} 未出现在 execution.order 中，不会被执行",
            stacklevel=2,
        )
    rules: dict[str, Rule] = {}
    for rid in order:
        spec = raw_rules[rid]
        operations = [
            (_compile(op["pattern"], op.get("flags", [])), op["replacement"])
            for op in spec["operations"]
        ]
        preconditions = spec.get("preconditions", {})
        rules[rid] = Rule(
            rid=rid,
            enabled=bool(spec.get("enabled", True)),
            mode=spec.get("mode", execution.get("default_mode", "review")),
            operations=operations,
            contains_cjk=bool(preconditions.get("contains_cjk", False)),
            min_cjk_chars=int(preconditions.get("min_cjk_chars", 0)),
        )
    auto = [rid for rid, rule in rules.items() if rule.enabled and rule.mode == "auto"]
    if auto:
        raise RulesError(
            f"规则 {auto} 声明了 mode=auto，但当前版本所有改名都必须人工批准；"
            "请改为 mode=review（或删除 mode 字段）后重试"
        )
    return RuleSet(order, rules, execution.get("default_mode", "review"))


def apply_rules(name: str, ruleset: RuleSet) -> tuple[str, list[str]]:
    """Apply enabled rules in order. Returns (new name, matched rule ids)."""
    current = name
    matched: list[str] = []
    for rid in ruleset.order:
        rule = ruleset.rules[rid]
        if not rule.enabled:
            continue
        cjk_count = len(CJK.findall(current))
        if rule.contains_cjk and cjk_count == 0:
            continue
        if cjk_count < rule.min_cjk_chars:
            continue
        before = current
        for pattern, replacement in rule.operations:
            current = pattern.sub(replacement, current)
        if current != before:
            if not current.strip():
                current = before
                continue
            matched.append(rid)
    return current, matched
