"""自建后端（Elysia）传输层 + 公开读接口。

只覆盖无需登录的 Query：评论 / 构筑 / 攻略 / 时间轴 / 榜单 / 脚本 /
MOD / DPS / gameData*。Mutation、admin、shop 私有、my* 一律不实现。
标准库 urllib 实现零额外 HTTP 依赖，302 默认跟随。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .errors import DobGraphQLError, DobHttpError

_DEFAULT_TIMEOUT = 30


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, dict | list, str]:
    """POST JSON 并返回 (status, body, final_url)，302 由 opener 自动跟随。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8")), resp.geturl()
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"message": str(exc)}
        raise DobHttpError(f"HTTP {exc.code}: {url}", status=exc.code, payload=body) from exc
    except urllib.error.URLError as exc:
        raise DobHttpError(f"连接失败: {url} ({exc.reason})", status=0) from exc


def _get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise DobHttpError(f"HTTP {exc.code}: {url}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise DobHttpError(f"连接失败: {url} ({exc.reason})", status=0) from exc


class BackendClient:
    """自建后端客户端，默认线上端点；本地联调传 http://localhost:8887。"""

    def __init__(self, base_url: str = "https://api.dna-builder.cn", timeout: float = _DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def graphql_url(self) -> str:
        return f"{self.base_url}/graphql"

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        """裸 GraphQL 调用，errors 非空时抛 DobGraphQLError。"""
        _, body, _ = _post_json(self.graphql_url, {"query": query, "variables": variables or {}}, self.timeout)
        if not isinstance(body, dict):
            raise DobGraphQLError(f"网关返回非对象: {body!r}", errors=[body])
        if body.get("errors"):
            first = body["errors"][0]
            msg = first.get("message", "graphql error") if isinstance(first, dict) else str(first)
            raise DobGraphQLError(msg, errors=body["errors"])
        return body.get("data") or {}

    # ---- 评论（读） ----
    def comments(self, target_id: str, limit: int = 50, offset: int = 0) -> list:
        data = self.graphql(
            "query($t:String!,$l:Int,$o:Int){comments(targetId:$t,limit:$l,offset:$o){id targetId content createdAt user{id name}}}",
            {"t": target_id, "l": limit, "o": offset},
        )
        return data.get("comments") or []

    def comments_count(self, target_id: str) -> int:
        data = self.graphql("query($t:String!){commentsCount(targetId:$t)}", {"t": target_id})
        return int(data.get("commentsCount") or 0)

    # ---- 构筑（读） ----
    def builds(self, search=None, char_id=None, user_id=None, limit=20, offset=0, sort_by="latest") -> list:
        data = self.graphql(
            "query($s:String,$c:Int,$u:String,$l:Int,$o:Int,$sb:String)"
            "{builds(search:$s,charId:$c,userId:$u,limit:$l,offset:$o,sortBy:$sb)"
            "{id title desc charId userId views likes isRecommended isPinned createdAt updateAt targetValue charSettings}}",
            {"s": search, "c": char_id, "u": user_id, "l": limit, "o": offset, "sb": sort_by},
        )
        return data.get("builds") or []

    def builds_count(self, search=None, char_id=None) -> int:
        data = self.graphql(
            "query($s:String,$c:Int){buildsCount(search:$s,charId:$c)}", {"s": search, "c": char_id}
        )
        return int(data.get("buildsCount") or 0)

    def build(self, build_id: str) -> dict | None:
        data = self.graphql(
            "query($id:String!){build(id:$id){id title desc charId charSettings userId views likes createdAt updateAt targetValue}}",
            {"id": build_id},
        )
        return data.get("build")

    def recommended_builds(self, limit=10) -> list:
        return (self.graphql("query($l:Int){recommendedBuilds(limit:$l){id title charId likes views}}", {"l": limit}).get("recommendedBuilds") or [])

    def trending_builds(self, limit=10) -> list:
        return (self.graphql("query($l:Int){trendingBuilds(limit:$l){id title charId likes views}}", {"l": limit}).get("trendingBuilds") or [])

    # ---- 攻略（读） ----
    def guides(self, search=None, type=None, char_id=None, user_id=None, limit=20, offset=0) -> list:
        data = self.graphql(
            "query($s:String,$t:String,$c:Int,$u:String,$l:Int,$o:Int)"
            "{guides(search:$s,type:$t,charId:$c,userId:$u,limit:$l,offset:$o){id title type charId views likes createdAt updateAt}}",
            {"s": search, "t": type, "c": char_id, "u": user_id, "l": limit, "o": offset},
        )
        return data.get("guides") or []

    def guide(self, guide_id: str) -> dict | None:
        data = self.graphql(
            "query($id:String!){guide(id:$id){id title type content images charId buildId views likes createdAt updateAt}}",
            {"id": guide_id},
        )
        return data.get("guide")

    # ---- 时间轴（读） ----
    def timelines(self, search=None, char_id=None, user_id=None, limit=20, offset=0, sort_by="recent") -> list:
        data = self.graphql(
            "query($s:String,$c:Int,$u:String,$l:Int,$o:Int,$sb:String)"
            "{timelines(search:$s,charId:$c,userId:$u,limit:$l,offset:$o,sortBy:$sb){id title charId views likes createdAt updateAt}}",
            {"s": search, "c": char_id, "u": user_id, "l": limit, "o": offset, "sb": sort_by},
        )
        return data.get("timelines") or []

    def timeline(self, timeline_id: str) -> dict | None:
        data = self.graphql(
            "query($id:String!){timeline(id:$id){id title charId views likes createdAt updateAt}}",
            {"id": timeline_id},
        )
        return data.get("timeline")

    # ---- 榜单（读） ----
    def ranking_lists(self) -> list:
        return (self.graphql("{rankingLists{id name desc createdAt updateAt}}").get("rankingLists") or [])

    def ranking_list(self, list_id: str) -> dict | None:
        data = self.graphql(
            "query($id:String!){rankingList(id:$id){id name desc items{id charId buildId sortOrder}}}",
            {"id": list_id},
        )
        return data.get("rankingList")

    def ranking_list_items(self, ranking_list_id: str) -> list:
        data = self.graphql(
            "query($id:String!){rankingListItems(rankingListId:$id){id charId buildId sortOrder}}",
            {"id": ranking_list_id},
        )
        return data.get("rankingListItems") or []

    # ---- 脚本 / MOD / DPS（读） ----
    def scripts(self, search=None, category=None, user_id=None, limit=20, offset=0) -> list:
        data = self.graphql(
            "query($s:String,$c:String,$u:String,$l:Int,$o:Int)"
            "{scripts(search:$s,category:$c,userId:$u,limit:$l,offset:$o){id title category createdAt updateAt}}",
            {"s": search, "c": category, "u": user_id, "l": limit, "o": offset},
        )
        return data.get("scripts") or []

    def script(self, script_id: str, preview: bool = False) -> dict | None:
        data = self.graphql(
            "query($id:String!,$p:Boolean){script(id:$id,preview:$p){id title category content}}",
            {"id": script_id, "p": preview},
        )
        return data.get("script")

    def game_mods(self, search=None, category=None, entity=None, limit=20, offset=0, sort_by=None) -> list:
        data = self.graphql(
            "query($s:String,$c:String,$e:String,$l:Int,$o:Int,$sb:String)"
            "{gameMods(search:$s,category:$c,entity:$e,limit:$l,offset:$o,sortBy:$sb){id title category entity status createdAt updateAt}}",
            {"s": search, "c": category, "e": entity, "l": limit, "o": offset, "sb": sort_by},
        )
        return data.get("gameMods") or []

    def game_mod(self, mod_id: str) -> dict | None:
        data = self.graphql(
            "query($id:String!){gameMod(id:$id){id title category entity status}}", {"id": mod_id}
        )
        return data.get("gameMod")

    def dps_list(self, char_id=None, build_id=None, timeline_id=None, limit=20, offset=0, sort_by="dpsValue") -> list:
        data = self.graphql(
            "query($c:Int,$b:String,$t:String,$l:Int,$o:Int,$sb:String)"
            "{dpsList(charId:$c,buildId:$b,timelineId:$t,limit:$l,offset:$o,sortBy:$sb){id charId buildId dpsValue createdAt}}",
            {"c": char_id, "b": build_id, "t": timeline_id, "l": limit, "o": offset, "sb": sort_by},
        )
        return data.get("dpsList") or []
