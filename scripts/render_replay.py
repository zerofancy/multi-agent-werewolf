#!/usr/bin/env python3
"""Validate a werewolf replay and render a self-contained, sequential HTML page.

Usage:
    python3 render_replay.py replay.json --output replay.html
    python3 render_replay.py replay.json --validate-only

Only the Python standard library is used. Validation checks the relationship
between the supplied records and events; it cannot establish whether the input
logs are authentic, whether a summary was really delivered, or who won a game.
"""

from __future__ import annotations

import argparse
from collections import Counter
import html
import json
from pathlib import Path
import string
import sys
from typing import Any


CONTENT_FIELDS = ("public", "team_message", "private", "actions")
TEXT_CHANNELS = {"public": "all", "team_message": "wolves", "private": "review"}
KINDS = {"scene", "dialogue", "action", "thought", "summary", "reflection"}
CHANNEL_LABELS = {
    "all": "全部可见",
    "wolves": "狼队私聊",
    "judge_private": "法官私聊",
    "review": "复盘视角",
}
ORIGIN_LABELS = {
    "verbatim": "逐字原话",
    "recorded_action": "记录中的行动",
    "reconstructed": "过程整理",
}


class ReplayValidationError(ValueError):
    """A collection of input problems with JSON paths for correction."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("\n".join(errors))


class Validator:
    def __init__(self, data: Any):
        self.data = data
        self.errors: list[str] = []
        self.players: dict[int, dict[str, Any]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.coverage: Counter[tuple[str, str]] = Counter()
        self.reflection_actors: set[int] = set()

    def error(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def object(self, value: Any, path: str) -> bool:
        if not isinstance(value, dict):
            self.error(path, "必须是对象")
            return False
        return True

    def array(self, value: Any, path: str) -> bool:
        if not isinstance(value, list):
            self.error(path, "必须是数组")
            return False
        return True

    def text(self, value: Any, path: str, *, nonempty: bool = False) -> bool:
        if not isinstance(value, str):
            self.error(path, "必须是字符串")
            return False
        if nonempty and not value.strip():
            self.error(path, "不得为空")
            return False
        return True

    def choice(self, value: Any, options: set[str], path: str) -> bool:
        if not isinstance(value, str) or value not in options:
            self.error(path, f"必须是以下值之一：{', '.join(sorted(options))}")
            return False
        return True

    def strings(self, value: Any, path: str) -> None:
        if self.array(value, path):
            for index, item in enumerate(value):
                self.text(item, f"{path}[{index}]")

    def seat(self, value: Any, path: str, *, judge_allowed: bool = False) -> bool:
        if type(value) is not int:
            self.error(path, "座位必须是整数")
            return False
        if judge_allowed and value == 0:
            return True
        if value not in self.players:
            self.error(path, f"未知玩家座位 {value}")
            return False
        return True

    def identifier(self, value: Any, path: str, seen: set[str]) -> bool:
        if not self.text(value, path, nonempty=True):
            return False
        if value in seen:
            self.error(path, f"ID 重复：{value}")
            return False
        seen.add(value)
        return True

    def validate_players(self, values: Any) -> None:
        if not self.array(values, "$.players"):
            return
        if not values:
            self.error("$.players", "至少需要一位玩家")
        for index, player in enumerate(values):
            path = f"$.players[{index}]"
            if not self.object(player, path):
                continue
            seat = player.get("seat")
            if type(seat) is not int or seat <= 0:
                self.error(f"{path}.seat", "必须是大于 0 的整数；0 保留给法官")
            elif seat in self.players:
                self.error(f"{path}.seat", f"座位重复：{seat}")
            else:
                self.players[seat] = player
            for key in ("name", "avatar", "role"):
                self.text(player.get(key), f"{path}.{key}", nonempty=True)
            self.choice(player.get("camp"), {"wolves", "good"}, f"{path}.camp")
            persona = player.get("persona")
            if self.object(persona, f"{path}.persona"):
                for key in ("identity", "personality", "speaking_style"):
                    self.text(persona.get(key), f"{path}.persona.{key}", nonempty=True)

    def validate_records(self, values: Any) -> None:
        if not self.array(values, "$.records"):
            return
        seen: set[str] = set()
        for index, record in enumerate(values):
            path = f"$.records[{index}]"
            if not self.object(record, path):
                continue
            unknown = set(record) - {"id", "actor", "phase", *CONTENT_FIELDS}
            if unknown:
                self.error(path, "未知记录字段：" + ", ".join(sorted(unknown))
                           + "；实际行动请归入 actions，调用元数据另存原始日志")
            record_id = record.get("id")
            if self.identifier(record_id, f"{path}.id", seen):
                self.records[record_id] = record
            if (self.data.get("voting_mode") == "sealed"
                    and isinstance(record.get("actions"), dict)
                    and "vote" in record["actions"]):
                if set(record["actions"]) != {"vote"}:
                    self.error(path, "独立锁票只能提交vote，不能混入其他行动")
                if "public" in record or "team_message" in record:
                    self.error(path, "独立锁票不得夹带public或team_message；理由仅交法官")
            self.seat(record.get("actor"), f"{path}.actor")
            self.text(record.get("phase"), f"{path}.phase", nonempty=True)
            for field in CONTENT_FIELDS:
                if field not in record:
                    continue
                if field == "actions":
                    self.object(record[field], f"{path}.{field}")
                else:
                    self.text(record[field], f"{path}.{field}")

    def validate_source(self, event: dict[str, Any], path: str) -> None:
        source = event.get("source")
        if source is None:
            return
        source_path = f"{path}.source"
        if not self.object(source, source_path):
            return
        record_id = source.get("record_id")
        record = None
        if self.text(record_id, f"{source_path}.record_id", nonempty=True):
            record = self.records.get(record_id)
            if record is None:
                self.error(f"{source_path}.record_id", f"未知 record ID：{record_id}")
        fields = source.get("fields")
        if not self.array(fields, f"{source_path}.fields"):
            return
        if not fields:
            self.error(f"{source_path}.fields", "至少需要一个内容字段")
        seen: set[str] = set()
        for index, field in enumerate(fields):
            field_path = f"{source_path}.fields[{index}]"
            if not self.choice(field, set(CONTENT_FIELDS), field_path):
                continue
            if field in seen:
                self.error(field_path, f"字段重复引用：{field}")
            seen.add(field)
            if record is None:
                continue
            if field not in record:
                self.error(field_path, f"记录 {record_id} 不含字段 {field}")
                continue
            self.coverage[(record_id, field)] += 1
            if event.get("actor") != record.get("actor"):
                self.error(f"{path}.actor", f"必须与记录 {record_id} 的 actor 一致")
            if field == "actions":
                if event.get("origin") != "recorded_action":
                    self.error(f"{path}.origin", "actions 来源必须标记为 recorded_action")
                if event.get("kind") != "action":
                    self.error(f"{path}.kind", "actions 来源必须使用 action")
                public_vote = (event.get("channel") == "all"
                               and isinstance(record[field], dict)
                               and set(record[field]) == {"vote"})
                if public_vote and self.data.get("voting_mode") == "sealed":
                    self.error(f"{path}.channel", "sealed投票来源必须私交法官；统一票型另写法官公告")
                if event.get("channel") != "judge_private" and not public_vote:
                    self.error(f"{path}.channel", "只有纯 vote 行动可公开；夜间能力和混合行动须使用 judge_private")
                if event.get("channel") == "judge_private" and event.get("recipients") != [record.get("actor")]:
                    self.error(f"{path}.recipients", "actions 只能将 actor 本人列为接收者")
                if public_vote and (not isinstance(event.get("recipients"), list)
                                    or record.get("actor") not in event["recipients"]):
                    self.error(f"{path}.recipients", "公开唱票必须将投票者列入实际接收者")
            else:
                if event.get("origin") != "verbatim":
                    self.error(f"{path}.origin", f"{field} 来源必须标记为 verbatim")
                if event.get("text") != record[field]:
                    self.error(f"{path}.text", f"必须逐字等于 records[{record_id}].{field}；原话不得改写")
                if event.get("channel") != TEXT_CHANNELS[field]:
                    self.error(f"{path}.channel", f"{field} 来源必须使用 {TEXT_CHANNELS[field]}")
                if field == "private" and event.get("kind") != "thought":
                    self.error(f"{path}.kind", "private 来源必须使用 thought")

    def validate_event(self, event: Any, path: str, seen: set[str]) -> None:
        if not self.object(event, path):
            return
        self.identifier(event.get("id"), f"{path}.id", seen)
        self.seat(event.get("actor"), f"{path}.actor", judge_allowed=True)
        self.choice(event.get("kind"), KINDS, f"{path}.kind")
        self.choice(event.get("channel"), set(CHANNEL_LABELS), f"{path}.channel")
        self.choice(event.get("origin"), set(ORIGIN_LABELS), f"{path}.origin")
        self.text(event.get("title"), f"{path}.title", nonempty=True)
        self.text(event.get("text"), f"{path}.text")
        if "facts" in event:
            self.strings(event["facts"], f"{path}.facts")
        recipients = event.get("recipients")
        if self.array(recipients, f"{path}.recipients"):
            seen_recipients: set[int] = set()
            for index, recipient in enumerate(recipients):
                recipient_path = f"{path}.recipients[{index}]"
                if not self.seat(recipient, recipient_path):
                    continue
                if recipient in seen_recipients:
                    self.error(recipient_path, f"接收座位重复：{recipient}")
                seen_recipients.add(recipient)
                if event.get("channel") == "wolves" and self.players[recipient].get("camp") != "wolves":
                    self.error(recipient_path, f"wolves 通道不能包含非狼人座位 {recipient}")
            if event.get("channel") == "judge_private" and not recipients:
                self.error(f"{path}.recipients", "judge_private 必须列出非空接收座位")
            if event.get("channel") == "all" and not recipients:
                self.error(f"{path}.recipients", "公开事件必须列出实际接收玩家；无人接收不能标为全体公开")
            if event.get("channel") == "wolves" and not recipients:
                self.error(f"{path}.recipients", "wolves 必须列出非空狼人接收座位")
            if event.get("channel") == "review" and recipients:
                self.error(f"{path}.recipients", "review 必须为空数组；内心判断未传给玩家")
        if event.get("actor") == 0 and event.get("origin") == "verbatim":
            self.error(f"{path}.origin", "法官文字不能标记为 verbatim")
        if event.get("origin") == "verbatim" and not isinstance(event.get("source"), dict):
            self.error(f"{path}.source", "verbatim 必须关联原始记录 source")
        if event.get("kind") == "action":
            if event.get("actor") == 0:
                self.error(f"{path}.actor", "action 必须来自玩家，不能由法官扮演")
            if event.get("origin") != "recorded_action":
                self.error(f"{path}.origin", "action 必须标记为 recorded_action")
            if not isinstance(event.get("source"), dict):
                self.error(f"{path}.source", "action 必须关联原始记录 source")
        if event.get("kind") == "thought" and event.get("channel") != "review":
            self.error(f"{path}.channel", "thought 只能使用 review")
        if event.get("kind") == "thought":
            if event.get("actor") == 0:
                self.error(f"{path}.actor", "thought 必须来自玩家；法官复盘请用 summary")
            if event.get("origin") != "verbatim":
                self.error(f"{path}.origin", "thought 必须标记为 verbatim")
            if not isinstance(event.get("source"), dict):
                self.error(f"{path}.source", "thought 必须关联原始记录 source")
        if event.get("kind") == "reflection" and event.get("channel") != "all":
            self.error(f"{path}.channel", "reflection 只能使用 all，赛后感言必须全体可见")
        if event.get("kind") == "reflection":
            actor = event.get("actor")
            if type(actor) is int and actor in self.players:
                self.reflection_actors.add(actor)
            else:
                self.error(f"{path}.actor", "赛后感言必须来自玩家")
            if event.get("origin") != "verbatim":
                self.error(f"{path}.origin", "赛后感言必须标记为 verbatim")
            source = event.get("source")
            if not isinstance(source, dict) or source.get("fields") != ["public"]:
                self.error(f"{path}.source", "赛后感言必须引用该玩家记录的 public 字段")
        self.validate_source(event, path)

    def validate_chapters(self, values: Any) -> None:
        if not self.array(values, "$.chapters"):
            return
        chapters_seen: set[str] = set()
        events_seen: set[str] = set()
        for index, chapter in enumerate(values):
            path = f"$.chapters[{index}]"
            if not self.object(chapter, path):
                continue
            self.identifier(chapter.get("id"), f"{path}.id", chapters_seen)
            self.text(chapter.get("title"), f"{path}.title", nonempty=True)
            if "summary" in chapter:
                self.text(chapter["summary"], f"{path}.summary")
            events = chapter.get("events")
            if self.array(events, f"{path}.events"):
                for event_index, event in enumerate(events):
                    self.validate_event(event, f"{path}.events[{event_index}]", events_seen)

    def validate_postgame(self, postgame: Any) -> None:
        path = "$.postgame"
        if not self.object(postgame, path):
            return
        summary_id = postgame.get("summary_id")
        self.text(summary_id, f"{path}.summary_id", nonempty=True)
        reviewers = postgame.get("reviewers")
        if not self.array(reviewers, f"{path}.reviewers"):
            return
        seen: set[int] = set()
        for index, reviewer in enumerate(reviewers):
            reviewer_path = f"{path}.reviewers[{index}]"
            if not self.object(reviewer, reviewer_path):
                continue
            seat = reviewer.get("seat")
            if self.seat(seat, f"{reviewer_path}.seat"):
                if seat in seen:
                    self.error(f"{reviewer_path}.seat", f"感言玩家重复：{seat}")
                seen.add(seat)
            self.choice(reviewer.get("side"), {"winner", "loser"}, f"{reviewer_path}.side")
            self.text(reviewer.get("selection_reason"), f"{reviewer_path}.selection_reason", nonempty=True)
            received = reviewer.get("received_summary_id")
            if self.text(received, f"{reviewer_path}.received_summary_id", nonempty=True) and received != summary_id:
                self.error(f"{reviewer_path}.received_summary_id", "尚未记录收到该全局总结；必须等于 postgame.summary_id")
        for seat in sorted(seen - self.reflection_actors):
            self.error(f"{path}.reviewers", f"名单中的 {seat}号 玩家缺少 reflection 感言")
        for seat in sorted(self.reflection_actors - seen):
            self.error(f"{path}.reviewers", f"reflection 感言作者 {seat}号 不在 reviewers 名单中")

    def validate(self) -> None:
        if not self.object(self.data, "$"):
            raise ReplayValidationError(self.errors)
        self.text(self.data.get("title"), "$.title", nonempty=True)
        for field in ("subtitle", "winner"):
            if field in self.data:
                self.text(self.data[field], f"$.{field}")
        if "rules" in self.data:
            self.strings(self.data["rules"], "$.rules")
        if "ruleset" in self.data:
            self.text(self.data["ruleset"], "$.ruleset", nonempty=True)
        if "voting_mode" in self.data:
            self.choice(self.data["voting_mode"], {"sealed", "public_sequence"}, "$.voting_mode")
        if self.data.get("ruleset") == "classic-12-guard-v1" and self.data.get("voting_mode") != "sealed":
            self.error("$.voting_mode", "classic-12-guard-v1必须使用sealed独立锁票")
        self.validate_players(self.data.get("players"))
        self.validate_records(self.data.get("records"))
        self.validate_chapters(self.data.get("chapters"))
        for record_id, record in self.records.items():
            for field in CONTENT_FIELDS:
                if field not in record:
                    continue
                count = self.coverage[(record_id, field)]
                if count != 1:
                    self.error(f"$.records[{record_id}].{field}", f"必须恰好覆盖一次；实际覆盖 {count} 次")
        if "postgame" in self.data:
            self.validate_postgame(self.data["postgame"])
        elif self.reflection_actors:
            self.error("$.postgame", "存在 reflection 感言时必须提供全局总结收取回执及 reviewers 名单")
        if self.errors:
            raise ReplayValidationError(self.errors)


def escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def embedded_json(value: Any) -> str:
    """Prevent an input string from closing the application/json script block."""
    return (json_text(value).replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def paragraph(value: str, class_name: str = "") -> str:
    # CSS preserves line breaks. Escaping the whole string preserves exact text.
    attribute = f' class="{class_name}"' if class_name else ""
    return f"<p{attribute}>{escape(value)}</p>"


class Renderer:
    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.players = {player["seat"]: player for player in data["players"]}
        self.records = {record["id"]: record for record in data["records"]}
        self.reviewers = {
            reviewer["seat"]: reviewer
            for reviewer in data.get("postgame", {}).get("reviewers", [])
        }

    def speaker(self, seat: int) -> tuple[str, str]:
        if seat == 0:
            return "⚖️", "法官"
        player = self.players[seat]
        return player["avatar"], f'{seat}号 {player["name"]}'

    def role_badge(self, seat: int) -> str:
        """Actual roles annotate the audience view, never player materials."""
        if seat == 0:
            return ""
        player = self.players[seat]
        return (f'<span class="role-badge role-{player["camp"]}" '
                'title="真实游戏身份·仅观众全知视角，不代表当时已公开">'
                f'{escape(player["role"])}</span>')

    @staticmethod
    def visible_events(chapter: dict[str, Any]) -> list[dict[str, Any]]:
        # Actual judge transmissions use all/judge_private/wolves channels.
        # Preserve raw logs but omit undelivered narration from the timeline.
        return [event for event in chapter["events"]
                if not (event["actor"] == 0 and event["channel"] == "review"
                        and event["kind"] in {"scene", "summary"}
                        and not event.get("source"))]

    def recipients(self, event: dict[str, Any]) -> str:
        seats = event["recipients"]
        if event["channel"] == "all":
            return "当时向全体公开"
        if event["channel"] == "review":
            return "仅供复盘阅读，当时未传给其他玩家"
        names = "、".join(self.speaker(seat)[1] for seat in seats)
        if event["channel"] == "judge_private":
            if event["actor"] == 0:
                return "法官 → " + escape(names)
            return escape(names) + " → 法官"
        return "仅狼队可见：" + escape(names)

    def action_facts(self, event: dict[str, Any]) -> list[str]:
        """Keep every recorded action visible even if its prose omits one."""
        source = event.get("source", {})
        if "actions" not in source.get("fields", []):
            return []
        actions = self.records[source["record_id"]]["actions"]
        labels = {
            "proposal_target": ("狼刀提案", "不提案"),
            "kill_target": ("狼刀目标", "空刀"),
            "inspect_target": ("查验目标", "不查验"),
            "guard_target": ("守护目标", "空守"),
            "poison_target": ("毒药目标", "不用毒药"),
            "vote": ("投票选择", "弃票"),
            "shot_target": ("猎枪目标", "不开枪"),
            "badge_target": ("警徽传递", "撕毁警徽"),
        }
        facts = []
        for key, value in actions.items():
            if key in {"candidate", "withdraw", "explode"} and type(value) is bool:
                label = {"candidate": "竞选警长", "withdraw": "退水", "explode": "狼人自爆"}[key]
                facts.append(label + "：" + ("是" if value else "否"))
            elif key == "direction" and type(value) is int and value in (-1, 1):
                facts.append("发言方向：" + ("顺时针" if value == 1 else "逆时针"))
            elif key == "heal" and type(value) is bool:
                facts.append("解药：" + ("使用" if value else "不用"))
            elif key in labels:
                label, unused = labels[key]
                if value is None:
                    target = unused
                elif type(value) is int and value in self.players:
                    target = self.speaker(value)[1]
                else:
                    target = json_text(value)
                facts.append(label + "：" + target)
            else:
                facts.append(key + "：" + json_text(value))
        return facts

    def cast(self) -> str:
        cards = []
        for player in self.data["players"]:
            persona = player["persona"]
            cards.append(
                '<article class="player-card">'
                f'<div class="player-heading"><span class="avatar" aria-hidden="true">{escape(player["avatar"])}</span>'
                f'<h3><small>{player["seat"]:02d}号</small> {escape(player["name"])} '
                + self.role_badge(player["seat"]) + '</h3></div>'
                '<dl class="persona">'
                f'<div><dt>公开人设</dt><dd>{escape(persona["identity"])}</dd></div>'
                f'<div><dt>性格</dt><dd>{escape(persona["personality"])}</dd></div>'
                f'<div><dt>说话风格</dt><dd>{escape(persona["speaking_style"])}</dd></div>'
                '</dl></article>'
            )
        return '<section class="cast-section" aria-labelledby="cast-title"><h2 id="cast-title">入座人物</h2><div class="cast">' + "".join(cards) + '</div></section>'

    def hero(self) -> str:
        event_count = sum(len(self.visible_events(chapter)) for chapter in self.data["chapters"])
        subtitle = self.data.get("subtitle", "按章节与事件原序展开，向下阅读即可走完整段时间线。")
        return (
            '<p class="eyebrow">WEREWOLF · COMPLETE REPLAY</p>'
            f'<h1>{escape(self.data["title"])}</h1>'
            + paragraph(subtitle, "deck")
            + f'<div class="stats"><span><b>{len(self.players)}</b>位玩家</span>'
            f'<span><b>{len(self.data["chapters"])}</b>个章节</span>'
            f'<span><b>{event_count}</b>条事件</span>'
            f'<span><b>{len(self.records)}</b>份原始记录</span></div>'
            + self.cast()
            + '<div class="legend" aria-label="通道说明">'
            + "".join(f'<span class="channel-{key}">{label}</span>' for key, label in CHANNEL_LABELS.items())
            + '</div><p class="reading-note">昵称右侧标记真实游戏身份，仅供观众全知阅读，不会向玩家泄露其他人的角色。'
            '通道与接收者表示当时实际传话范围；玩家自称身份仍只算声明。'
            '“内心判断·未传话”单独淡化，不是赛内台词。“过程整理”标记实际法官公告或私告。'
            '目录只跳转位置，正文始终按顺序完整展开；仅原始来源可折叠。</p>'
        )

    def navigation(self) -> str:
        links = [
            f'<a href="#chapter-{index}"><span>{index:02d}</span>{escape(chapter["title"])}</a>'
            for index, chapter in enumerate(self.data["chapters"], 1)
        ]
        if self.reviewers:
            links.append('<a href="#postgame-receipts">感言依据</a>')
        links.extend(['<a href="#identities">终局身份表</a>', '<a href="#rules">规则与来源</a>'])
        return '<h2>顺序目录</h2><nav>' + "".join(links) + '</nav><a class="top-link" href="#top">↑ 回到开头</a>'

    def source(self, event: dict[str, Any]) -> str:
        source = event.get("source")
        if not source:
            return ""
        record = self.records[source["record_id"]]
        raw = {field: record[field] for field in source["fields"]}
        return (
            '<details class="source"><summary>原始来源 · '
            f'{escape(source["record_id"])} / {escape(", ".join(source["fields"]))}</summary>'
            f'<p>记录 ID：<code>{escape(record["id"])}</code> · actor={record["actor"]} · '
            f'阶段：{escape(record["phase"])}</p><pre>{escape(json_text(raw))}</pre></details>'
        )

    def receipt(self, seat: int) -> str:
        reviewer = self.reviewers.get(seat)
        if not reviewer:
            return ""
        return (
            '<p class="summary-receipt">法官日志标记：已依据全局总结复盘 · '
            f'<code>{escape(reviewer["received_summary_id"])}</code></p>'
        )

    def event(self, event: dict[str, Any], number: int) -> str:
        avatar, name = self.speaker(event["actor"])
        channel = event["channel"]
        facts = ""
        visible_facts = self.action_facts(event) + event.get("facts", [])
        if visible_facts:
            facts = '<ul class="facts">' + "".join(f'<li>{escape(fact)}</li>' for fact in visible_facts) + '</ul>'
        thought = '<span class="thought-label">内心判断·未传话</span>' if event["kind"] == "thought" else ""
        reflection = '<span class="reflection-label">赛后感言·全体可见</span>' if event["kind"] == "reflection" else ""
        receipt = self.receipt(event["actor"]) if event["kind"] == "reflection" else ""
        origin_label = "原始内心记录" if event["kind"] == "thought" else ORIGIN_LABELS[event["origin"]]
        return (
            f'<article class="event kind-{event["kind"]} channel-{channel}" id="event-{number}" data-event-id="{escape(event["id"])}">'
            '<div class="event-meta">'
            f'<span class="avatar" aria-hidden="true">{escape(avatar)}</span><span class="speaker">{escape(name)}</span>'
            + self.role_badge(event["actor"])
            + f'<span class="scope">{CHANNEL_LABELS[channel]}</span>'
            f'<span class="event-number">{number:03d}</span></div>'
            f'<p class="recipients">{self.recipients(event)}</p>'
            f'<div class="event-heading"><h3>{escape(event["title"])}</h3>'
            f'<span class="origin">{origin_label}</span>{thought}{reflection}</div>'
            + paragraph(event["text"], "event-text") + facts + receipt + self.source(event) + '</article>'
        )

    def chapters(self) -> str:
        chapters = []
        number = 0
        for index, chapter in enumerate(self.data["chapters"], 1):
            events = []
            for event in self.visible_events(chapter):
                number += 1
                events.append(self.event(event, number))
            chapters.append(
                f'<section class="chapter" id="chapter-{index}" data-chapter-id="{escape(chapter["id"])}">'
                f'<header class="chapter-header"><span class="chapter-index">CHAPTER {index:02d}</span>'
                f'<h2>{escape(chapter["title"])}</h2></header>'
                '<div class="chapter-events">' + "".join(events) + '</div></section>'
            )
        return "".join(chapters)

    def postgame(self) -> str:
        if not self.reviewers:
            return ""
        cards = []
        for seat, reviewer in self.reviewers.items():
            avatar, name = self.speaker(seat)
            side = "胜方" if reviewer["side"] == "winner" else "败方"
            cards.append(
                '<article class="reviewer-card">'
                f'<h3><span aria-hidden="true">{escape(avatar)}</span> {escape(name)} '
                + self.role_badge(seat) + f' <small>{side}</small></h3>'
                + self.receipt(seat)
                + paragraph(reviewer["selection_reason"], "selection-reason")
                + f'<p class="receipt-id">总结编号：<code>{escape(reviewer["received_summary_id"])}</code></p></article>'
            )
        return (
            '<section class="appendix" id="postgame-receipts"><h2>赛后感言依据</h2>'
            '<p class="appendix-intro">入选玩家先收到同一份全局总结，再回顾自己的选择。以下列出人选与各自的复盘视角。</p>'
            '<div class="reviewer-grid">' + "".join(cards) + '</div></section>'
        )

    def identities(self) -> str:
        rows = []
        for player in self.data["players"]:
            camp = "狼人阵营" if player["camp"] == "wolves" else "好人阵营"
            rows.append(
                '<tr>'
                f'<th scope="row">{player["seat"]}号 <span aria-hidden="true">{escape(player["avatar"])}</span> {escape(player["name"])}</th>'
                f'<td>{escape(player["role"])}</td><td>{camp}</td></tr>'
            )
        winner = paragraph(f'日志记录的胜方：{self.data["winner"]}', "winner") if self.data.get("winner") else ""
        return (
            '<section class="appendix" id="identities"><h2>终局身份表</h2>' + winner
            + '<table class="identities"><thead><tr><th>玩家</th><th>真实身份</th><th>阵营</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody></table></section>'
        )

    def rules(self) -> str:
        rules = self.data.get("rules", [])
        content = '<ol class="rules-list">' + "".join(f'<li>{escape(rule)}</li>' for rule in rules) + '</ol>' if rules else '<p class="appendix-intro">输入未单列规则。</p>'
        return (
            '<section class="appendix" id="rules"><h2>规则与来源</h2>' + content
            + '<p class="provenance-note">各条目保留原始来源，可展开核对。玩家内心仅供复盘阅读，没有作为赛内对白传话；'
            '法官根据实际记录串联的文字标为“过程整理”。</p></section>'
        )

    def render(self, template_path: Path) -> str:
        template = string.Template(template_path.read_text(encoding="utf-8"))
        return template.substitute(
            document_title=escape(self.data["title"]),
            hero_html=self.hero(),
            nav_html=self.navigation(),
            content_html=self.chapters() + self.postgame() + self.identities() + self.rules(),
            data_json=embedded_json(self.data),
        )


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 对象含重复键：{key}")
        result[key] = value
    return result


def reject_nonfinite(value: str) -> None:
    raise ValueError(f"JSON 不允许非有限数字：{value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", type=Path, help="法官整理的复盘 JSON")
    parser.add_argument("--output", type=Path, help="生成的自包含 HTML 路径")
    parser.add_argument("--validate-only", action="store_true", help="只验证输入，不创建或更新 HTML")
    args = parser.parse_args(argv)
    if not args.validate_only and args.output is None:
        parser.error("生成 HTML 时必须提供 --output；只验证可使用 --validate-only")
    if not args.validate_only and args.input.resolve() == args.output.resolve():
        parser.error("--output 不得覆盖输入 JSON")
    try:
        data = json.loads(args.input.read_text(encoding="utf-8-sig"), object_pairs_hook=reject_duplicate_keys, parse_constant=reject_nonfinite)
        Validator(data).validate()
        count = sum(len(chapter["events"]) for chapter in data["chapters"])
        if args.validate_only:
            print(f"验证通过：{len(data['records'])} 份记录，{count} 条事件；未写入 HTML。")
            return 0
        template_path = Path(__file__).resolve().parent.parent / "assets" / "replay.html"
        rendered = Renderer(data).render(template_path)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"验证通过，已生成：{args.output.resolve()}")
        return 0
    except ReplayValidationError as error:
        print(f"复盘校验失败（{len(error.errors)} 项）：", file=sys.stderr)
        for item in error.errors:
            print(f"  - {item}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError) as error:
        print(f"无法生成复盘：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
