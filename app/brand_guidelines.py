"""Versioned, tenant-scoped brand guidance and deterministic content policy."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from uuid import uuid4


DEMO_BRAND_GUIDELINE_SOURCE = "skill:demo-brand-content-house-style"
DEMO_BRAND_TOOLS = (
    "https://demo.example/tools/pricing-calculator",
    "https://demo.example/tools/comparison",
    "https://demo.example/tools/changelog",
)
DEMO_BRAND_NEWSLETTER_INSTRUCTIONS = """Write in the brand voice, in direct first person, with one concrete thesis and useful decision support—not as an expanded social post or generic blog post. Open with “Hey,”. Explain the move, why it matters, who is a good fit, the practical play, traps, a quick checklist, and the bottom line; headings may vary naturally. Use current verified numbers, fees, dates, ratios, or example math when applicable. Include a natural Demo Brand tool backlink and a soft topic-specific invitation to reply. Close the editorial body exactly with “— The Team”. Normal issues must be at least 750 words and should land between 750 and 900 words without filler. A shorter quick hit is never inferred by AI: it requires a revision-specific reason and explicit operator authorization. Before approval, include a 16:9 editorial thumbnail, avoid readable text/logos/fake marks/people/distorted hands, and enable web thumbnail display. Volatile facts require current governed provenance and fact checking."""
DEMO_BRAND_NEWSLETTER_RULES: dict[str, Any] = {
    "minimum_words": 750,
    "preferred_words": {"minimum": 750, "maximum": 900},
    "quick_hit": {"minimum_words": 250, "requires_operator_authorization": True},
    "required_opening": "Hey,",
    "required_signoff": "— The Team",
    "approved_tool_backlinks": list(DEMO_BRAND_TOOLS),
    "pricing_tool_backlinks": [
        "https://demo.example/tools/pricing-calculator",
    ],
    "thumbnail": {"required": True, "display_on_web": True},
    "require_one_thesis": True,
    "require_reply_language": True,
    "require_current_fact_check_and_provenance": True,
    "operator_checklist": [
        "direct_first_person_voice",
        "one_thesis",
        "move",
        "why_it_matters",
        "good_fit",
        "play",
        "traps",
        "checklist",
        "bottom_line",
        "numbers_and_example_math_when_applicable",
        "topic_specific_reply_cta",
    ],
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_guidelines (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, content_type TEXT NOT NULL,
  channel TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
  active_version_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(brand_id,content_type,channel)
);
CREATE TABLE IF NOT EXISTS brand_guideline_versions (
  id TEXT PRIMARY KEY, guideline_id TEXT NOT NULL, version INTEGER NOT NULL,
  instructions TEXT NOT NULL, rules_json TEXT NOT NULL, source_ref TEXT,
  change_reason TEXT NOT NULL, created_by TEXT NOT NULL,
  content_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(guideline_id) REFERENCES brand_guidelines(id),
  UNIQUE(guideline_id,version)
);
CREATE TABLE IF NOT EXISTS brand_guideline_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, guideline_id TEXT NOT NULL,
  version_id TEXT, action TEXT NOT NULL, actor TEXT NOT NULL,
  reason TEXT NOT NULL, details_json TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS newsletter_policy_reviews (
  id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, revision INTEGER NOT NULL,
  guideline_version_id TEXT NOT NULL, reviewer TEXT NOT NULL,
  checklist_json TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(issue_id,revision,guideline_version_id)
);
CREATE TABLE IF NOT EXISTS newsletter_quick_hit_overrides (
  id TEXT PRIMARY KEY, issue_id TEXT NOT NULL, revision INTEGER NOT NULL,
  guideline_version_id TEXT NOT NULL, actor TEXT NOT NULL,
  reason TEXT NOT NULL, authorized_at TEXT NOT NULL,
  UNIQUE(issue_id,revision,guideline_version_id)
);
CREATE TRIGGER IF NOT EXISTS brand_guideline_versions_no_update
BEFORE UPDATE ON brand_guideline_versions BEGIN SELECT RAISE(ABORT,'guideline versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS brand_guideline_versions_no_delete
BEFORE DELETE ON brand_guideline_versions BEGIN SELECT RAISE(ABORT,'guideline versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS newsletter_policy_reviews_no_update
BEFORE UPDATE ON newsletter_policy_reviews BEGIN SELECT RAISE(ABORT,'policy reviews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS newsletter_policy_reviews_no_delete
BEFORE DELETE ON newsletter_policy_reviews BEGIN SELECT RAISE(ABORT,'policy reviews are immutable'); END;
CREATE TRIGGER IF NOT EXISTS newsletter_quick_hit_overrides_no_update
BEFORE UPDATE ON newsletter_quick_hit_overrides BEGIN SELECT RAISE(ABORT,'quick-hit overrides are immutable'); END;
CREATE TRIGGER IF NOT EXISTS newsletter_quick_hit_overrides_no_delete
BEFORE DELETE ON newsletter_quick_hit_overrides BEGIN SELECT RAISE(ABORT,'quick-hit overrides are immutable'); END;
"""


class BrandGuidelineError(ValueError):
    """A guideline mutation or policy attestation is invalid."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise BrandGuidelineError("rules and checklist values must be JSON-persistable") from exc


def _fingerprint(instructions: str, rules: Mapping[str, Any]) -> str:
    material = _json({"instructions": instructions, "rules": dict(rules)})
    return "sha256:" + sha256(material.encode()).hexdigest()


class BrandGuidelineStore:
    def __init__(self, database: str | Path, *, clock: Callable[[], str] = _now) -> None:
        self.database = str(database)
        self.clock = clock
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def create(
        self, *, brand_id: str, content_type: str, channel: str, name: str,
        instructions: str, rules: Mapping[str, Any], actor: str, reason: str,
        source_ref: str | None = None, activate: bool = False,
    ) -> dict[str, Any]:
        values = [brand_id, content_type, channel, name, instructions, actor, reason]
        if any(not str(value).strip() for value in values):
            raise BrandGuidelineError("brand, scope, name, instructions, actor, and reason are required")
        guideline_id, version_id, timestamp = str(uuid4()), str(uuid4()), self.clock()
        normalized_rules = self._validate_rules(rules)
        fingerprint = _fingerprint(instructions.strip(), normalized_rules)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO brand_guidelines
                       (id,brand_id,content_type,channel,name,status,active_version_id,created_at,updated_at)
                       VALUES (?,?,?,?,?,'draft',NULL,?,?)""",
                    (guideline_id, brand_id, content_type.strip().casefold(), channel.strip().casefold(),
                     name.strip(), timestamp, timestamp),
                )
            except sqlite3.IntegrityError as exc:
                raise BrandGuidelineError("a guideline already exists for this brand and scope") from exc
            connection.execute(
                """INSERT INTO brand_guideline_versions
                   (id,guideline_id,version,instructions,rules_json,source_ref,change_reason,
                    created_by,content_fingerprint,created_at) VALUES (?,?,1,?,?,?,?,?,?,?)""",
                (version_id, guideline_id, instructions.strip(), _json(normalized_rules), source_ref,
                 reason.strip(), actor.strip(), fingerprint, timestamp),
            )
            self._audit(connection, guideline_id, version_id, "created", actor, reason,
                        {"content_type": content_type, "channel": channel, "version": 1}, timestamp)
        if activate:
            return self.activate(guideline_id, 1, actor=actor, reason=reason)
        return self.get(guideline_id)

    def create_version(
        self, guideline_id: str, *, instructions: str, rules: Mapping[str, Any],
        actor: str, reason: str, source_ref: str | None = None,
    ) -> dict[str, Any]:
        if any(not value.strip() for value in (instructions, actor, reason)):
            raise BrandGuidelineError("instructions, actor, and change reason are required")
        normalized_rules = self._validate_rules(rules)
        timestamp, version_id = self.clock(), str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            guideline = connection.execute(
                "SELECT * FROM brand_guidelines WHERE id=? AND status!='archived'", (guideline_id,),
            ).fetchone()
            if guideline is None:
                raise KeyError("brand guideline not found")
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS version FROM brand_guideline_versions WHERE guideline_id=?",
                (guideline_id,),
            ).fetchone()["version"])
            connection.execute(
                """INSERT INTO brand_guideline_versions
                   (id,guideline_id,version,instructions,rules_json,source_ref,change_reason,
                    created_by,content_fingerprint,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (version_id, guideline_id, version, instructions.strip(), _json(normalized_rules),
                 source_ref, reason.strip(), actor.strip(), _fingerprint(instructions.strip(), normalized_rules),
                 timestamp),
            )
            connection.execute("UPDATE brand_guidelines SET updated_at=? WHERE id=?", (timestamp, guideline_id))
            self._audit(connection, guideline_id, version_id, "version_created", actor, reason,
                        {"version": version}, timestamp)
        return self.get(guideline_id)

    def activate(self, guideline_id: str, version: int, *, actor: str, reason: str) -> dict[str, Any]:
        if not actor.strip() or not reason.strip():
            raise BrandGuidelineError("activation actor and reason are required")
        timestamp = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            guideline = connection.execute(
                "SELECT * FROM brand_guidelines WHERE id=? AND status!='archived'", (guideline_id,),
            ).fetchone()
            if guideline is None:
                raise KeyError("brand guideline not found")
            selected = connection.execute(
                "SELECT * FROM brand_guideline_versions WHERE guideline_id=? AND version=?",
                (guideline_id, version),
            ).fetchone()
            if selected is None:
                raise KeyError("brand guideline version not found")
            if guideline["active_version_id"] == selected["id"]:
                return self._decode_guideline(connection, guideline)
            previous = guideline["active_version_id"]
            connection.execute(
                "UPDATE brand_guidelines SET status='active',active_version_id=?,updated_at=? WHERE id=?",
                (selected["id"], timestamp, guideline_id),
            )
            affected = self._invalidate_newsletter_governance(
                connection, dict(guideline), actor=actor.strip(), timestamp=timestamp,
            )
            self._audit(connection, guideline_id, selected["id"], "activated", actor, reason,
                        {"version": version, "previous_version_id": previous,
                         "invalidated_newsletter_count": affected}, timestamp)
        return self.get(guideline_id)

    def archive(self, guideline_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        if not actor.strip() or not reason.strip():
            raise BrandGuidelineError("archive actor and reason are required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM brand_guidelines WHERE id=?", (guideline_id,)).fetchone()
            if row is None:
                raise KeyError("brand guideline not found")
            if row["active_version_id"]:
                raise BrandGuidelineError("an active guideline cannot be archived; activate a replacement instead")
            timestamp = self.clock()
            connection.execute(
                "UPDATE brand_guidelines SET status='archived',updated_at=? WHERE id=?",
                (timestamp, guideline_id),
            )
            self._audit(connection, guideline_id, None, "archived", actor, reason, {}, timestamp)
        return self.get(guideline_id)

    def get(self, guideline_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM brand_guidelines WHERE id=?", (guideline_id,)).fetchone()
            if row is None:
                raise KeyError("brand guideline not found")
            return self._decode_guideline(connection, row)

    def list(self, brand_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM brand_guidelines WHERE brand_id=?"
        if not include_archived:
            query += " AND status!='archived'"
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY content_type,channel,id", (brand_id,)).fetchall()
            return [self._decode_guideline(connection, row) for row in rows]

    def resolve(self, brand_id: str, content_type: str, channel: str) -> dict[str, Any] | None:
        scopes = ((content_type.casefold(), channel.casefold()),
                  (content_type.casefold(), "*"), ("*", channel.casefold()), ("*", "*"))
        with self._connect() as connection:
            for content_scope, channel_scope in scopes:
                row = connection.execute(
                    """SELECT * FROM brand_guidelines WHERE brand_id=? AND content_type=?
                       AND channel=? AND status='active' AND active_version_id IS NOT NULL""",
                    (brand_id, content_scope, channel_scope),
                ).fetchone()
                if row is not None:
                    return self._active_version(connection, row)
        return None

    def audit(self, guideline_id: str) -> list[dict[str, Any]]:
        self.get(guideline_id)
        with self._connect() as connection:
            return [self._decode_json(dict(row), "details_json", "details") for row in connection.execute(
                "SELECT * FROM brand_guideline_audit WHERE guideline_id=? ORDER BY sequence",
                (guideline_id,),
            )]

    def record_policy_review(
        self, *, issue_id: str, revision: int, reviewer: str,
        checklist: Mapping[str, bool], content_type: str = "newsletter", channel: str = "beehiiv",
    ) -> dict[str, Any]:
        if not reviewer.strip():
            raise BrandGuidelineError("policy reviewer is required")
        with self._connect() as connection:
            issue = connection.execute(
                "SELECT brand_id,current_revision FROM newsletter_issues WHERE id=?", (issue_id,),
            ).fetchone()
            if issue is None:
                raise KeyError("newsletter issue not found")
            if int(issue["current_revision"]) != int(revision):
                raise BrandGuidelineError("policy review revision must match the current revision")
        guideline = self.resolve(issue["brand_id"], content_type, channel)
        if guideline is None:
            raise BrandGuidelineError("no active guideline applies to this newsletter")
        required = set(guideline["rules"].get("operator_checklist") or [])
        unknown = set(checklist) - required
        if unknown:
            raise BrandGuidelineError("unknown checklist items: " + ", ".join(sorted(unknown)))
        timestamp = self.clock()
        record = {
            "id": str(uuid4()), "issue_id": issue_id, "revision": int(revision),
            "guideline_version_id": guideline["version_id"], "reviewer": reviewer.strip(),
            "checklist_json": _json({key: checklist.get(key) is True for key in sorted(required)}),
            "created_at": timestamp,
        }
        with self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO newsletter_policy_reviews
                       (id,issue_id,revision,guideline_version_id,reviewer,checklist_json,created_at)
                       VALUES (:id,:issue_id,:revision,:guideline_version_id,:reviewer,:checklist_json,:created_at)""",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                raise BrandGuidelineError("this revision already has an immutable policy review") from exc
        result = dict(record)
        result["checklist"] = json.loads(result.pop("checklist_json"))
        return result

    def authorize_quick_hit(
        self, *, issue_id: str, revision: int, actor: str, reason: str,
        content_type: str = "newsletter", channel: str = "beehiiv",
    ) -> dict[str, Any]:
        if not actor.strip() or len(reason.strip()) < 12:
            raise BrandGuidelineError("quick-hit authorization requires an actor and a specific reason")
        with self._connect() as connection:
            issue = connection.execute(
                "SELECT brand_id,current_revision FROM newsletter_issues WHERE id=?", (issue_id,),
            ).fetchone()
            if issue is None:
                raise KeyError("newsletter issue not found")
            if int(issue["current_revision"]) != int(revision):
                raise BrandGuidelineError("quick-hit revision must match the current revision")
        guideline = self.resolve(issue["brand_id"], content_type, channel)
        if guideline is None:
            raise BrandGuidelineError("no active guideline applies to this newsletter")
        quick_hit_rule = guideline["rules"].get("quick_hit")
        if not isinstance(quick_hit_rule, Mapping) or quick_hit_rule.get("requires_operator_authorization") is not True:
            raise BrandGuidelineError("the active guideline does not permit quick-hit authorization")
        record = {
            "id": str(uuid4()), "issue_id": issue_id, "revision": int(revision),
            "guideline_version_id": guideline["version_id"], "actor": actor.strip(),
            "reason": reason.strip(), "authorized_at": self.clock(),
        }
        with self._connect() as connection:
            try:
                connection.execute(
                    """INSERT INTO newsletter_quick_hit_overrides
                       (id,issue_id,revision,guideline_version_id,actor,reason,authorized_at)
                       VALUES (:id,:issue_id,:revision,:guideline_version_id,:actor,:reason,:authorized_at)""",
                    record,
                )
            except sqlite3.IntegrityError as exc:
                raise BrandGuidelineError("this revision already has an immutable quick-hit authorization") from exc
        return record

    def evaluate_newsletter(
        self, *, brand_id: str, issue_id: str, revision: int, content: Mapping[str, Any],
    ) -> dict[str, Any]:
        guideline = self.resolve(brand_id, "newsletter", "beehiiv")
        if guideline is None:
            return {"guideline": None, "word_count": self._word_count(content),
                    "reviewable": True, "blockers": [], "warnings": []}
        rules = guideline["rules"]
        text = self._body_text(content)
        count = self._word_count(content)
        with self._connect() as connection:
            review_row = connection.execute(
                """SELECT * FROM newsletter_policy_reviews WHERE issue_id=? AND revision=?
                   AND guideline_version_id=?""",
                (issue_id, revision, guideline["version_id"]),
            ).fetchone()
            override_row = connection.execute(
                """SELECT * FROM newsletter_quick_hit_overrides WHERE issue_id=? AND revision=?
                   AND guideline_version_id=?""",
                (issue_id, revision, guideline["version_id"]),
            ).fetchone()
        checklist = json.loads(review_row["checklist_json"]) if review_row else {}
        blockers: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        add = lambda code, message: blockers.append({"code": code, "message": message})
        minimum = int(rules.get("minimum_words", 0))
        if count < minimum:
            quick = rules.get("quick_hit") or {}
            quick_minimum = int(quick.get("minimum_words", minimum))
            if override_row is None:
                add("policy_minimum_word_count",
                    f"This issue has {count} words; active guideline v{guideline['version']} requires at least {minimum}. A shorter quick hit requires explicit operator authorization and a reason.")
            elif count < quick_minimum:
                add("policy_quick_hit_floor",
                    f"The authorized quick hit has {count} words; guideline v{guideline['version']} requires at least {quick_minimum}.")
            else:
                warnings.append({"code": "policy_quick_hit_authorized",
                                 "message": f"Quick hit explicitly authorized by {override_row['actor']}: {override_row['reason']}"})
        preferred = rules.get("preferred_words") or {}
        if count > int(preferred.get("maximum", 10**9)):
            warnings.append({"code": "policy_above_preferred_length",
                             "message": f"This issue has {count} words; the preferred maximum is {preferred['maximum']}."})
        opening = str(rules.get("required_opening") or "")
        if opening and not text.lstrip().startswith(opening):
            add("policy_required_opening", f"The editorial body must open with {opening!r}.")
        signoff = str(rules.get("required_signoff") or "")
        if signoff and signoff not in [line.strip() for line in text.splitlines()]:
            add("policy_required_signoff", f"The editorial body must include the exact sign-off {signoff!r}.")
        if rules.get("require_one_thesis") and not str(content.get("editorial_thesis") or "").strip():
            add("policy_one_thesis", "State one concrete editorial thesis for this issue.")
        links = [url for url in rules.get("approved_tool_backlinks") or [] if url in text]
        if not links:
            add("policy_tool_backlink", "Include at least one approved Demo Brand tool backlink naturally in the issue.")
        topic_text = " ".join((str(content.get("editorial_thesis") or ""), text)).casefold()
        if "pricing" in topic_text and not any(
            url in text for url in rules.get("pricing_tool_backlinks") or []
        ):
            add("policy_pricing_tool", "Pricing coverage must naturally link to the pricing calculator.")
        metadata = content.get("delivery_metadata") or {}
        thumbnail = rules.get("thumbnail") or {}
        if thumbnail.get("required") and not str(metadata.get("thumbnail_url") or "").strip():
            add("policy_thumbnail_url", "Add a Beehiiv thumbnail URL before this revision can be reviewed.")
        web_settings = metadata.get("web_settings") or {}
        if thumbnail.get("display_on_web") and web_settings.get("display_thumbnail_on_web") is not True:
            add("policy_thumbnail_web_display", "Enable thumbnail display on the Beehiiv web version.")
        if rules.get("require_reply_language") and not re.search(r"\breply\b", text, re.IGNORECASE):
            add("policy_reply_cta", "Include a soft, topic-specific invitation for the reader to reply.")
        first_person = bool(re.search(r"\b(?:I|I’m|I'm|I’ve|I've|my|me)\b", text, re.IGNORECASE))
        if not first_person and checklist.get("direct_first_person_voice") is not True:
            add("policy_brand_voice", "Use a direct first-person brand voice or explicitly confirm it in the policy review.")
        required_checks = list(rules.get("operator_checklist") or [])
        missing = [key for key in required_checks if checklist.get(key) is not True]
        for key in missing:
            if key == "direct_first_person_voice" and first_person:
                continue
            add("policy_review_required", "Operator policy review must confirm: " + key.replace("_", " ") + ".")
        return {
            "guideline": {key: guideline[key] for key in (
                "id", "name", "content_type", "channel", "version_id", "version",
                "content_fingerprint", "source_ref",
            )},
            "word_count": count, "reviewable": not blockers,
            "blockers": blockers, "warnings": warnings,
            "policy_review": ({"reviewer": review_row["reviewer"],
                               "created_at": review_row["created_at"], "checklist": checklist}
                              if review_row else None),
            "quick_hit_override": (dict(override_row) if override_row else None),
        }

    def seed_demo_brand(self, brand_id: str, *, actor: str = "system:seed") -> dict[str, Any]:
        existing = self.resolve(brand_id, "newsletter", "beehiiv")
        if existing is not None:
            return self.get(existing["id"])
        return self.create(
            brand_id=brand_id, content_type="newsletter", channel="beehiiv",
            name="Demo Brand newsletter house style",
            instructions=DEMO_BRAND_NEWSLETTER_INSTRUCTIONS,
            rules=DEMO_BRAND_NEWSLETTER_RULES, actor=actor,
            reason="Seed the governed Demo Brand house style with the operator's stricter 750-word minimum.",
            source_ref=DEMO_BRAND_GUIDELINE_SOURCE, activate=True,
        )

    @staticmethod
    def _validate_rules(rules: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(rules, Mapping):
            raise BrandGuidelineError("rules must be an object")
        normalized = json.loads(_json(dict(rules)))
        minimum = normalized.get("minimum_words")
        if minimum is not None and (not isinstance(minimum, int) or not 1 <= minimum <= 10000):
            raise BrandGuidelineError("minimum_words must be an integer between 1 and 10000")
        checklist = normalized.get("operator_checklist", [])
        if not isinstance(checklist, list) or any(not isinstance(item, str) or not item for item in checklist):
            raise BrandGuidelineError("operator_checklist must be a list of names")
        return normalized

    def _decode_guideline(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        versions = [self._decode_version(item) for item in connection.execute(
            "SELECT * FROM brand_guideline_versions WHERE guideline_id=? ORDER BY version DESC",
            (row["id"],),
        )]
        result["versions"] = versions
        result["active_version"] = next(
            (item for item in versions if item["id"] == row["active_version_id"]), None,
        )
        return result

    def _active_version(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        version = connection.execute(
            "SELECT * FROM brand_guideline_versions WHERE id=?", (row["active_version_id"],),
        ).fetchone()
        assert version is not None
        decoded = self._decode_version(version)
        return {
            "id": row["id"], "brand_id": row["brand_id"], "name": row["name"],
            "content_type": row["content_type"], "channel": row["channel"],
            "version_id": decoded["id"], "version": decoded["version"],
            "instructions": decoded["instructions"], "rules": decoded["rules"],
            "source_ref": decoded["source_ref"],
            "content_fingerprint": decoded["content_fingerprint"],
        }

    @staticmethod
    def _decode_version(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["rules"] = json.loads(result.pop("rules_json"))
        return result

    @staticmethod
    def _decode_json(row: dict[str, Any], source: str, target: str) -> dict[str, Any]:
        row[target] = json.loads(row.pop(source) or "{}")
        return row

    @staticmethod
    def _body_text(content: Mapping[str, Any]) -> str:
        sections = content.get("sections") or []
        return "\n\n".join(
            str(section.get("body") or "") for section in sections if isinstance(section, Mapping)
        )

    @classmethod
    def _word_count(cls, content: Mapping[str, Any]) -> int:
        return len(re.findall(r"\b[\w’'-]+\b", cls._body_text(content), flags=re.UNICODE))

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, guideline_id: str, version_id: str | None,
        action: str, actor: str, reason: str, details: Mapping[str, Any], timestamp: str,
    ) -> None:
        connection.execute(
            """INSERT INTO brand_guideline_audit
               (guideline_id,version_id,action,actor,reason,details_json,at)
               VALUES (?,?,?,?,?,?,?)""",
            (guideline_id, version_id, action, actor.strip(), reason.strip(), _json(dict(details)), timestamp),
        )

    @staticmethod
    def _invalidate_newsletter_governance(
        connection: sqlite3.Connection, guideline: Mapping[str, Any], *,
        actor: str, timestamp: str,
    ) -> int:
        if guideline["content_type"] not in {"newsletter", "*"} or guideline["channel"] not in {"beehiiv", "*"}:
            return 0
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "newsletter_issues" not in tables:
            return 0
        issues = connection.execute(
            """SELECT id FROM newsletter_issues WHERE brand_id=?
               AND lifecycle IN ('fact_checked','approved')""",
            (guideline["brand_id"],),
        ).fetchall()
        ids = [row["id"] for row in issues]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        connection.execute(
            f"""UPDATE newsletter_issues SET lifecycle='draft',approved_revision=NULL,
               approved_by=NULL,approved_at=NULL,updated_at=? WHERE id IN ({placeholders})""",
            (timestamp, *ids),
        )
        if {"approval_snapshots", "approval_snapshot_invalidations"} <= tables:
            snapshots = connection.execute(
                f"""SELECT id FROM approval_snapshots WHERE resource_type='newsletter_issue'
                   AND resource_id IN ({placeholders}) AND id NOT IN
                   (SELECT snapshot_id FROM approval_snapshot_invalidations)""",
                ids,
            ).fetchall()
            connection.executemany(
                """INSERT INTO approval_snapshot_invalidations
                   (snapshot_id,actor,reason,invalidated_at) VALUES (?,?,?,?)""",
                [(row["id"], actor, "active brand guideline changed", timestamp) for row in snapshots],
            )
        return len(ids)


__all__ = [
    "BrandGuidelineError", "BrandGuidelineStore", "DEMO_BRAND_GUIDELINE_SOURCE",
    "DEMO_BRAND_NEWSLETTER_INSTRUCTIONS", "DEMO_BRAND_NEWSLETTER_RULES",
]
