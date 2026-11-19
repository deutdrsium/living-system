"""Dashboard-only account setup for the selected private role."""

import json

from astrbot.api.web import request
from companion_v2.execution.models import TaskError
from companion_v2.foundation.outcome import Outcome

from .life_api import LifeWebAPI


PREFIX = "/astrbot_plugin_private_companion/social"


class SocialWebAPI:
    def __init__(self, controller):
        self.controller = controller
        self.life = LifeWebAPI(controller)

    def routes(self):
        return [(PREFIX, self.status, ["GET"], "Read private social account status"),
                (PREFIX + "/command", self.command, ["POST"], "Configure private social accounts")]

    def _authorized(self):
        manager, operator = self.life.manager()
        manager.authorize(operator)
        return manager, manager.app.social_port

    async def status(self):
        from .web_api import outcome_response
        try:
            if request.query:
                raise ValueError("unexpected query")
            async with self.controller._control_lock:
                manager, port = self._authorized()
                owner_key = port.context_key(manager.context)
                port.check_context(owner_key)
                result = {"enabled": port.enabled_for(manager.context), "configured": port.configured(),
                    "verification": port.verification(),
                    "zhihu_session": port.zhihu_session_info(),
                    "google_zone": port.settings().get("google_zone", "mcp_serp"),
                    "blocked": [name for name in port.configured() if port.blocked_for(owner_key, name)],
                    "capabilities": {
                        "bilibili": "推荐、关注动态、搜索、看视频、点赞、收藏、关注",
                        "xiaohongshu": "推荐、搜索、笔记、点赞、收藏；暂不支持关注",
                        "zhihu": "首页、正文、赞同、收藏到默认收藏夹；搜索另需知乎开放平台授权，暂不支持关注",
                        "douban": "图书和电影搜索、详情、榜单及想看标记；暂不支持关注流或关注",
                        "brightdata": "Google 搜索和网页正文"}}
            return outcome_response(Outcome.success(result))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError):
            return outcome_response(Outcome.failed("social_request_invalid", "invalid account request"))

    async def command(self):
        from .web_api import outcome_response
        try:
            if request.query:
                raise ValueError("unexpected query")
            raw = await request.body()
            if len(raw) > 131072:
                raise ValueError("request too large")
            data = json.loads(raw.decode("utf-8"))
            if type(data) is not dict:
                raise ValueError("invalid command")
            action = data.get("action")
            allowed = {"enabled": {"action", "enabled"}, "credential": {"action", "platform", "value"},
                       "resume": {"action", "platform"},
                       "zhihu_search": {"action", "value"},
                       "zhihu_cookie": {"action", "value"},
                       "zhihu_cookie_ua": {"action", "value", "userAgent"},
                       "zhihu_session": {"action", "storageState", "userAgent"},
                       "google_zone": {"action", "value"},
                       "login": {"action", "platform"}, "check": {"action", "platform"}}
            if action not in allowed or set(data) != allowed[action]:
                raise ValueError("invalid command fields")
            async with self.controller._control_lock:
                manager, port = self._authorized()
                owner_key = port.context_key(manager.context)
                port.bind_context(owner_key)
                if action == "enabled":
                    port.set_enabled(owner_key, data["enabled"])
                    return outcome_response(Outcome.success({"enabled": data["enabled"]}))
                if action == "credential":
                    port.save_credential(data["platform"], data["value"])
                    return outcome_response(Outcome.success({"saved": True}))
                if action == "resume":
                    port.block_platform(owner_key, data["platform"], False)
                    return outcome_response(Outcome.success({"resumed": True}))
                if action == "google_zone":
                    port.set_google_zone(owner_key, data["value"])
                    return outcome_response(Outcome.success({"saved": True}))
            if action == "login":
                result = await port.login(data["platform"])
            elif action == "zhihu_search":
                await port.save_zhihu_search(data["value"])
                port.record_verification("zhihu_search", "passed")
                result = {"saved": True, "verified": True}
            elif action == "zhihu_cookie":
                result = await port.save_zhihu_cookie(data["value"])
                if result.get("ok"):
                    port.record_verification("zhihu", "passed")
            elif action == "zhihu_cookie_ua":
                result = await port.save_zhihu_cookie(data["value"], data["userAgent"])
                if result.get("ok"):
                    port.record_verification("zhihu", "passed")
            elif action == "zhihu_session":
                result = await port.save_zhihu_session(data["storageState"], data["userAgent"])
                if result.get("ok"):
                    port.record_verification("zhihu", "passed")
            else:
                try:
                    result = await port.login_status(data["platform"])
                except Exception:
                    port.record_verification(data["platform"], "unavailable")
                    raise
                if result.get("record_verification") is not False:
                    port.record_verification(data["platform"], "passed" if result.get("logged_in") else "failed")
            return outcome_response(Outcome.success(result))
        except TaskError as error:
            return outcome_response(Outcome.failed(error.code, str(error)))
        except (ValueError, TypeError, UnicodeError):
            return outcome_response(Outcome.failed("social_request_invalid", "invalid account request"))
        except Exception:
            return outcome_response(Outcome.failed("social_service_unavailable", "平台服务暂不可用，请稍后查看状态"))
