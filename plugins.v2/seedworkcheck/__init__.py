"""MoviePilot V2 plugin for configurable multi-site seed workgroup checks."""

import re
import requests
from datetime import date, datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils

from .logic import CheckResult, SiteMetrics, evaluate_rule, format_report, parse_rules


lock = Lock()


class SeedWorkCheck(_PluginBase):
    plugin_name = "保种工作组检查"
    plugin_desc = "按站点和工作组规则检查保种体积、数量和持续时间。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/contract.png"
    plugin_version = "1.0.1"
    plugin_author = "OpenAI"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "seedworkcheck_"
    plugin_order = 2
    auth_level = 2

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "5 1 * * *"
    _queue_count = 5
    _rules_text = ""
    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()
        self._rules = []
        self._metrics: Dict[str, SiteMetrics] = {}
        self._results: List[CheckResult] = []
        self._site_schema = []

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = str(config.get("cron") or "5 1 * * *").strip()
        self._queue_count = max(1, int(config.get("queue_count") or 5))
        self._rules_text = str(config.get("rules") or "")
        try:
            self._rules = parse_rules(self._rules_text)
        except ValueError as exc:
            self._rules = []
            logger.error("保种工作组规则配置错误：%s", exc)

        self._metrics = self.get_data("metrics") or {}
        self._results = []
        if self._enabled or self._onlyonce:
            module_name = f"{self.__module__}.siteuserinfo"
            self._site_schema = ModuleHelper.load(
                module_name,
                filter_func=lambda _, obj: hasattr(obj, "schema"),
            )
            self._site_schema.sort(key=lambda item: item.order)
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    self.refresh_all_site_data,
                    "date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                )
                self._onlyonce = False
                self._save_config()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/seedwork_check",
                "event": EventType.PluginAction,
                "desc": "检查保种工作组",
                "category": "做种",
                "data": {"action": "seedwork_check"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [
            {
                "id": "SeedWorkgroupCheck",
                "name": "保种工作组每日检查",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.refresh_all_site_data,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._field("VSwitch", "enabled", "启用插件", 4),
                            self._field("VSwitch", "notify", "发送每日通知", 4),
                            self._field("VSwitch", "onlyonce", "保存后立即检查", 4),
                            self._field("VTextField", "cron", "执行周期", 4, "5位 Cron，例如 5 1 * * *"),
                            self._field("VTextField", "queue_count", "并发请求数", 4, "默认 5"),
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rules",
                                            "label": "站点规则",
                                            "rows": 10,
                                            "placeholder": "站点|是否官种|官组关键词|体积GB|数量|周期天数|开始日期",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "每行一条规则，字段用 | 分隔；官组关键词用英文逗号分隔。例：NovaHD|是|NovaHD,官方组|5|2|365|2025/08/24。非官种规则的关键词留空，数量填 0 表示不检查数量。",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "queue_count": 5,
            "rules": "",
        }

    @staticmethod
    def _field(component: str, model: str, label: str, md: int, placeholder: str = "") -> dict:
        props = {"model": model, "label": label}
        if placeholder:
            props["placeholder"] = placeholder
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": component, "props": props}],
        }

    def get_page(self) -> List[dict]:
        if not self._results:
            return [{"component": "VAlert", "props": {"type": "info", "text": "暂无检查结果，请先保存配置或执行一次检查。"}}]
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": self._status_type(result.status),
                    "variant": "tonal",
                    "text": self._result_text(result),
                },
            }
            for result in self._results
        ]

    def get_dashboard(self, key: str = "", **kwargs):
        return {"cols": 12}, {"title": "保种工作组检查", "refresh": 300}, self.get_page()

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as exc:
            logger.error("停止保种工作组检查服务失败：%s", exc)

    @eventmanager.register(EventType.PluginAction)
    def refresh(self, event: Event):
        if event and (event.event_data or {}).get("action") != "seedwork_check":
            return
        self.refresh_all_site_data()
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="保种工作组检查完成",
                userid=event.event_data.get("user"),
            )

    def refresh_all_site_data(self):
        if not self._rules:
            logger.warning("保种工作组检查未配置站点规则")
            return
        with lock:
            self._metrics = {}
            sites = [site for site in SitesHelper().get_indexers() if not site.get("public")]
            site_map = {str(site.get("name")): site for site in sites}
            unique_names = sorted({rule.site_name for rule in self._rules})
            targets = [site_map[name] for name in unique_names if name in site_map]
            for missing in [name for name in unique_names if name not in site_map]:
                self._metrics[missing] = SiteMetrics(error="MoviePilot 未找到该站点，名称需与站点列表完全一致")
            if targets:
                with ThreadPool(min(len(targets), self._queue_count)) as pool:
                    pool.map(self._refresh_one_site, targets)
            self._results = [
                evaluate_rule(rule, self._metrics.get(rule.site_name, SiteMetrics(error="未获取到站点数据")))
                for rule in self._rules
            ]
            self.save_data("metrics", {name: self._metrics_to_dict(metrics) for name, metrics in self._metrics.items()})
            self.save_data("results", [self._result_to_dict(result) for result in self._results])
            if self._notify:
                self.post_message(mtype=NotificationType.SiteMessage, title="保种工作组每日检查", text=format_report(self._results))

    def _refresh_one_site(self, site_info: dict):
        name = str(site_info.get("name"))
        site_rules = [rule for rule in self._rules if rule.site_name == name]
        try:
            site_user_info = self._build(site_info)
            if not site_user_info:
                self._metrics[name] = SiteMetrics(error="站点页面无法解析或 Cookie 已失效")
                return
            keywords = sorted({keyword for rule in site_rules for keyword in rule.official_keywords})
            site_user_info.official_team[name] = keywords
            site_user_info.parse_official_seeding_info()
            self._metrics[name] = SiteMetrics(
                total_count=int(site_user_info.total_seeding_size[0] or 0),
                total_size_bytes=int(site_user_info.total_seeding_size[1] or 0),
                official_count=int(site_user_info.official_seeding_size[0] or 0),
                official_size_bytes=int(site_user_info.official_seeding_size[1] or 0),
                error=site_user_info.err_msg,
            )
        except Exception as exc:
            logger.error("站点 %s 保种数据获取失败：%s", name, exc, exc_info=True)
            self._metrics[name] = SiteMetrics(error=str(exc))

    def _build(self, site_info: dict):
        site_cookie = site_info.get("cookie")
        url = site_info.get("url")
        if not site_cookie or not url:
            return None
        site_name = str(site_info.get("name"))
        ua = site_info.get("ua")
        proxy = site_info.get("proxy")
        proxies = settings.PROXY if proxy else None
        with requests.Session() as session:
            response = RequestUtils(cookies=site_cookie, session=session, ua=ua, proxies=proxies).get_res(url=url)
            if not response or response.status_code != 200:
                return None
            response.encoding = "utf-8" if re.search(r"charset=\"?utf-8", response.text, re.I) else response.apparent_encoding
            html_text = response.text
            if not html_text:
                return None
            for schema in self._site_schema:
                if schema.match(html_text):
                    return schema(site_name, url, site_cookie, html_text, session=session, ua=ua, proxy=proxy)
        return None

    @staticmethod
    def _status_type(status: str) -> str:
        return {"已完成": "success", "保种达标": "info", "未配置官组": "warning", "获取失败": "error"}.get(status, "warning")

    @staticmethod
    def _result_text(result: CheckResult) -> str:
        rule = result.rule
        kind = "官种" if rule.official else "总做种"
        return f"{rule.site_name} | {result.status} | {kind} {result.current_count} 个 / {rule.min_count} 个 | 剩余 {result.days_remaining} 天"

    @staticmethod
    def _metrics_to_dict(metrics: SiteMetrics) -> dict:
        return {
            "total_count": metrics.total_count,
            "total_size_bytes": metrics.total_size_bytes,
            "official_count": metrics.official_count,
            "official_size_bytes": metrics.official_size_bytes,
            "error": metrics.error,
        }

    @staticmethod
    def _result_to_dict(result: CheckResult) -> dict:
        return {
            "site_name": result.rule.site_name,
            "status": result.status,
            "current_count": result.current_count,
            "current_size_bytes": result.current_size_bytes,
            "size_gap_bytes": result.size_gap_bytes,
            "count_gap": result.count_gap,
            "days_elapsed": result.days_elapsed,
            "days_remaining": result.days_remaining,
            "error": result.error,
        }

    def _save_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "queue_count": self._queue_count,
                "rules": self._rules_text,
            }
        )
"""MoviePilot V2 plugin for configurable multi-site seed workgroup checks."""

import re
import requests
from datetime import date, datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import Event
from app.core.event import eventmanager
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils

from .logic import CheckResult, SiteMetrics, evaluate_rule, format_report, parse_rules


lock = Lock()


class SeedWorkgroupCheck(_PluginBase):
    plugin_name = "保种工作组检查"
    plugin_desc = "按站点和工作组规则检查保种体积、数量和持续时间。"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/contract.png"
    plugin_version = "1.0.0"
    plugin_author = "OpenAI"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "seedworkcheck_"
    plugin_order = 2
    auth_level = 2

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "5 1 * * *"
    _queue_count = 5
    _rules_text = ""
    _scheduler: Optional[BackgroundScheduler] = None

    def __init__(self):
        super().__init__()
        self._rules = []
        self._metrics: Dict[str, SiteMetrics] = {}
        self._results: List[CheckResult] = []
        self._site_schema = []

    def init_plugin(self, config: dict = None):
        self.stop_service()
        config = config or {}
        self._enabled = bool(config.get("enabled", False))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce", False))
        self._cron = str(config.get("cron") or "5 1 * * *").strip()
        self._queue_count = max(1, int(config.get("queue_count") or 5))
        self._rules_text = str(config.get("rules") or "")
        try:
            self._rules = parse_rules(self._rules_text)
        except ValueError as exc:
            self._rules = []
            logger.error("保种工作组规则配置错误：%s", exc)

        self._metrics = self.get_data("metrics") or {}
        self._results = []
        if self._enabled or self._onlyonce:
            module_name = f"{self.__module__}.siteuserinfo"
            self._site_schema = ModuleHelper.load(
                module_name,
                filter_func=lambda _, obj: hasattr(obj, "schema"),
            )
            self._site_schema.sort(key=lambda item: item.order)
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    self.refresh_all_site_data,
                    "date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                )
                self._onlyonce = False
                self._save_config()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/seedwork_check",
                "event": EventType.PluginAction,
                "desc": "检查保种工作组",
                "category": "做种",
                "data": {"action": "seedwork_check"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        return [
            {
                "id": "SeedWorkgroupCheck",
                "name": "保种工作组每日检查",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.refresh_all_site_data,
                "kwargs": {},
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._field("VSwitch", "enabled", "启用插件", 4),
                            self._field("VSwitch", "notify", "发送每日通知", 4),
                            self._field("VSwitch", "onlyonce", "保存后立即检查", 4),
                            self._field("VTextField", "cron", "执行周期", 4, "5位 Cron，例如 5 1 * * *"),
                            self._field("VTextField", "queue_count", "并发请求数", 4, "默认 5"),
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rules",
                                            "label": "站点规则",
                                            "rows": 10,
                                            "placeholder": "站点|是否官种|官组关键词|体积GB|数量|周期天数|开始日期",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "每行一条规则，字段用 | 分隔；官组关键词用英文逗号分隔。例：NovaHD|是|NovaHD,官方组|5|2|365|2025/08/24。非官种规则的关键词留空，数量填 0 表示不检查数量。",
                                        },
                                    }
                                ],
                            },
                        ],
                    }
                ],
            }
        ], {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cron": "5 1 * * *",
            "queue_count": 5,
            "rules": "",
        }

    @staticmethod
    def _field(component: str, model: str, label: str, md: int, placeholder: str = "") -> dict:
        props = {"model": model, "label": label}
        if placeholder:
            props["placeholder"] = placeholder
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [{"component": component, "props": props}],
        }

    def get_page(self) -> List[dict]:
        if not self._results:
            return [{"component": "VAlert", "props": {"type": "info", "text": "暂无检查结果，请先保存配置或执行一次检查。"}}]
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": self._status_type(result.status),
                    "variant": "tonal",
                    "text": self._result_text(result),
                },
            }
            for result in self._results
        ]

    def get_dashboard(self, key: str = "", **kwargs):
        return {"cols": 12}, {"title": "保种工作组检查", "refresh": 300}, self.get_page()

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as exc:
            logger.error("停止保种工作组检查服务失败：%s", exc)

    @eventmanager.register(EventType.PluginAction)
    def refresh(self, event: Event):
        if event and (event.event_data or {}).get("action") != "seedwork_check":
            return
        self.refresh_all_site_data()
        if event:
            self.post_message(
                channel=event.event_data.get("channel"),
                title="保种工作组检查完成",
                userid=event.event_data.get("user"),
            )

    def refresh_all_site_data(self):
        if not self._rules:
            logger.warning("保种工作组检查未配置站点规则")
            return
        with lock:
            self._metrics = {}
            sites = [site for site in SitesHelper().get_indexers() if not site.get("public")]
            site_map = {str(site.get("name")): site for site in sites}
            unique_names = sorted({rule.site_name for rule in self._rules})
            targets = [site_map[name] for name in unique_names if name in site_map]
            for missing in [name for name in unique_names if name not in site_map]:
                self._metrics[missing] = SiteMetrics(error="MoviePilot 未找到该站点，名称需与站点列表完全一致")
            if targets:
                with ThreadPool(min(len(targets), self._queue_count)) as pool:
                    pool.map(self._refresh_one_site, targets)
            self._results = [
                evaluate_rule(rule, self._metrics.get(rule.site_name, SiteMetrics(error="未获取到站点数据")))
                for rule in self._rules
            ]
            self.save_data("metrics", {name: self._metrics_to_dict(metrics) for name, metrics in self._metrics.items()})
            self.save_data("results", [self._result_to_dict(result) for result in self._results])
            if self._notify:
                self.post_message(mtype=NotificationType.SiteMessage, title="保种工作组每日检查", text=format_report(self._results))

    def _refresh_one_site(self, site_info: dict):
        name = str(site_info.get("name"))
        site_rules = [rule for rule in self._rules if rule.site_name == name]
        try:
            site_user_info = self._build(site_info)
            if not site_user_info:
                self._metrics[name] = SiteMetrics(error="站点页面无法解析或 Cookie 已失效")
                return
            keywords = sorted({keyword for rule in site_rules for keyword in rule.official_keywords})
            site_user_info.official_team[name] = keywords
            site_user_info.parse_official_seeding_info()
            self._metrics[name] = SiteMetrics(
                total_count=int(site_user_info.total_seeding_size[0] or 0),
                total_size_bytes=int(site_user_info.total_seeding_size[1] or 0),
                official_count=int(site_user_info.official_seeding_size[0] or 0),
                official_size_bytes=int(site_user_info.official_seeding_size[1] or 0),
                error=site_user_info.err_msg,
            )
        except Exception as exc:
            logger.error("站点 %s 保种数据获取失败：%s", name, exc, exc_info=True)
            self._metrics[name] = SiteMetrics(error=str(exc))

    def _build(self, site_info: dict):
        site_cookie = site_info.get("cookie")
        url = site_info.get("url")
        if not site_cookie or not url:
            return None
        site_name = str(site_info.get("name"))
        ua = site_info.get("ua")
        proxy = site_info.get("proxy")
        proxies = settings.PROXY if proxy else None
        with requests.Session() as session:
            response = RequestUtils(cookies=site_cookie, session=session, ua=ua, proxies=proxies).get_res(url=url)
            if not response or response.status_code != 200:
                return None
            response.encoding = "utf-8" if re.search(r"charset=\"?utf-8", response.text, re.I) else response.apparent_encoding
            html_text = response.text
            if not html_text:
                return None
            for schema in self._site_schema:
                if schema.match(html_text):
                    return schema(site_name, url, site_cookie, html_text, session=session, ua=ua, proxy=proxy)
        return None

    @staticmethod
    def _status_type(status: str) -> str:
        return {"已完成": "success", "保种达标": "info", "未配置官组": "warning", "获取失败": "error"}.get(status, "warning")

    @staticmethod
    def _result_text(result: CheckResult) -> str:
        rule = result.rule
        kind = "官种" if rule.official else "总做种"
        return f"{rule.site_name} | {result.status} | {kind} {result.current_count} 个 / {rule.min_count} 个 | 剩余 {result.days_remaining} 天"

    @staticmethod
    def _metrics_to_dict(metrics: SiteMetrics) -> dict:
        return {
            "total_count": metrics.total_count,
            "total_size_bytes": metrics.total_size_bytes,
            "official_count": metrics.official_count,
            "official_size_bytes": metrics.official_size_bytes,
            "error": metrics.error,
        }

    @staticmethod
    def _result_to_dict(result: CheckResult) -> dict:
        return {
            "site_name": result.rule.site_name,
            "status": result.status,
            "current_count": result.current_count,
            "current_size_bytes": result.current_size_bytes,
            "size_gap_bytes": result.size_gap_bytes,
            "count_gap": result.count_gap,
            "days_elapsed": result.days_elapsed,
            "days_remaining": result.days_remaining,
            "error": result.error,
        }

    def _save_config(self):
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "cron": self._cron,
                "queue_count": self._queue_count,
                "rules": self._rules_text,
            }
        )
