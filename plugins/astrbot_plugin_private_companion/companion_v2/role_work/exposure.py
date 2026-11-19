"""Real recommendation exposures and grounded reactions for private browsing."""
from datetime import datetime
import json
import math
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest
from .interests import scope_key

FEED_OPERATIONS = {"bilibili": "bili_recommend", "xiaohongshu": "xhs_recommend", "zhihu": "zhihu_home"}
READ_OPERATIONS = {"bilibili": "bili_watch", "xiaohongshu": "xhs_read", "zhihu": "zhihu_read"}


def _plain(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def _safe_url(value):
    try:
        u = urlsplit(_plain(value, 500))
        if u.scheme == "https" and u.hostname and not u.username and not u.password and not u.port:
            return urlunsplit((u.scheme, u.netloc, u.path, "", ""))[:400]
    except ValueError:
        pass
    return ""


def feed_items(platform, payload):
    """Only use cards actually returned by a supported recommendation endpoint."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return []
    cards = data.get("feeds" if platform == "xiaohongshu" else "items", [])
    if not isinstance(cards, list):
        return []
    items, seen = [], set()
    for card in cards[:100]:
        if not isinstance(card, dict):
            continue
        note = card.get("noteCard") if isinstance(card.get("noteCard"), dict) else {}
        if platform == "bilibili":
            ident = _plain(card.get("bvid"), 32)
            if not re.fullmatch(r"BV[0-9A-Za-z]{10}", ident):
                continue
            title, summary, url, token = card.get("title"), card.get("desc"), card.get("url"), ""
        elif platform == "xiaohongshu":
            ident = _plain(card.get("id"), 80)
            if not re.fullmatch(r"[0-9A-Za-z_-]{8,80}", ident):
                continue
            title, summary, url, token = note.get("displayTitle"), note.get("desc"), "", _plain(card.get("xsecToken"), 1024)
        elif platform == "zhihu":
            ident = _safe_url(card.get("url"))
            if not re.fullmatch(r"https://www\.zhihu\.com/question/[1-9][0-9]*/answer/[1-9][0-9]*", ident):
                continue
            title, summary, url, token = card.get("title"), card.get("summary"), ident, ""
        else:
            return []
        title = _plain(title, 200)
        if not title or ident in seen:
            continue
        seen.add(ident)
        tags = card.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [_plain(x, 50) for x in tags if isinstance(x, str) and _plain(x, 50)][:2]
        items.append({"platform": platform, "id": ident, "title": title, "summary": _plain(summary, 500),
                      "url": _safe_url(url), "tags": tags, "_token": token})
    return items


def public_item(item):
    return {key: item[key] for key in ("platform", "id", "title", "summary", "url", "tags")}


def familiarity(rows, now, coefficient=1.0, timezone="Asia/Shanghai"):
    """Deduplicate a card each local day and accept at most three cards per topic/day."""
    zone, per_day, seen, points = ZoneInfo(timezone), {}, set(), 0.0
    previous = None
    for row in sorted(rows, key=lambda x: (x["exposed_at"], x["platform"], x["content_id"])):
        when = datetime.fromisoformat(row["exposed_at"])
        if when > now:
            continue
        day = when.astimezone(zone).date()
        dedupe = (day, row["platform"], row["content_id"])
        if dedupe in seen or per_day.get(day, 0) >= 3:
            continue
        seen.add(dedupe)
        per_day[day] = per_day.get(day, 0) + 1
        if previous:
            points *= 2 ** (-(when - previous).total_seconds() / (14 * 86400))
        points = min(10.0, points + (coefficient if coefficient in (0.8, 1.0, 1.2) else 1.0))
        previous = when
    if previous:
        points *= 2 ** (-(now - previous).total_seconds() / (14 * 86400))
    return round(points, 3), len(seen), len({(r[1], r[2]) for r in seen})


class ExposureStore:
    def __init__(self, runtime, visible=None, session_reader=None, life=None):
        self.runtime = runtime
        self.visible = visible or (lambda context, activity_id: True)
        self.session_reader = session_reader
        self.life = life

    def life_reader(self):
        reader = self.session_reader
        owner = getattr(self.life, "activity_owner", None)
        active = reader is not None and (not hasattr(reader, "active") or reader.active())
        if callable(owner) and owner() == "good_life" and not active:
            raise TaskError("life_provider_unavailable", "browsing life provider is unavailable")
        return reader if active else None

    def session_platform(self, context, activity_id):
        if self.life_reader() is not None:
            return self.life_reader().session_platform(context, activity_id)
        row = self.runtime.read_one("SELECT platform FROM role_exposure_sessions WHERE scope_key=? AND activity_id=?",
                                    (scope_key(context), activity_id))
        return row["platform"] if row else None

    def previous_platform(self, context, activity_id):
        if self.life_reader() is not None:
            return self.life_reader().previous_platform(context, activity_id)
        row = self.runtime.read_one("""SELECT platform FROM role_exposure_sessions WHERE scope_key=? AND activity_id<>?
            AND platform<>'unavailable' ORDER BY rowid DESC LIMIT 1""", (scope_key(context), activity_id))
        return row["platform"] if row else None

    def save_batch(self, context, activity_id, platform, items, now):
        key = scope_key(context)
        reader = self.life_reader()
        if reader is not None:
            reader.authorize_write(context, activity_id)
            # Interest and feedback tables keep their host foreign keys, but the
            # content and its exposure state belong only to the life store.
            with self.runtime.transaction() as con:
                for item in items:
                    con.execute("""INSERT OR IGNORE INTO role_exposures
                        (scope_key,activity_id,platform,content_id,title,summary,source_url,fetched_at)
                        VALUES (?,?,?,?, '', '', '', ?)""", (key, activity_id, platform, item["id"], now.isoformat()))
            cards = [{name: item[name] for name in ("id", "title", "summary", "url")} for item in items]
            reader.command(context, activity_id, "save_batch", platform=platform, items=cards, observed_at=now.isoformat())
            return
        with self.runtime.transaction() as con:
            con.execute("INSERT OR IGNORE INTO role_exposure_sessions(scope_key, activity_id, platform) VALUES (?, ?, ?)",
                        (key, activity_id, platform))
            for item in items:
                con.execute("""INSERT OR IGNORE INTO role_exposures
                    (scope_key,activity_id,platform,content_id,title,summary,source_url,fetched_at)
                    VALUES (?,?,?,?,?,?,?,?)""", (key, activity_id, platform, item["id"], item["title"],
                                                  item["summary"], item["url"], now.isoformat()))

    def selected(self, context, activity_id, choice):
        reader = self.life_reader()
        if reader is not None:
            selected = reader.command(context, activity_id, "selected", choice=choice or "")
            if type(selected) is not str:
                raise TaskError("life_provider_data_invalid", "browsing selection is invalid")
            return selected
        key = scope_key(context)
        with self.runtime.transaction() as con:
            row = con.execute("SELECT selected_id, selection_made FROM role_exposure_sessions WHERE scope_key=? AND activity_id=?",
                              (key, activity_id)).fetchone()
            if row is None:
                raise ValueError("missing recommendation session")
            if not row["selection_made"]:
                con.execute("UPDATE role_exposure_sessions SET selected_id=?,selection_made=1 WHERE scope_key=? AND activity_id=?",
                            (choice or "", key, activity_id))
                return choice or ""
            return row["selected_id"]

    def mark_exposed(self, context, activity_id, platform, ids, now, *, skipped=()):
        reader = self.life_reader()
        if reader is not None:
            reader.authorize_write(context, activity_id)
            pending = [ident for ident in ids if (item := reader.exposure(context, activity_id, platform, ident))
                       and item["exposed_at"] is None]
            with self.runtime.transaction() as con:
                topics = set()
                for ident in pending:
                    topics.update(row["interest_id"] for row in con.execute("""SELECT interest_id FROM role_exposure_topics
                        WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?""",
                        (scope_key(context), activity_id, platform, ident)).fetchall())
                for topic in topics:
                    con.execute("UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id=?",
                                (scope_key(context), topic))
            reader.command(context, activity_id, "mark_exposed", platform=platform, ids=list(ids),
                           skipped=list(skipped), observed_at=now.isoformat())
            return
        with self.runtime.transaction() as con:
            changed_topics = set()
            for ident in ids:
                changed = con.execute("""UPDATE role_exposures SET exposed_at=?,
                    coverage=CASE WHEN ? AND attempted_at IS NULL AND coverage='' THEN 'skipped' ELSE coverage END
                    WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=? AND exposed_at IS NULL""",
                    (now.isoformat(), int(ident in skipped), scope_key(context), activity_id, platform, ident))
                if changed.rowcount:
                    changed_topics.update(r["interest_id"] for r in con.execute("""SELECT interest_id FROM role_exposure_topics
                        WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?""",
                        (scope_key(context), activity_id, platform, ident)).fetchall())
            for topic in changed_topics:
                con.execute("UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id=?", (scope_key(context),topic))

    def mark_attempt(self, context, activity_id, platform, ident, coverage, now, *, random_attempt=False):
        if coverage not in ("text", "image", "image_description"):
            return
        reader = self.life_reader()
        if reader is not None:
            reader.authorize_write(context, activity_id)
            item = reader.exposure(context, activity_id, platform, ident)
            if item and item["attempted_at"] is None:
                with self.runtime.transaction() as con:
                    con.execute("""UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id IN
                        (SELECT interest_id FROM role_exposure_topics WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?)""",
                        (scope_key(context), scope_key(context), activity_id, platform, ident))
            reader.command(context, activity_id, "mark_attempt", platform=platform, ident=ident, coverage=coverage,
                           observed_at=now.isoformat(), random_attempt=bool(random_attempt))
            return
        with self.runtime.transaction() as con:
            changed = con.execute("""UPDATE role_exposures SET attempted_at=COALESCE(attempted_at,?), coverage=?,
                random_attempt=MAX(random_attempt,?) WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=? AND attempted_at IS NULL""",
                (now.isoformat(), coverage, int(random_attempt), scope_key(context), activity_id, platform, ident))
            if changed.rowcount:
                con.execute("""UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id IN
                    (SELECT interest_id FROM role_exposure_topics WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?)""",
                    (scope_key(context),scope_key(context),activity_id,platform,ident))

    def topics(self, context, activity_id, platform, mapping):
        key = scope_key(context)
        reader = self.life_reader()
        prior_by_id = {}
        if reader is not None:
            reader.authorize_write(context, activity_id)
            for ident in mapping:
                prior = self.runtime.read_all("""SELECT activity_id,interest_id FROM role_exposure_topics
                    WHERE scope_key=? AND platform=? AND content_id=? AND activity_id<>?""",
                    (key, platform, ident, activity_id))
                prior_by_id[ident] = [row for row in prior if self.visible(context, row["activity_id"])]
        with self.runtime.transaction() as con:
            for ident, names in mapping.items():
                if not con.execute("SELECT 1 FROM role_exposures WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?",
                                   (key, activity_id, platform, ident)).fetchone():
                    continue
                prior = (prior_by_id[ident] if reader is not None else con.execute("""SELECT t.activity_id,t.interest_id FROM role_exposure_topics t
                    WHERE t.scope_key=? AND t.platform=? AND t.content_id=? AND t.activity_id<>?""",
                    (key, platform, ident, activity_id)).fetchall())
                previous_topics = []
                for row in prior:
                    if (reader is not None or self.visible(context, row["activity_id"])) and row["interest_id"] not in previous_topics:
                        previous_topics.append(row["interest_id"])
                    if len(previous_topics) == 2:
                        break
                for topic in previous_topics:
                    con.execute("INSERT OR IGNORE INTO role_exposure_topics VALUES (?,?,?,?,?)",
                                (key, activity_id, platform, ident, topic))
                current = con.execute("""SELECT COUNT(*) AS n FROM role_exposure_topics WHERE scope_key=?
                    AND activity_id=? AND platform=? AND content_id=?""", (key,activity_id,platform,ident)).fetchone()["n"]
                for raw in names[:2]:
                    if current >= 2:
                        break
                    if not isinstance(raw, str):
                        continue
                    name = unicodedata.normalize("NFKC", raw).strip()[:50]
                    if not name:
                        continue
                    rows = con.execute("SELECT interest_id,name,aliases_json FROM role_interests WHERE scope_key=?", (key,)).fetchall()
                    match = next((r for r in rows if name.casefold() in
                        {unicodedata.normalize("NFKC", n).casefold() for n in [r["name"], *json.loads(r["aliases_json"])]}), None)
                    identifier = match["interest_id"] if match else digest([key, name.casefold()])
                    con.execute("""INSERT OR IGNORE INTO role_interests
                        (scope_key,interest_id,name,aliases_json,summary,next_question,paused,revision)
                        VALUES (?,?,?,'[]','','',0,1)""", (key, identifier, name))
                    con.execute("INSERT OR IGNORE INTO role_interest_coefficients VALUES (?,?,1.0,0)", (key, identifier))
                    added = con.execute("INSERT OR IGNORE INTO role_exposure_topics VALUES (?,?,?,?,?)",
                                        (key, activity_id, platform, ident, identifier))
                    if added.rowcount:
                        current += 1
                        con.execute("UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id=?", (key, identifier))

    def topic_rows(self, context, interest_id):
        if self.life_reader() is not None:
            references = self.runtime.read_all("""SELECT activity_id,platform,content_id FROM role_exposure_topics
                WHERE scope_key=? AND interest_id=?""", (scope_key(context), interest_id))
            rows = [self.life_reader().exposure(context, row["activity_id"], row["platform"], row["content_id"])
                    for row in references]
            return sorted((row for row in rows if row and row["exposed_at"] and self.visible(context, row["activity_id"])),
                          key=lambda row: row["exposed_at"])
        rows = self.runtime.read_all("""SELECT e.* FROM role_exposures e JOIN role_exposure_topics t
            ON t.scope_key=e.scope_key AND t.activity_id=e.activity_id AND t.platform=e.platform AND t.content_id=e.content_id
            WHERE t.scope_key=? AND t.interest_id=? AND e.exposed_at IS NOT NULL ORDER BY e.exposed_at""", (scope_key(context), interest_id))
        return [r for r in rows if self.visible(context, r["activity_id"])]

    def blocked(self, context, platform, ident, topic_ids=()):
        key = scope_key(context)
        targets = [("item", platform + ":" + ident)] + [("topic", topic) for topic in topic_ids]
        for kind, target in targets:
            rows = self.runtime.read_all("""SELECT activity_id FROM role_exposure_feedback WHERE scope_key=?
                AND target_kind=? AND target_id=? AND reaction='negative' AND active=1""", (key, kind, target))
            if any(self.visible(context, row["activity_id"]) for row in rows):
                return True
        return False

    def prior_topics(self, context, platform, ident):
        rows = self.runtime.read_all("""SELECT DISTINCT t.activity_id,t.interest_id FROM role_exposure_topics t
            WHERE t.scope_key=? AND t.platform=? AND t.content_id=?""", (scope_key(context),platform,ident))
        return [r["interest_id"] for r in rows if self.visible(context,r["activity_id"])]

    def already_attempted(self, context, platform, ident):
        if self.life_reader() is not None:
            return self.life_reader().attempted(context, platform, ident)
        rows = self.runtime.read_all("""SELECT activity_id FROM role_exposures WHERE scope_key=? AND platform=?
            AND content_id=? AND attempted_at IS NOT NULL""", (scope_key(context), platform, ident))
        return any(self.visible(context, row["activity_id"]) for row in rows)

    def exposure(self, context, activity_id, platform, content_id):
        if self.life_reader() is not None:
            return self.life_reader().exposure(context, activity_id, platform, content_id)
        return self.runtime.read_one("""SELECT * FROM role_exposures WHERE scope_key=? AND activity_id=?
            AND platform=? AND content_id=?""", (scope_key(context), activity_id, platform, content_id))

    def recent(self, context, *, limit=100):
        if self.life_reader() is not None:
            return self.life_reader().recent(context, limit=limit)
        return self.runtime.read_all("SELECT * FROM role_exposures WHERE scope_key=? ORDER BY fetched_at DESC LIMIT ?",
                                     (scope_key(context), limit))

    def feedback(self, context, activity_id, platform, feedback, now):
        key = scope_key(context)
        reader = self.life_reader()
        life_exposures = {}
        if reader is not None:
            reader.authorize_write(context, activity_id)
            for item in feedback if isinstance(feedback, list) else ():
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    ident = item["id"]
                    life_exposures[ident] = reader.exposure(context, activity_id, platform, ident)
        with self.runtime.transaction() as con:
            for item in feedback if isinstance(feedback, list) else ():
                if not isinstance(item, dict):
                    continue
                ident, reaction, kind = item.get("id"), item.get("reaction"), item.get("target")
                if not isinstance(ident, str) or reaction not in ("positive", "negative", "neutral", "unknown") or kind not in ("item", "topic"):
                    continue
                exp = (life_exposures.get(ident) if reader is not None else con.execute(
                    "SELECT * FROM role_exposures WHERE scope_key=? AND activity_id=? AND platform=? AND content_id=?",
                    (key, activity_id, platform, ident)).fetchone())
                if not exp or not exp["attempted_at"] or not exp["exposed_at"]:
                    continue
                target = platform + ":" + ident
                if kind == "topic":
                    topic = _plain(item.get("topic"), 50)
                    found = con.execute("""SELECT i.interest_id FROM role_interests i JOIN role_exposure_topics t
                        ON i.scope_key=t.scope_key AND i.interest_id=t.interest_id
                        WHERE t.scope_key=? AND t.activity_id=? AND t.platform=? AND t.content_id=? AND i.name=?""",
                        (key, activity_id, platform, ident, topic)).fetchone()
                    if not found:
                        continue
                    target = found["interest_id"]
                wants = item.get("wants_continue") is True
                reason = _plain(item.get("reason"), 200)
                if reaction == "positive" and not exp["random_attempt"]:
                    con.execute("UPDATE role_exposure_feedback SET active=0 WHERE scope_key=? AND target_kind=? AND target_id=? AND reaction='negative'", (key, kind, target))
                changed = con.execute("""INSERT INTO role_exposure_feedback VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
                    ON CONFLICT(scope_key,activity_id,platform,content_id,target_kind,target_id) DO UPDATE SET
                        reaction=excluded.reaction,wants_continue=excluded.wants_continue,reason=excluded.reason,
                        occurred_at=excluded.occurred_at,autonomous=excluded.autonomous,active=1
                    WHERE role_exposure_feedback.reaction<>excluded.reaction
                        OR role_exposure_feedback.wants_continue<>excluded.wants_continue
                        OR role_exposure_feedback.reason<>excluded.reason""",
                    (key, activity_id, platform, ident, kind, target, reaction, int(wants), reason,
                     now.isoformat(), int(not exp["random_attempt"])))
                if kind == "topic" and changed.rowcount:
                    con.execute("UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND interest_id=?", (key, target))

    def assess(self, context, assessments):
        with self.runtime.transaction() as con:
            for name, coefficient in assessments.items() if isinstance(assessments, dict) else ():
                if type(coefficient) not in (int, float) or coefficient not in (0.8, 1.0, 1.2):
                    continue
                changed = con.execute("""UPDATE role_interest_coefficients SET coefficient=?,assessed=1 WHERE scope_key=? AND assessed=0
                    AND interest_id IN (SELECT interest_id FROM role_interests WHERE scope_key=? AND name=?)""",
                    (coefficient, scope_key(context), scope_key(context), name))
                if changed.rowcount:
                    con.execute("UPDATE role_interests SET revision=revision+1 WHERE scope_key=? AND name=?", (scope_key(context), name))
