"""Release durable, simulated grades from the existing academic records."""
from datetime import datetime, time, timedelta
import json

from ..event_delivery import ACADEMIC_NOTICES
from ..kernel.codec import digest, encode, moment, require, wire
from .models import zone
from .service import timestamp

VERSION_KEY = "academics_term_grades_schema_version"
RULE_VERSION = "term_grade_rule_v1"
EVENT_KIND = "academics.grade.released"
CHECK_INTERVAL = timedelta(minutes=1)
DDL = """CREATE TABLE life_term_grades (
    owner_key TEXT NOT NULL, term_id TEXT NOT NULL, course_id TEXT NOT NULL,
    record_json TEXT NOT NULL, PRIMARY KEY(owner_key,term_id,course_id))"""


def install(db):
    version = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
    table = db.execute("SELECT sql FROM sqlite_master WHERE name='life_term_grades'").fetchone()
    if version is None:
        require(table is None, "unclaimed term grades schema")
        db.execute(DDL)
        db.execute("INSERT INTO life_metadata VALUES (?, '1')", (VERSION_KEY,))
    else:
        require(version[0] == "1" and table and " ".join(table[0].split()) == " ".join(DDL.split()),
                "unsupported term grades schema")


def _draw(owner, term_id, course_id, purpose):
    return int(digest((RULE_VERSION, owner, term_id, course_id, purpose))[:13], 16) / (1 << 52)


def release_time(owner, term_id, course_id, assessment_at, tz):
    assessment_at = moment(assessment_at)
    days = 7 + int(15 * _draw(owner, term_id, course_id, "release_days"))
    hour = 10 + int(9 * _draw(owner, term_id, course_id, "release_hour"))
    date = assessment_at.astimezone(tz).date() + timedelta(days=days)
    release = datetime.combine(date, time(hour), tz)
    # At the edges, keep the daytime slot within the full elapsed-day window.
    if release < assessment_at + timedelta(days=7):
        release += timedelta(days=1)
    elif release > assessment_at + timedelta(days=21):
        release -= timedelta(days=1)
    return moment(release)


def letter_grade(score):
    for minimum, letter in ((90, "A"), (85, "A-"), (82, "B+"), (78, "B"), (75, "B-"),
                            (71, "C+"), (66, "C"), (62, "C-"), (60, "D")):
        if score >= minimum:
            return letter
    return "F"


def _attendance(db, owner, term_id, course_id):
    total = present = 0
    has_participation = False
    for row in db.execute("""SELECT json_extract(s.record_json, '$.cancelled'), p.record_json
            FROM life_academic_sessions s LEFT JOIN life_academic_participation p USING (owner_key, session_id)
            WHERE s.owner_key=? AND s.term_id=? AND json_extract(s.record_json, '$.course_id')=?""",
            (owner, term_id, course_id)):
        cancelled, record = row
        has_participation |= record is not None
        if cancelled:
            continue
        total += 1
        if record is not None:
            participation = json.loads(record)
            if "attendance" in participation:
                present += participation["attendance"]["outcome"] in ("on_time", "late")
            else:
                # Older class activities recorded work intervals without an attendance outcome.
                present += participation["attended_s"] > 0
    return present / total if has_participation and total else 1.0


def _review(db, owner, assignment):
    completed = db.execute("""SELECT COALESCE(SUM(json_extract(record_json, '$.completed_work_s')), 0)
        FROM life_study_chunks WHERE owner_key=? AND assignment_id=?""", (owner, assignment["assignment_id"])).fetchone()[0]
    return min(1.0, completed / assignment["estimated_work_s"])


class TermGrades:
    def __init__(self, academics):
        self.academics = academics
        self.next_check = None

    def tick(self, *, now, get_access):
        if self.next_check is not None and now < self.next_check:
            return
        next_check = now + CHECK_INTERVAL
        storage = self.academics.storage
        access = get_access()
        for scope in access.scopes():
            with storage._lock:
                db = storage._connection
                owner = db.execute("SELECT context_key FROM life_academic_owners WHERE owner_key=?",
                                   (scope.owner_key,)).fetchone()
                if owner is None or owner[0] != scope.context_key or not access.authorize(scope, "kernel.recover"):
                    continue
                with access.write_guard(scope, "kernel.recover"), storage.transaction():
                    next_check = self._release(db, scope, now, next_check)
        self.next_check = next_check

    def _release(self, db, scope, now, next_check):
        owner = scope.owner_key
        records = self.academics._records
        terms = records(db, owner, "term")
        course_names = {(item["term_id"], item["course"]["course_id"]): item["course"]["name"]
                        for item in records(db, owner, "timetable").values()}
        latest = {}
        for assignment in records(db, owner, "assignment").values():
            key = assignment["term_id"], assignment["course_id"]
            deadline = timestamp(assignment["due_at"]), assignment["assignment_id"]
            if key not in latest or deadline > latest[key][0]:
                latest[key] = deadline, assignment
        assessment = {key: deadline[0] for key, (deadline, _) in latest.items()}
        for term_id, course_id, course_name, end_at in db.execute("""SELECT term_id,
                json_extract(record_json, '$.course_id') AS course_id,
                json_extract(record_json, '$.course_name'), MAX(json_extract(record_json, '$.end_at'))
            FROM life_academic_sessions WHERE owner_key=? GROUP BY term_id, course_id""", (owner,)):
            key = term_id, course_id
            course_names.setdefault(key, course_name)
            end = timestamp(end_at)
            assessment[key] = max(end, assessment.get(key, end))
        released = {tuple(row) for row in db.execute("SELECT term_id,course_id FROM life_term_grades WHERE owner_key=?",
                                                    (owner,))}
        for key in sorted(assessment.keys() - released):
            term_id, course_id = key
            if term_id not in terms:
                continue
            term = terms[term_id]
            release = release_time(owner, term_id, course_id, assessment[key], zone(term["timezone"]))
            if now < release:
                next_check = min(next_check, release)
                continue
            assignment = latest[key][1] if key in latest else None
            self._save(db, scope, term, course_id, course_names[key], assessment[key], release, assignment, now)
        return next_check

    def _save(self, db, scope, term, course_id, course_name, assessment, release, assignment, now):
        owner, term_id = scope.owner_key, term["term_id"]
        components = {"attendance": _attendance(db, owner, term_id, course_id),
                      "luck": _draw(owner, term_id, course_id, "luck")}
        if assignment is None:
            weights = {"attendance": 0.6, "luck": 0.4}
        else:
            components["review"] = _review(db, owner, assignment)
            weights = {"attendance": 0.3, "review": 0.5, "luck": 0.2}
        score = round(50 + 50 * sum(weights[key] * components[key] for key in weights))
        letter = letter_grade(score)
        notice = f"【教务通知】{term['academic_year']}《{course_name}》成绩已发布：{score}（{letter}）"
        record = {"term_id": term_id, "course_id": course_id, "course_name": course_name,
                  "assignment_id": assignment["assignment_id"] if assignment is not None else None,
                  "assessment_at": wire(assessment), "release_at": wire(release), "score": score, "letter": letter,
                  "components": components, "weights": weights, "rule_version": RULE_VERSION, "provenance": "simulated"}
        db.execute("INSERT INTO life_term_grades VALUES (?,?,?,?)", (owner, term_id, course_id, encode(record)))
        self.academics.storage.record_event(event_id="term-grade-" + digest((owner, term_id, course_id)),
            owner_key=owner, context_key=scope.context_key, kind=EVENT_KIND, payload={"notice": notice},
            occurred_at=now, consumers=(ACADEMIC_NOTICES,))
