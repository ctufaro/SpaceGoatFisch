"""SpaceGoat Fisch Tournament Server. All application code and web assets live here.

Run --self-test for deterministic replay/integrity tests, --smoke-test for bundled
OCR/Qt/HTTP startup checks, --build for a Windows one-directory distribution.
No gameplay APIs are used. A visual observer cannot recover unseen messages.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import io
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import re
import socket
import sqlite3
import sys
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import cv2
import numpy as np

APP_NAME = "SpaceGoat Fisch Tournament Server"
LEGACY_PATTERN = r"^(?:\[[^\]]+\]\s*)?(?P<username>[A-Za-z0-9_]{3,20})\s+caught\s+(?:an?\s+)?(?P<rarity>Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine)\s+(?P<fish>.+?)\s*\((?P<weight>[\d.,\s]+)\s*kg\)[.!]?$"
LEGACY_START = r"^(?:\[[^\]]+\]\s*)?[A-Za-z0-9_]{3,20}\s+caught\b"
# Based on the operator's two live-game screenshots, not a gameplay API spec.
# Require the System prefix and terminal punctuation to avoid counting partial wraps.
# Preserve modifiers in the fish descriptor; no unverified species catalog is used.
PREVIOUS_FISCH_PATTERN = r"^\[\s*System\s*\]\s*(?P<username>[A-Za-z0-9_]{3,20})\s+has\s+perfectly\s+caught\s+an?\s+(?P<rarity>Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine)\s+(?P<weight>[\d.,\s]+)\s*kg\s+(?P<fish>.+?)\s*(?:!\.?|\.)$"
FISCH_PATTERN = r"^\[\s*System\s*\]\s*(?P<username>[A-Za-z0-9_]{3,20}?)\s*has\s*(?:perfectly\s*)?caught\s*an?\s*(?P<rarity>Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine)\s*(?P<weight>[\d.,\s]+)\s*kg\s*(?P<fish>.+?)\s*[!.]+$"
FISCH_START = r"^\s*\[\s*System\s*\]"
COMMON_FISH_PATTERN = r'''^\[\s*System\s*\]\s*(?:[A-Za-z]\s+)?(?P<username>[A-Za-z0-9_]{3,20}?)(?:\s+[A-Za-z|])?\s*has\s*(?:s\s+)?(?:perfectly\s*)?caught\s*(?:t\s*)?an?\s*[(:'";’]*\s*(?P<rarity>Common)\s*[(:'";’]*\s*(?:[A-Za-z&|]\s+)?(?:\d{1,2}\s+(?=(?:\d{1,2}|\d{4,})\.\d+\s*kg))?(?P<weight>[\d.,\s]+)\s*kg\s*(?P<fish>.+?)\s*[!.]+$'''
ALL_RARITIES_PATTERN = r'''^\[\s*System\s*\]\s*(?:[A-Za-z]\s+)?(?P<username>[A-Za-z0-9_]{3,20}?)\s*(?:[I|hl]\s+)?has\s*(?:s\s+)?(?:perfectly\s*)?caught\s*(?:t\s+)?an?\s*[(:'";’]*\s*(?P<rarity>Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine)\s*[(:'";’]*\s*(?:\d{1,2}\s+(?=(?:\d{1,2}|\d{4,})\.\d+\s*kg))?(?P<weight>[\d.,\s]+)\s*kg\s*(?P<fish>.+?)\s*[!.]+$'''
NAMED_PATTERNS = [
    {"name": "Common Fish", "pattern": COMMON_FISH_PATTERN},
    {"name": "All Rarities", "pattern": ALL_RARITIES_PATTERN},
    {"name": "Limited Fish", "pattern": ALL_RARITIES_PATTERN.replace(
        "(?P<rarity>Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine)", "(?P<rarity>Limited)")},
]
DEFAULTS = {
    "region": None, "host": "", "port": 8765, "fps": 12,
    "frame_queue": 24, "ocr_queue": 64, "diagnostic": False,
    "confidence": 0.92, "min_kg": "0.001", "max_kg": "1000000",
    "decimal_separator": ".", "parser_verified": True, "best_effort": True,
    "patterns": [FISCH_PATTERN], "message_start": FISCH_START, "registered_players": [], "name_corrections": {}, "rules": "",
    "overlay": {"players": 5, "scale": 1.0, "opacity": 0.86,
                "vertical": False, "recent": True},
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def data_directory():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SpaceGoatFisch"


class Config:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "config.json"
        self.lock = threading.RLock()
        self.values = json.loads(json.dumps(DEFAULTS))
        if self.path.exists():
            previous_text = self.path.read_text(encoding="utf-8")
            self.values.update(json.loads(previous_text))
            # Only replace the shipped illustrative parser. Keep customized rules,
            # selected region, tournament history, and review events intact.
            if self.values.get("patterns") == [LEGACY_PATTERN] and self.values.get("message_start") == LEGACY_START:
                backup = self.directory / "config.pre-fisch-samples.json"
                if not backup.exists():
                    backup.write_text(previous_text, encoding="utf-8")
                self.values.update(patterns=[FISCH_PATTERN], message_start=FISCH_START,
                                   decimal_separator=".", parser_verified=True)
            elif self.values.get("patterns") == [PREVIOUS_FISCH_PATTERN] and self.values.get("message_start") == FISCH_START:
                backup = self.directory / "config.pre-spacing-fix.json"
                if not backup.exists():
                    backup.write_text(previous_text, encoding="utf-8")
                self.values["patterns"] = [FISCH_PATTERN]
        # Discard credentials left by older versions.
        self.values.pop("admin_token", None)
        if "pattern_presets" not in self.values:
            self.values["pattern_presets"] = json.loads(json.dumps(NAMED_PATTERNS))
            for pattern in self.values["patterns"]:
                if not any(p["pattern"] == pattern for p in self.values["pattern_presets"]):
                    self.values["pattern_presets"].append({"name": "Existing pattern " + str(len(self.values["pattern_presets"]) - 2), "pattern": pattern})
        self.save()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.values))

    def save(self):
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)

    def update(self, values):
        with self.lock:
            self.values.update(values)
            self.save()


def parse_weight(text, settings):
    """Locale is explicit: 1,234 is never guessed to be 1.234 or 1234."""
    raw = str(text).strip().replace("\u00a0", " ")
    decimal = settings["decimal_separator"]
    thousands = "," if decimal == "." else "."
    pieces = raw.split(decimal)
    if len(pieces) > 2:
        raise ValueError("Multiple decimal separators")
    whole = pieces[0]
    if thousands in whole and " " in whole:
        raise ValueError("Mixed grouping separators")
    separator = thousands if thousands in whole else " " if " " in whole else None
    if separator:
        groups = whole.split(separator)
        if not (1 <= len(groups[0]) <= 3 and groups[0].isdigit()
                and all(len(g) == 3 and g.isdigit() for g in groups[1:])):
            raise ValueError("Invalid thousands grouping")
        whole = "".join(groups)
    if not whole.isdigit() or (len(pieces) == 2 and not pieces[1].isdigit()):
        raise ValueError("Invalid numeric weight")
    number = Decimal(whole + ("." + pieces[1] if len(pieces) == 2 else ""))
    if not Decimal(settings["min_kg"]) <= number <= Decimal(settings["max_kg"]):
        raise ValueError("Weight outside configured plausibility limits")
    units = number * 1000000  # integer milligrams avoids accumulation rounding
    if units != units.to_integral_value():
        raise ValueError("More than six decimal places")
    return int(units)


def resolve_player(label, players):
    """Map nameplate OCR to a unique registered username; ties stay unresolved."""
    from difflib import SequenceMatcher
    compact = lambda text: re.sub(r"[^a-z0-9_]", "", str(text).lower())
    observed = compact(label)
    scores = {}
    exact = set()
    for player in players:
        best = 0
        for name in [player["username"], *player.get("aliases", [])]:
            expected = compact(name)
            if not expected:
                continue
            if observed == expected:
                exact.add(player["username"])
                score = 1.0
            elif expected in observed and len(observed)-len(expected) <= 2:
                score = .98 - .01*(len(observed)-len(expected))
            elif len(expected) >= 6 and abs(len(expected)-len(observed)) <= 2:
                score = SequenceMatcher(None,observed,expected).ratio()
                if score < .92:
                    score = 0
            else:
                score = 0
            best = max(best,score)
        if best:
            scores[player["username"]] = best
    if exact:
        return next(iter(exact)) if len(exact)==1 else None
    ranked = sorted(scores.items(),key=lambda item:item[1],reverse=True)
    if not ranked or (len(ranked)>1 and ranked[0][1]-ranked[1][1] < .04):
        return None
    return ranked[0][0]


def resolve_correction(label, corrections):
    compact = lambda text: re.sub(r"[^a-z0-9_]", "", str(text).lower())
    observed = compact(label)
    # Exact canonical names take priority over OCR icon cleanup.
    for target in corrections.values():
        if compact(target)==observed:
            return target
    cleaned = re.sub(r"^[A-Za-z]\s+(?=[A-Za-z0-9_]{3})", "", str(label).strip())
    candidates = {observed, compact(cleaned)}
    matches = {target for source,target in corrections.items() if compact(source) in candidates}
    return next(iter(matches)) if len(matches)==1 else None


def parse_catch(raw, confidence, visual_confidence, settings):
    normalized = " ".join(unicodedata.normalize("NFKC", raw).split())
    # Overlapping colored OCR boxes can repeat the System closing bracket. Only
    # normalize this known UI delimiter; never guess username letters or digits.
    normalized = re.sub(r"^(\[\s*System\s*\])(?:\s*\])+\s*",r"\1 ",normalized,flags=re.IGNORECASE)
    roster = settings.get("registered_players", [])
    corrections = settings.get("name_corrections", {})
    canonical = None
    header = re.match(r"^(\[\s*System\s*\]\s*)(.*?)\s*has\b(.*)$", normalized, re.IGNORECASE)
    if header:
        canonical = resolve_correction(header[2],corrections) or (resolve_player(header[2],roster) if roster else None)
        if canonical:
            normalized = header[1]+canonical+" has"+header[3]
    if settings.get("best_effort", False):
        # Nameplates may include display-name spaces, icon letters and vertical strokes.
        header = re.match(r"^(\[\s*System\s*\]\s*)(.*?)\s+has\b(.*)$", normalized, re.IGNORECASE)
        if header:
            name = re.sub(r"\s+[Il|]$", "", header[2]).strip()
            name = re.sub(r"^[A-Za-z]\s+(?=[A-Za-z0-9_]{3})", "", name)
            normalized = header[1] + name.replace(" ", "") + " has" + header[3]
        normalized = re.sub(r"(\bhas)\s+s\s+(?=(?:perfectly\s*)?caught)",r"\1 ",normalized,flags=re.IGNORECASE)
        normalized = re.sub(r"(\bcaught)\s+t\s+(?=an?\b)", r"\1 ", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"(\bcaught\s*an?\s*)[(:'\";?]+\s*",r"\1",normalized,flags=re.IGNORECASE)
        normalized = re.sub(r"(\b(?:Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine))\s*[(:'\";?&]+\s*",r"\1 ",normalized,flags=re.IGNORECASE)
        normalized = re.sub(r"(\b(?:Common|Uncommon|Unusual|Rare|Legendary|Mythical|Exotic|Secret|Limited|Divine))\s+[A-Za-z|]\s+(?=\d)",r"\1 ",normalized,flags=re.IGNORECASE)
    matches = [m.groupdict() for p in settings["patterns"]
               if (m := re.fullmatch(p, normalized, re.IGNORECASE))]
    # Overlapping named patterns may extract the same catch. Keep disagreements
    # ambiguous, but do not send identical extractions to review.
    matches = [dict(fields) for fields in dict.fromkeys(tuple(m.items()) for m in matches)]
    result = {"username": "", "fish": "", "rarity": "", "weight_units": 0,
              "status": "review", "reason": "No configured announcement pattern matched",
              "raw_text": raw, "confidence": float(confidence),
              "visual_confidence": float(visual_confidence)}
    if len(matches) != 1:
        if len(matches) > 1:
            result["reason"] = "Ambiguous parser matches"
        return result
    fields = matches[0]
    if canonical:
        fields["username"] = canonical
    try:
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", fields["username"]):
            raise ValueError("Invalid Roblox username")
        if not fields["fish"].strip() or not fields["rarity"].strip():
            raise ValueError("Missing fish or rarity")
        result.update({k: fields[k].strip() for k in ("username", "fish", "rarity")})
        try:
            result["weight_units"] = parse_weight(fields["weight"], settings)
        except ValueError:
            # Only best-effort mode drops short detached OCR tokens before a complete
            # decimal weight. Valid thousands grouping is always preserved above.
            weight = re.fullmatch(r"(?:[0-9]{1,2}\s+)+([0-9]+[.,][0-9]+)", fields["weight"].strip())
            if not settings.get("best_effort", False) or not weight:
                raise
            result["weight_units"] = parse_weight(weight[1], settings)
        if settings.get("best_effort", False):
            result.update(status="confirmed", reason="Best-effort live score")
        elif not settings["parser_verified"]:
            result["reason"] = "Parser not verified against real Fisch samples"
        elif confidence < settings["confidence"]:
            result["reason"] = "Low OCR confidence"
        elif visual_confidence < 0.9:
            result["reason"] = "Uncertain visual insertion"
        else:
            result.update(status="confirmed", reason="")
    except (ValueError, KeyError, InvalidOperation) as exc:
        result["reason"] = str(exc)
    return result


class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS tournaments(
              id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
              created TEXT NOT NULL, finished TEXT);
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, visual_id TEXT NOT NULL UNIQUE,
              tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
              username TEXT NOT NULL, fish TEXT NOT NULL, rarity TEXT NOT NULL,
              weight_units INTEGER NOT NULL, timestamp TEXT NOT NULL,
              raw_text TEXT NOT NULL, confidence REAL NOT NULL,
              visual_confidence REAL NOT NULL, status TEXT NOT NULL,
              reason TEXT NOT NULL, evidence TEXT);
            CREATE TABLE IF NOT EXISTS kicked_players(
              tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
              username TEXT NOT NULL COLLATE NOCASE,
              PRIMARY KEY(tournament_id,username));
            CREATE INDEX IF NOT EXISTS event_session ON events(tournament_id,status);
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, actor TEXT NOT NULL,
              action TEXT NOT NULL, event_id INTEGER, before_json TEXT, after_json TEXT);
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(tournaments)")}
            for column, definition in (("scheduled_start", "REAL"), ("started_at", "REAL"), ("elapsed_seconds", "REAL NOT NULL DEFAULT 0")):
                if column not in columns:
                    db.execute(f"ALTER TABLE tournaments ADD COLUMN {column} {definition}")
            if db.execute("SELECT value FROM metadata WHERE key='active'").fetchone() is None:
                self._create(db, "Fisch Weight Championship")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _active(self, db):
        row = db.execute("SELECT value FROM metadata WHERE key='active'").fetchone()
        return dict(db.execute("SELECT * FROM tournaments WHERE id=?", (int(row[0]),)).fetchone())

    def active(self):
        with self.connection() as db:
            return self._active(db)

    def _audit(self, db, actor, action, event_id=None, before=None, after=None):
        db.execute("INSERT INTO audit(timestamp,actor,action,event_id,before_json,after_json) VALUES(?,?,?,?,?,?)",
                   (now(), actor, action, event_id, json.dumps(before), json.dumps(after)))

    def _create(self, db, name):
        cursor = db.execute("INSERT INTO tournaments(name,status,created) VALUES(?,?,?)", (name, "ready", now()))
        db.execute("INSERT OR REPLACE INTO metadata VALUES('active',?)", (str(cursor.lastrowid),))
        self._audit(db, "admin", "create", after={"id": cursor.lastrowid, "name": name})
        return cursor.lastrowid

    def transition(self, action, name="Fisch Weight Championship", countdown_seconds=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = self._active(db)
            clock = time.time()
            elapsed = active["elapsed_seconds"] + (max(0, clock-active["started_at"]) if active["started_at"] is not None and active["status"] == "running" else 0)
            if action == "schedule":
                if active["status"] != "ready":
                    raise ValueError("Create a ready tournament before scheduling its start")
                if isinstance(countdown_seconds, bool) or not isinstance(countdown_seconds, (int, float)) or not 1 <= countdown_seconds <= 86400:
                    raise ValueError("Start countdown must be between 1 and 86400 seconds")
                db.execute("UPDATE tournaments SET scheduled_start=? WHERE id=?", (clock+countdown_seconds,active["id"]))
                self._audit(db,"admin","schedule",before=active,after=self._active(db))
                return
            if action == "cancel_countdown":
                if active["status"] != "ready":
                    raise ValueError("No pending start countdown")
                db.execute("UPDATE tournaments SET scheduled_start=NULL WHERE id=?", (active["id"],))
                self._audit(db,"admin","cancel_countdown",before=active,after=self._active(db))
                return
            if action in ("reset", "create"):
                db.execute("UPDATE tournaments SET elapsed_seconds=?,started_at=NULL,scheduled_start=NULL WHERE id=?", (elapsed,active["id"]))
                db.execute("UPDATE tournaments SET status='finished',finished=? WHERE id=? AND status!='finished'", (now(), active["id"]))
                self._create(db, name.strip()[:120] or "Fisch Weight Championship")
            else:
                allowed = {"start": ("ready", "running"), "pause": ("running", "paused"),
                           "resume": ("paused", "running"), "finish": (("ready", "running", "paused"), "finished")}
                if action not in allowed:
                    raise ValueError("Unknown tournament action")
                source, target = allowed[action]
                if active["status"] not in (source if isinstance(source, tuple) else (source,)):
                    raise ValueError(f"Cannot {action} a {active['status']} tournament")
                db.execute("UPDATE tournaments SET status=?,finished=?,scheduled_start=NULL,started_at=?,elapsed_seconds=? WHERE id=?",
                           (target, now() if target == "finished" else None,
                            clock if target == "running" else None, 0 if action == "start" else elapsed, active["id"]))
                self._audit(db, "admin", action, before=active, after=self._active(db))

    def insert(self, fields, visual_id, tournament_id, timestamp, evidence=None, actor="ocr"):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            fields = dict(fields)
            if db.execute("SELECT 1 FROM kicked_players WHERE tournament_id=? AND username=?", (tournament_id,fields["username"])).fetchone():
                fields.update(status="disqualified",reason="Kicked from scoreboard")
            # The capture assigns the session at acquisition, never at OCR completion.
            keys = ("username", "fish", "rarity", "weight_units", "raw_text", "confidence", "visual_confidence", "status", "reason")
            # Suppress rapid re-reads, including minor fish-text OCR variations.
            # Equal catches outside this short window remain separate events.
            if actor == "ocr" and fields.get("reason") == "Best-effort live score":
                duplicate = db.execute("""SELECT id FROM events WHERE tournament_id=?
                    AND status='confirmed' AND username=? COLLATE NOCASE
                    AND rarity=? COLLATE NOCASE AND weight_units=?
                    AND ABS((julianday(timestamp)-julianday(?))*86400) < 3
                    LIMIT 1""", (tournament_id, fields["username"], fields["rarity"],
                                fields["weight_units"], timestamp)).fetchone()
                if duplicate:
                    return None
            cursor = db.execute("""INSERT OR IGNORE INTO events(visual_id,tournament_id,timestamp,evidence,
                username,fish,rarity,weight_units,raw_text,confidence,visual_confidence,status,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (visual_id, tournament_id, timestamp, evidence, *(fields[k] for k in keys)))
            if cursor.rowcount:
                self._audit(db, actor, "add", cursor.lastrowid, after=fields)
                return cursor.lastrowid
            return None

    def correct(self, event_id, changes, settings, delete=False):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("Event not found")
            before = dict(row)
            after = before.copy()
            if delete:
                after.update(status="deleted", reason=str(changes.get("reason", "Operator removed event"))[:500])
            else:
                for key in ("username", "fish", "rarity", "status", "reason"):
                    if key in changes:
                        after[key] = str(changes[key]).strip()[:500]
                if "weight_kg" in changes:
                    after["weight_units"] = parse_weight(str(changes["weight_kg"]), settings)
                if after["status"] not in ("confirmed", "review", "disqualified"):
                    raise ValueError("Invalid status")
                if after["status"] == "confirmed":
                    validate_manual(after, settings)
                    if db.execute("SELECT 1 FROM kicked_players WHERE tournament_id=? AND username=?",(after["tournament_id"],after["username"])).fetchone():
                        raise ValueError("Player was kicked from this tournament")
            db.execute("UPDATE events SET username=?,fish=?,rarity=?,weight_units=?,status=?,reason=? WHERE id=?",
                       (*(after[k] for k in ("username", "fish", "rarity", "weight_units", "status", "reason")), event_id))
            self._audit(db, "admin", "delete" if delete else "correct", event_id, before, after)

    def kick_player(self, username):
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}",username):
            raise ValueError("Invalid player username")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = self._active(db)
            db.execute("INSERT OR IGNORE INTO kicked_players VALUES(?,?)",(active["id"],username))
            rows = db.execute("SELECT * FROM events WHERE tournament_id=? AND username=? COLLATE NOCASE AND status IN ('confirmed','review')",(active["id"],username)).fetchall()
            for row in rows:
                before = dict(row)
                after = before | {"status":"disqualified","reason":"Kicked from scoreboard"}
                db.execute("UPDATE events SET status='disqualified',reason='Kicked from scoreboard' WHERE id=?",(row["id"],))
                self._audit(db,"admin","kick",row["id"],before,after)
            self._audit(db,"admin","kick_player",after={"tournament_id":active["id"],"username":username})

    def reconcile_players(self, players, corrections=None):
        corrections = corrections or {}
        if not players and not corrections:
            return
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = self._active(db)
            for kicked in db.execute("SELECT username FROM kicked_players WHERE tournament_id=?",(active["id"],)).fetchall():
                target = resolve_correction(kicked[0],corrections)
                if target:
                    db.execute("INSERT OR IGNORE INTO kicked_players VALUES(?,?)",(active["id"],target))
            rows = db.execute("SELECT * FROM events WHERE tournament_id=? AND username!=''",(active["id"],)).fetchall()
            for row in rows:
                header = re.match(r"^\[\s*System\s*\]\s*(.*?)\s*has\b",row["raw_text"],re.IGNORECASE)
                canonical = resolve_correction(row["username"],corrections) or resolve_correction(header[1] if header else row["username"],corrections) or (resolve_player(header[1] if header else row["username"],players) if players else None)
                if not canonical or canonical == row["username"]:
                    continue
                after = dict(row) | {"username":canonical}
                if db.execute("SELECT 1 FROM kicked_players WHERE tournament_id=? AND username=?",(active["id"],canonical)).fetchone():
                    after.update(status="disqualified",reason="Kicked from scoreboard")
                db.execute("UPDATE events SET username=?,status=?,reason=? WHERE id=?",(canonical,after["status"],after["reason"],row["id"]))
                self._audit(db,"admin","resolve_player",row["id"],dict(row),after)

    def state(self):
        with self.connection() as db:
            db.execute("BEGIN")
            active = self._active(db)
            rows = db.execute("""SELECT username,SUM(weight_units) total,COUNT(*) catches,
                MAX(weight_units) largest FROM events WHERE tournament_id=? AND status='confirmed'
                GROUP BY username COLLATE NOCASE ORDER BY total DESC,username COLLATE NOCASE""", (active["id"],)).fetchall()
            leaderboard = []
            previous, rank = None, 0
            for i, row in enumerate(rows, 1):
                if row["total"] != previous:
                    rank = i
                previous = row["total"]
                leaderboard.append({"rank": rank, "username": row["username"], "total_kg": row["total"] / 1000000,
                                    "catches": row["catches"], "largest_kg": row["largest"] / 1000000,
                                    "average_kg": row["total"] / row["catches"] / 1000000})
            counts = dict(db.execute("SELECT status,COUNT(*) FROM events WHERE tournament_id=? GROUP BY status", (active["id"],)).fetchall())
            last = db.execute("SELECT * FROM events WHERE tournament_id=? AND status='confirmed' ORDER BY id DESC LIMIT 1", (active["id"],)).fetchone()
            return {"tournament": active, "leaderboard": leaderboard, "counts": counts,
                    "recent": self.event_json(last) if last else None,
                    "kicked_players": [r[0] for r in db.execute("SELECT username FROM kicked_players WHERE tournament_id=? ORDER BY username",(active["id"],))]}

    @staticmethod
    def event_json(row):
        result = dict(row)
        result["weight_kg"] = result["weight_units"] / 1000000
        return result

    def events(self, tournament_id=None, limit=50, offset=0):
        with self.connection() as db:
            session = tournament_id if tournament_id is not None else self._active(db)["id"]
            rows = db.execute("SELECT * FROM events WHERE tournament_id=? ORDER BY id DESC LIMIT ? OFFSET ?", (session, limit, offset)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM events WHERE tournament_id=?", (session,)).fetchone()[0]
            return {"items": [self.event_json(r) for r in rows], "total": count, "offset": offset, "limit": limit}

    def history(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM tournaments ORDER BY id DESC")]

    def audits(self, event_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM audit WHERE event_id=? ORDER BY id", (event_id,))]


def validate_manual(fields, settings):
    if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", fields["username"]):
        raise ValueError("Username must have 3–20 letters, digits, or underscores")
    if not fields["fish"] or not fields["rarity"]:
        raise ValueError("Fish and rarity required")
    if not Decimal(settings["min_kg"]) * 1000000 <= fields["weight_units"] <= Decimal(settings["max_kg"]) * 1000000:
        raise ValueError("Weight outside plausibility limits")


def glyph_mask(frame):
    """Keep bright/saturated glyphs; discard most changing 3D backgrounds.

    Chat still needs a stable, dark backdrop. Background motion triggers review.
    """
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = ((hsv[:, :, 2] > 170) & ((hsv[:, :, 1] < 100) | (hsv[:, :, 2] > 220))).astype(np.uint8)
    return mask


def row_bands(mask):
    active = np.flatnonzero(mask.sum(axis=1) >= max(4, mask.shape[1] * .008))
    if not len(active):
        return []
    groups = np.split(active, np.where(np.diff(active) > 3)[0] + 1)
    return [(max(0, int(g[0]) - 2), min(mask.shape[0], int(g[-1]) + 3)) for g in groups if len(g) >= 3]


def visual_difference(a, b):
    return np.count_nonzero(a != b) / max(1, np.count_nonzero(a | b))


def ocr_text_rows(result):
    """Group color/font fragments by vertical overlap, then read left-to-right.

    Sorting raw box centers alone can put '[System]' AFTER a username or place
    a bold fish name before its italic modifier on the same physical row.
    """
    boxes = []
    for polygon, text, confidence in result or []:
        points = np.asarray(polygon)
        boxes.append({"left": float(points[:,0].min()), "top": float(points[:,1].min()),
                      "bottom": float(points[:,1].max()), "text": text, "confidence": float(confidence)})
    rows = []
    for box in sorted(boxes, key=lambda b: (b["top"],b["left"])):
        matches = []
        for row in rows:
            overlap = max(0, min(row["bottom"],box["bottom"])-max(row["top"],box["top"]))
            fraction = overlap/max(1, min(row["bottom"]-row["top"],box["bottom"]-box["top"]))
            if fraction >= .45:
                matches.append((fraction,row))
        if matches:
            row = max(matches,key=lambda pair:pair[0])[1]
            row["boxes"].append(box)
            row["top"] = min(row["top"],box["top"])
            row["bottom"] = max(row["bottom"],box["bottom"])
        else:
            rows.append({"top":box["top"],"bottom":box["bottom"],"boxes":[box]})
    return [(" ".join(b["text"] for b in sorted(row["boxes"],key=lambda b:b["left"])),
             min(b["confidence"] for b in row["boxes"])) for row in sorted(rows,key=lambda r:r["top"])]


def ocr_input(image):
    scaled = cv2.resize(image,None,fx=2,fy=2,interpolation=cv2.INTER_CUBIC)
    return cv2.copyMakeBorder(scaled,12,12,12,12,cv2.BORDER_CONSTANT,value=(0,0,0))


class VisualTracker:
    """Pixel registration, then insertion bands. No OCR-text deduplication.

    Two stable frames are required before updating a baseline. Old rows must
    match above an insertion. Large redraws are review-only and re-baselined.
    A periodic screen that is pixel-identical is fundamentally unobservable.
    """
    def __init__(self):
        self.previous = None
        self.settling = None
        self.stable = 0
        self.counter = 0

    def reset(self):
        self.previous = self.settling = None
        self.stable = 0

    def feed(self, frame):
        mask = glyph_mask(frame)
        if self.settling is None or self.settling.shape != mask.shape or visual_difference(self.settling, mask) > .08:
            self.settling = mask.copy()
            self.stable = 1
            return [], None
        self.stable += 1
        if self.stable < 2:
            return [], None
        if self.previous is None:
            self.previous = mask.copy()
            return [], "Baseline established; visible messages excluded"
        previous = self.previous
        if visual_difference(previous, mask) < .04:
            return [], None
        h, w = mask.shape
        candidates = []
        old_bands = row_bands(previous)
        old_bottom = old_bands[-1][1] if old_bands else 0
        # Score only foreground-union pixels, so an empty background cannot match.
        for shift in range(0, int(h * .8)):
            overlap = old_bottom - shift
            if overlap < 12:
                continue
            old, new = previous[shift:old_bottom], mask[:overlap]
            union = np.count_nonzero(old | new)
            if union < 30:
                continue
            mismatch = np.count_nonzero(old != new) / union
            if mismatch < .18:
                candidates.append((mismatch, shift))
        bands = row_bands(mask)
        self.previous = mask.copy()
        if not bands:
            return [], "Chat cleared/obstructed; re-baselined"
        if not candidates:
            self.counter += 1
            return [(frame.copy(), .35, self.counter)], "No reliable row overlap: possible loss or obstruction; review only"
        candidates.sort()
        score, shift = candidates[0]
        # Distinct, similarly good translations imply periodic/identical rows.
        ambiguous = any(abs(s-shift) > 4 and e <= score + .015 for e, s in candidates[1:])
        if shift == 0:
            bottom = old_bands[-1][1] if old_bands else 0
            fresh = [(a, b) for a, b in bands if a >= bottom - 1]
        else:
            fresh = [(a, b) for a, b in bands if a >= old_bottom - shift - 1]
        if not fresh:
            return [], "Minor redraw/movement; no new bottom rows"
        start = fresh[0][0]
        self.counter += 1
        confidence = .6 if ambiguous else (1.0 if score < .06 else .8)
        return [(frame[start:fresh[-1][1]].copy(), confidence, self.counter)], (
            "Ambiguous scrolling: review required" if ambiguous else None)


class Runtime:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.lock = threading.RLock()
        self.health = {"server": "starting", "capture": "stopped", "ocr": "idle", "warnings": [],
                       "frame_overflows": 0, "ocr_overflows": 0, "last_frame": None,
                       "last_ocr": None, "preview": "", "monitoring": False}
        self.revision = 0
        self.stop_event = threading.Event()
        self.monitor_stop = threading.Event()
        self.cancel_work = threading.Event()
        self.restart_pending = False
        self.restart_thread = None
        self.auto_monitor = True
        self.threads = []
        self.control_lock = threading.RLock()
        self.server = None
        self.engine = None
        self.session_epoch = 0
        self.track_done = threading.Event()
        self.ocr_lock = threading.RLock()
        self.preview_frame = None
        self.preview_thread = None

    def update_health(self, **values):
        with self.lock:
            self.health.update(values)

    def warn(self, message):
        logging.warning(message)
        with self.lock:
            warnings = self.health["warnings"]
            if not warnings or warnings[-1]["message"] != message:
                warnings.append({"timestamp": now(), "message": message})
                del warnings[:-30]

    def changed(self):
        with self.lock:
            self.revision += 1

    def state(self):
        state = self.store.state()
        with self.lock:
            state.update(health=json.loads(json.dumps(self.health)), revision=self.revision)
        settings = self.config.snapshot()
        state["overlay"] = settings["overlay"]
        state["best_effort"] = settings.get("best_effort", False)
        state["server_time"] = time.time()
        state["rules"] = settings.get("rules", "")
        return state

    def initialize_ocr(self):
        from rapidocr_onnxruntime import RapidOCR
        import rapidocr_onnxruntime
        # Package-relative paths work in source and PyInstaller's _internal folder.
        models = Path(rapidocr_onnxruntime.__file__).parent / "models"
        def model(kind):
            paths = sorted(models.glob(f"*{kind}*.onnx"))
            if len(paths) != 1:
                raise RuntimeError(f"Bundled {kind} model missing or ambiguous in {models}")
            return str(paths[0])
        self.engine = RapidOCR(det_model_path=model("det"), cls_model_path=model("cls"), rec_model_path=model("rec"),
                               intra_op_num_threads=2, inter_op_num_threads=1)

    def preview_image(self, image):
        """Read the full crop for diagnostics only. Never inserts database events."""
        with self.ocr_lock:
            if self.engine is None:
                self.initialize_ocr()
            result, _ = self.engine(ocr_input(image))
        rows = ocr_text_rows(result)
        self.update_health(preview="\n".join(text for text,confidence in rows) or "No text recognized in the selected capture. Check the region and Roblox visibility.",
                           last_ocr=now())
        if self.config.snapshot()["diagnostic"]:
            directory = self.config.directory / "evidence"
            directory.mkdir(exist_ok=True)
            cv2.imwrite(str(directory / "latest-preview.png"), image)

    def request_preview(self):
        if self.preview_thread and self.preview_thread.is_alive():
            return
        region = self.config.snapshot()["region"]
        if not region:
            raise ValueError("Select a chat region first")
        def work():
            try:
                import mss
                self.update_health(preview="Capturing and reading the selected region…")
                with mss.mss() as capture:
                    image = np.array(capture.grab(dict(zip(("left","top","width","height"),region))))[:, :, :3].copy()
                self.preview_image(image)
            except Exception as exc:
                logging.exception("Diagnostic preview failed")
                self.update_health(preview=f"Preview failed: {exc}")
                self.warn(f"Diagnostic preview failed: {exc}")
        self.preview_thread = threading.Thread(target=work, name="preview", daemon=True)
        self.preview_thread.start()

    def start_monitoring(self):
        with self.control_lock:
            if any(t.is_alive() for t in self.threads):
                if not self.monitor_stop.is_set():
                    return  # Start is idempotent.
                self.restart_pending = True
                self.update_health(monitoring=False, capture="Restart requested; finishing current OCR call")
                if not self.restart_thread or not self.restart_thread.is_alive():
                    self.restart_thread = threading.Thread(target=self.finish_restart, name="capture-restart", daemon=True)
                    self.restart_thread.start()
                return
            settings = self.config.snapshot()
            region = settings["region"]
            if not region or region[2] < 60 or region[3] < 30:
                raise ValueError("Select a valid chat region first")
            import mss
            with mss.mss() as capture:
                if not any(region[0] >= m["left"] and region[1] >= m["top"] and
                           region[0]+region[2] <= m["left"]+m["width"] and
                           region[1]+region[3] <= m["top"]+m["height"] for m in capture.monitors[1:]):
                    raise ValueError("Region is outside a connected monitor; select it again")
            self.restart_pending = False
            self.cancel_work = threading.Event()
            self.monitor_stop = threading.Event()
            self.track_done = threading.Event()
            self.frames = queue.Queue(settings["frame_queue"])
            self.candidates = queue.Queue(settings["ocr_queue"])
            self.run_id = uuid.uuid4().hex
            self.update_health(monitoring=True, capture="starting", ocr="loading bundled models")
            self.threads = [threading.Thread(target=fn, name=name, daemon=True) for fn, name in
                            ((lambda: self.guarded(self.capture_loop), "capture"),
                             (lambda: self.guarded(self.track_loop, tracking=True), "tracking"),
                             (lambda: self.guarded(self.ocr_loop), "ocr"))]
            for t in self.threads:
                t.start()

    def guarded(self, function, tracking=False):
        try:
            function()
        except Exception as exc:
            logging.exception("Worker failed")
            self.warn(f"{threading.current_thread().name} worker failed: {exc}; monitoring stopped")
            self.monitor_stop.set()
            self.update_health(monitoring=False)
        finally:
            if tracking:
                self.track_done.set()

    def finish_restart(self):
        while not self.stop_event.is_set():
            with self.control_lock:
                if not self.restart_pending:
                    return
                workers = list(self.threads)
                if not any(t.is_alive() for t in workers):
                    try:
                        self.start_monitoring()
                    except Exception as exc:
                        self.restart_pending = False
                        self.update_health(monitoring=False, capture=f"Restart failed: {exc}")
                        self.warn(f"Monitoring restart failed: {exc}")
                    return
            self.stop_event.wait(.1)

    def stop_monitoring(self, wait=False):
        with self.control_lock:
            self.restart_pending = False
            self.monitor_stop.set()
            self.cancel_work.set()
            # Discard backlog rather than spending minutes processing stale images.
            for work_queue in (getattr(self,"frames",None),getattr(self,"candidates",None)):
                if work_queue is not None:
                    while True:
                        try:
                            work_queue.get_nowait()
                            work_queue.task_done()
                        except queue.Empty:
                            break
            with self.lock:
                self.preview_frame = None
            self.update_health(monitoring=False, capture="Stopped; current OCR call may still be finishing")
        if wait:
            deadline = time.monotonic()+5
            for t in self.threads:
                t.join(timeout=max(0,deadline-time.monotonic()))

    def monitoring_action(self, action):
        with self.control_lock:
            if action == "start":
                self.start_monitoring()
            elif action == "stop":
                self.stop_monitoring()
            elif action == "restart":
                self.stop_monitoring()
                self.start_monitoring()
            elif action == "preview":
                self.request_preview()
            else:
                raise ValueError("Unknown monitoring action")
        self.changed()

    def tournament_action(self, action, name="Fisch Weight Championship", countdown_seconds=None):
        with self.control_lock:
            if action in ("start", "resume", "schedule") and self.auto_monitor:
                self.start_monitoring()
            # A monotonic epoch tags frames. Changes invalidate old visual baselines.
            self.store.transition(action, name, countdown_seconds)
            self.session_epoch += 1
            self.changed()

    def timer_tick(self):
        with self.control_lock:
            active = self.store.active()
            if active["status"] == "ready" and active["scheduled_start"] is not None and time.time() >= active["scheduled_start"]:
                self.tournament_action("start")
                return True
        return False

    def timer_loop(self):
        while not self.stop_event.wait(.1):
            try:
                self.timer_tick()
            except Exception:
                logging.exception("Tournament timer failed")

    def capture_loop(self):
        import mss
        settings = self.config.snapshot()
        l, t, w, h = settings["region"]
        last_mask, last_change = None, time.monotonic()
        last_preview = 0
        overflow_generation = 0
        try:
            with mss.mss() as capture:
                while not self.monitor_stop.is_set():
                    began = time.monotonic()
                    # Serialize acquisition against tournament transitions.
                    with self.control_lock:
                        session = self.store.active()
                        session_epoch = self.session_epoch
                        frame = np.array(capture.grab({"left": l, "top": t, "width": w, "height": h}))[:, :, :3].copy()
                    mask = glyph_mask(frame)
                    if last_mask is None or np.mean(mask != last_mask) > .003:
                        last_change = began
                    last_mask = mask
                    status = "capturing"
                    if not np.any(mask):
                        status = "No visible chat glyphs; check Roblox visibility/obstruction"
                    elif began-last_change > 30:
                        status = "Unchanged >30s: may be idle, hidden, or capture stalled"
                    self.update_health(capture=status, last_frame=now())
                    if began - last_preview >= 3:
                        with self.lock:
                            self.preview_frame = frame
                        last_preview = began
                    item = (frame, session["id"], session["status"], now(), overflow_generation, session_epoch)
                    try:
                        self.frames.put_nowait(item)
                    except queue.Full:
                        overflow_generation += 1
                        with self.lock:
                            self.health["frame_overflows"] += 1
                        self.warn("Frame buffer overflow; visual events may be missing. Re-baselining.")
                    self.monitor_stop.wait(max(0, 1/settings["fps"]-(time.monotonic()-began)))
        except Exception as exc:
            logging.exception("Capture failed")
            self.warn(f"Capture failure: {exc}")
            self.update_health(capture="failed", monitoring=False)
            self.monitor_stop.set()

    def track_loop(self):
        tracker = VisualTracker()
        epoch = None
        while not self.cancel_work.is_set() and (not self.monitor_stop.is_set() or not self.frames.empty()):
            try:
                frame, session, status, stamp, generation, session_epoch = self.frames.get(timeout=.2)
            except queue.Empty:
                continue
            current_epoch = (session, status, generation, session_epoch)
            if current_epoch != epoch:
                tracker.reset()
                epoch = current_epoch
            candidates, warning = tracker.feed(frame)
            if warning and "Baseline" not in warning and "Minor" not in warning:
                self.warn(warning)
            if status != "running":
                self.frames.task_done()
                continue
            for image, confidence, sequence in candidates:
                item = (image, confidence, f"{self.run_id}:{session}:{sequence}", session, stamp)
                try:
                    self.candidates.put_nowait(item)
                except queue.Full:
                    with self.lock:
                        self.health["ocr_overflows"] += 1
                    self.warn("OCR queue overflow: insertion evidence saved for manual recovery")
                    self.save_evidence(image, item[2], force=True)
            self.frames.task_done()
        self.track_done.set()

    def save_evidence(self, image, event_id, force=False):
        if not force and not self.config.snapshot()["diagnostic"]:
            return None
        directory = self.config.directory / "evidence"
        directory.mkdir(exist_ok=True)
        path = directory / (event_id.replace(":", "_") + ".png")
        if not cv2.imwrite(str(path), image):
            raise RuntimeError("Cannot write screenshot evidence")
        return path.name

    def ocr_loop(self):
        try:
            with self.ocr_lock:
                if self.engine is None:
                    self.initialize_ocr()
            self.update_health(ocr="ready")
        except Exception as exc:
            self.warn(f"OCR initialization failed: {exc}")
            self.update_health(ocr="failed", monitoring=False)
            self.monitor_stop.set()
            return
        # Wrapped rows can arrive in adjacent visual insertions. Keep an incomplete
        # fragment briefly; complete matches are never joined to another message.
        pending = None
        while not self.cancel_work.is_set() and (not self.track_done.is_set() or not self.candidates.empty()):
            try:
                item = self.candidates.get(timeout=.2)
            except queue.Empty:
                if pending and time.monotonic()-pending["since"] > 1.5:
                    self.persist_ocr(pending)
                    pending = None
                with self.lock:
                    preview = self.preview_frame
                    self.preview_frame = None
                if preview is not None:
                    try:
                        self.preview_image(preview)
                    except Exception as exc:
                        self.warn(f"Periodic OCR preview failed: {exc}")
                continue
            image, visual, identity, session, stamp = item
            settings = self.config.snapshot()
            evidence = None
            try:
                evidence = self.save_evidence(image, identity, force=visual < .9)
                scaled = ocr_input(image)
                with self.ocr_lock:
                    result, _ = self.engine(scaled)
                if self.cancel_work.is_set():
                    continue
                lines = ocr_text_rows(result)
                groups = []
                for text, score in lines:
                    if groups and not re.search(settings["message_start"], text, re.IGNORECASE):
                        groups[-1][0] += " " + text
                        groups[-1][1] = min(groups[-1][1], float(score))
                    else:
                        groups.append([text, float(score)])
                if not groups:
                    groups = [["[OCR returned no text]", 0.0]]
                for index, (text, score) in enumerate(groups):
                    piece = {"text": text, "confidence": score, "visual": visual, "identity": f"{identity}:{index}",
                             "session": session, "stamp": stamp, "evidence": evidence, "since": time.monotonic()}
                    if pending:
                        if pending["session"] == session and not re.search(settings["message_start"], text, re.IGNORECASE):
                            piece["text"] = pending["text"] + " " + text
                            piece["confidence"] = min(score, pending["confidence"])
                            piece["visual"] = min(visual, pending["visual"])
                            piece["identity"] = pending["identity"]
                            piece["stamp"] = pending["stamp"]
                        else:
                            self.persist_ocr(pending)
                        pending = None
                    parsed = parse_catch(piece["text"], piece["confidence"], piece["visual"], settings)
                    if parsed["status"] == "review" and not piece["evidence"]:
                        piece["evidence"] = self.save_evidence(image, identity, force=True)
                    if parsed["weight_units"] > 0 or index < len(groups)-1:
                        self.persist_ocr(piece)
                    else:
                        pending = piece
                self.update_health(ocr="ready", last_ocr=now(), preview="\n".join(g[0] for g in groups))
            except Exception as exc:
                logging.exception("OCR candidate failed")
                self.warn(f"OCR failure: {exc}; event held for review")
                evidence = evidence or self.save_evidence(image, identity, force=True)
                self.persist_ocr({"text": f"[OCR failure: {exc}]", "confidence": 0, "visual": visual,
                                  "identity": identity, "session": session, "stamp": stamp, "evidence": evidence})
            finally:
                self.candidates.task_done()
        if pending and not self.cancel_work.is_set():
            self.persist_ocr(pending)
        self.update_health(ocr="idle")

    def persist_ocr(self, piece):
        with self.control_lock:
            if self.cancel_work.is_set():
                return
            fields = parse_catch(piece["text"], piece["confidence"], piece["visual"], self.config.snapshot())
            self.store.insert(fields, piece["identity"], piece["session"], piece["stamp"], piece.get("evidence"))
            self.changed()

    def close(self):
        self.stop_event.set()
        self.stop_monitoring(wait=True)
        if self.preview_thread:
            self.preview_thread.join(timeout=15)
        if self.server:
            self.server.should_exit = True


WEB_STYLE = """
*{box-sizing:border-box}body{margin:0;font-family:Segoe UI,Arial,sans-serif;color:#edfaff}
button,input,select,textarea{font:inherit;border-radius:7px;border:1px solid #345266;padding:9px;background:#112735;color:#edfaff}
button{cursor:pointer;background:#155d69}button:hover{background:#207c89}h1,h2{margin:12px 0}.muted{color:#9db6c5}
.panel{background:#112735;border:1px solid #28485a;border-radius:14px;padding:18px;margin:14px 0}
table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:9px;border-bottom:1px solid #294252}
.review{color:#ffd16d}.confirmed{color:#73ebc4}.deleted,.disqualified{color:#ff9393}
.grid{display:flex;gap:10px;flex-wrap:wrap}label{display:flex;flex-direction:column;gap:4px}.error{color:#ffad91;white-space:pre-wrap}
"""

FISCH_STYLE = r"""/* Paste the entire file into OBS Browser Source > Properties > Custom CSS. */
:root { --fisch-panel: rgba(8, 9, 15, .94); --fisch-line: rgba(225, 232, 242, .25); --fisch-cyan: #83ceff; --fisch-gold: #ffdb68; }
html, body { background: transparent !important; margin: 0 !important; overflow: hidden !important; }
body { padding: 12px !important; font-family: "Segoe UI", Arial, sans-serif !important; color: #f5f5f5 !important; }
.board { width: 620px; background: var(--fisch-panel) !important; border: 1px solid var(--fisch-line) !important; border-radius: 0 !important; box-shadow: 0 5px 24px #0008 !important; }
header { position: relative; padding: 19px 22px 16px !important; background: linear-gradient(115deg, #242332e8, #0d0d16f5 72%) !important; border-bottom: 1px solid var(--fisch-line) !important; }
.eyebrow { color: var(--fisch-cyan) !important; font-size: 11px !important; font-weight: 700; letter-spacing: 2px !important; }
h1 { margin: 12px 0 !important; font-size: 28px !important; font-weight: 900 !important; font-style: italic; letter-spacing: .2px !important; line-height: 1.15; color: #fff !important; text-shadow: 2px 2px 0 #000a; }
#timer { display: inline-block; margin: 3px 0 0 !important; padding: 6px 11px; border: 1px solid #ffdb6860; background: #09090fbf; color: var(--fisch-gold); font-size: 19px !important; font-weight: 800 !important; letter-spacing: 1px; font-variant-numeric: tabular-nums; }
#connection { margin-top: 10px; color: var(--fisch-cyan) !important; font-size: 11px !important; font-weight: 700; letter-spacing: 1.5px !important; }
#rows { display: flex; flex-direction: column; gap: 7px; padding: 13px !important; color: #bbb; font-style: italic; }
.row { min-height: 52px; padding: 10px 12px !important; gap: 13px !important; background: #11121bbc; border: 1px solid var(--fisch-line); border-radius: 0 !important; animation: none !important; transition: none !important; font-style: normal; }
.rank { flex: 0 0 37px; width: 37px !important; font-size: 21px !important; font-weight: 900 !important; font-style: italic; color: #a9afbe; }
.name { min-width: 0; font-size: 19px !important; font-style: italic; font-weight: 800 !important; white-space: nowrap; color: #f8f8fb; }
.weight { flex-shrink: 0; font-size: 22px !important; font-weight: 800 !important; color: #e4e8ee; font-variant-numeric: tabular-nums; }
.row.first { border-color: #ffdc7ca6; background: linear-gradient(100deg, #65501e66, #171410c9) !important; }
.first .rank, .first .weight { color: var(--fisch-gold) !important; }
.row.second { border-color: #83ceff70; }
.second .rank, .second .weight { color: var(--fisch-cyan) !important; }
.third .rank, .third .weight { color: #e9b896 !important; }
#recent { padding: 12px 18px !important; border-top: 1px solid var(--fisch-line) !important; background: #15161d8c; color: #d7dfeb !important; font-size: 13px !important; line-height: 1.5; overflow-wrap: anywhere; }
footer { padding: 12px 18px !important; color: #a9afbd !important; font-size: 11px !important; letter-spacing: 1px !important; line-height: 1.4; }
#status { color: #a7df96; font-weight: 800; }
#accuracy { margin-top: 7px !important; color: #9c9fac; font-size: 11px; letter-spacing: 0 !important; }
.board.vertical { width: 380px; }
.vertical h1 { font-size: 22px !important; }
.vertical .row { gap: 8px !important; padding: 10px 8px !important; }
.vertical .rank { flex-basis: 29px; width: 29px !important; font-size: 18px !important; }
.vertical .name { font-size: 16px !important; }
.vertical .weight { font-size: 18px !important; }

#rules { margin-top: 12px; color: var(--fisch-cyan); font-size: 14px; line-height: 1.5; white-space: pre-wrap; overflow-wrap: anywhere; }
#rules:empty, #connection:empty { display: none; }
"""

OVERLAY_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>SpaceGoat Overlay</title><style>
__STYLE__
body{background:transparent;overflow:hidden;padding:16px}.board{width:620px;transform-origin:top left;border-radius:18px;overflow:hidden;box-shadow:0 12px 35px #001a2260;border:1px solid #49d9c460}
header{padding:22px;background:linear-gradient(120deg,#10495c,#116555);border-bottom:2px solid #7eeac3}
.eyebrow{font-size:11px;letter-spacing:4px;color:#afffdc}h1{font-size:25px;letter-spacing:1px}#rows{padding:10px 16px}
.row{display:flex;align-items:center;gap:16px;padding:12px;border-radius:10px;transition:background .5s}
.rank{width:38px;font-size:23px;font-weight:800}.name{flex:1;font-size:21px;font-weight:600;overflow:hidden;text-overflow:ellipsis}.weight{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.first{color:#ffdb7d;background:#eec23b12}.second{color:#d8ebf5}.third{color:#e9b185}footer{padding:12px 22px;font-size:12px;letter-spacing:2px;color:#aee5dc}#recent{padding:12px 22px;border-top:1px solid #64ceb330;font-size:14px}#connection{font-size:11px;color:#ffa885;letter-spacing:1px}
.vertical{width:380px}.vertical h1{font-size:19px}.vertical .name{font-size:16px}.vertical .weight{font-size:18px}.vertical .row{gap:8px;padding:10px 0}
__FISCH_STYLE__
</style></head><body><section class="board" id="board"><header><div class="eyebrow">SPACEGOAT • LIVE FROM FISCH</div><h1 id="title">FISCH WEIGHT CHAMPIONSHIP</h1><div id="timer" style="font-size:22px;font-weight:700;margin:10px 0;font-variant-numeric:tabular-nums"></div><div id="rules"></div></header><div id="rows"></div><div id="recent"></div><footer><span id="connection">Connecting?</span> MOST WEIGHT WINS! <span id="status"></span><div id="accuracy" style="margin-top:8px;letter-spacing:0">Experimental scores - not 100% accurate; still working on it.</div></footer></section><script>
const $=id=>document.getElementById(id), fmt=x=>Number(x).toLocaleString(undefined,{maximumFractionDigits:2});
let connected=false,lastState=null,lastUpdate=0,refreshing=false;const params=new URLSearchParams(location.search);
let timerState=null,timerReceived=0;
function render(s){timerState=s;timerReceived=performance.now();updateTimer();lastState=s;try{localStorage.setItem('spacegoat-last',JSON.stringify(s))}catch(e){}
$('accuracy').hidden=!s.best_effort;$('rules').textContent=s.rules||'';const o=s.overlay, board=$('board');board.classList.toggle('vertical',params.get('layout')==='vertical'||o.vertical);
board.style.setProperty('--fisch-panel','rgba(8,9,15,'+o.opacity+')');board.style.transform='scale('+o.scale+')';board.style.background='rgba(5,25,36,'+o.opacity+')';$('title').textContent=s.tournament.name.toUpperCase();$('status').textContent='• '+s.tournament.status.toUpperCase();
const existing=new Map([...$('rows').children].map(el=>[el.dataset.user,el]));
if(!existing.size)$('rows').replaceChildren();
for(const p of s.leaderboard.slice(0,o.players)){let el=existing.get(p.username);if(!el){el=document.createElement('div');el.innerHTML='<div class="rank"></div><div class="name"></div><div class="weight"></div>';el.dataset.user=p.username}
el.className='row '+({1:'first',2:'second',3:'third'}[p.rank]||'');el.children[0].textContent='#'+p.rank;el.children[1].textContent=p.username;el.children[2].textContent=fmt(p.total_kg)+' kg';$('rows').append(el);existing.delete(p.username)}for(const el of existing.values())el.remove();
if(!s.leaderboard.length)$('rows').textContent='Waiting for confirmed catches…';
$('recent').hidden=!o.recent;$('recent').textContent=s.recent?'LATEST • '+s.recent.username+' • '+s.recent.fish+' • '+fmt(s.recent.weight_kg)+' kg':'';}
function updateTimer(){if(!timerState)return;const t=timerState.tournament,now=timerState.server_time+(performance.now()-timerReceived)/1000;const fmtTime=x=>{x=Math.max(0,Math.floor(x));return (x>=3600?Math.floor(x/3600)+':':'')+String(Math.floor(x/60)%60).padStart(2,'0')+':'+String(x%60).padStart(2,'0')};let text='Ready to start';if(t.status==='ready'&&t.scheduled_start!=null)text='STARTS IN '+fmtTime(Math.ceil(t.scheduled_start-now));else if(t.status!=='ready'){const elapsed=(t.elapsed_seconds||0)+(t.status==='running'&&t.started_at!=null?Math.max(0,now-t.started_at):0);text=(t.status==='paused'?'PAUSED ':t.status==='finished'?'FINAL TIME ':'ELAPSED ')+fmtTime(elapsed)}$('timer').textContent=text}
setInterval(updateTimer,100);
function stale(){connected=false;$('connection').textContent='STALE DATA • RECONNECTING'}
try{const cached=localStorage.getItem('spacegoat-last');if(cached)render(JSON.parse(cached))}catch(e){}
async function refresh(){if(refreshing)return;refreshing=true;try{const r=await fetch('/api/state',{cache:'no-store',signal:AbortSignal.timeout(3000)});if(!r.ok)throw new Error('State unavailable');render(await r.json());lastUpdate=Date.now();connected=true;$('connection').textContent=''}catch(e){stale()}finally{refreshing=false}}refresh();
// Recover promptly if the event stream stalls or reconnects.
setInterval(()=>{if(Date.now()-lastUpdate>=1000)refresh()},500);
const source=new EventSource('/stream');source.addEventListener('state',e=>{render(JSON.parse(e.data));lastUpdate=Date.now();connected=true;$('connection').textContent=''});source.onopen=()=>refresh();source.onerror=stale;
</script></body></html>""".replace("__STYLE__", WEB_STYLE).replace("__FISCH_STYLE__", FISCH_STYLE)

ADMIN_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SpaceGoat Admin</title><style>__STYLE__ body{background:#071721;padding:22px;max-width:1400px;margin:auto}textarea{width:100%;min-height:80px}input{max-width:240px}pre{white-space:pre-wrap}a{color:#80e9d0}</style></head><body>
<h1>SpaceGoat Tournament Control</h1><p class="muted">Corrections are audited. Review events contribute zero until confirmed.</p>
<div class="panel"><span id="auth">Connected</span></div><div id="error" class="error"></div>
<div id="private"><div class="panel"><h2 id="title"></h2><div class="grid"><input id="sessionName" placeholder="New tournament name"><button data-action="create">Create</button><button data-action="start">Start now</button><button data-action="pause">Pause</button><button data-action="resume">Resume</button><button data-action="finish">Finish</button><button data-action="reset">Reset into new session</button><button id="export">Export CSV</button></div><div class="grid" style="margin-top:12px"><label>Start countdown (seconds)<input id="countdownSeconds" type="number" min="1" max="86400" value="60"></label><button id="scheduleStart">Start with countdown</button><button data-action="cancel_countdown">Cancel countdown</button></div><p id="timer" style="font-size:22px;font-weight:700;font-variant-numeric:tabular-nums"></p><p class="muted">Create a tournament, then start now or use a countdown. Pause freezes scoring and the elapsed clock; Resume continues both. Finish keeps the final scores. Capture starts automatically when you start or schedule the tournament.</p><div id="stats"></div><table><thead><tr><th>Rank</th><th>Player</th><th>Total kg</th><th>Catches</th><th>Largest</th><th>Average</th><th>Actions</th></tr></thead><tbody id="leaders"></tbody></table></div>
<div class="panel"><h2>Player name corrections</h2><p class="muted">Everyone joins automatically when a readable catch appears. Add corrections only when needed: wrongName-&gt;correctUsername, one per line. Saving merges matching scores in the current tournament and corrects future catches. No registration is required.</p><textarea id="roster" placeholder="FifthSparkGMZZ-&gt;FifthSparkGMZ"></textarea><button id="rosterSave">Save corrections</button><div class="grid" style="margin-top:12px"><label>Incorrect name<input id="correctionSource" placeholder="FifthSparkGMZZ"></label><label>Correct username<input id="correctionTarget" list="playerNames" placeholder="FifthSparkGMZ"></label><datalist id="playerNames"></datalist><button id="addCorrection">Add and save correction</button></div><p id="correctionNotice"></p><p id="kickedPlayers"></p></div><div class="panel"><h2>Overlay rules</h2><label>Rules shown below the timer<textarea id="rulesText" maxlength="2000" placeholder="Common fish only. Highest total weight wins."></textarea></label><button id="rulesSave">Save rules</button></div><div class="panel"><h2>Capture / OCR</h2><p id="captureStatus"></p><div class="grid"><button data-monitor="start">Start capture</button><button data-monitor="stop">Stop capture</button><button data-monitor="restart">Restart capture</button><button data-monitor="preview">Test OCR</button></div><p class="muted">Start now, Resume, and Start with countdown start capture automatically. Keep the game chat visible and unobstructed; place this dashboard on another monitor when possible.</p><details><summary>Chat region setup</summary><p>Select a monitor, load its screenshot, then drag around the catch messages. Click Save chat region. Stop capture before changing the region.</p><div class="grid"><select id="captureMonitor"></select><button id="loadScreen">Load monitor screenshot</button></div><canvas id="regionCanvas" style="display:none;width:100%;max-width:900px;touch-action:none;cursor:crosshair;margin:12px 0"></canvas><div class="grid"><label>Left<input id="regionLeft" type="number"></label><label>Top<input id="regionTop" type="number"></label><label>Width<input id="regionWidth" type="number" min="60"></label><label>Height<input id="regionHeight" type="number" min="30"></label><button id="saveRegion">Save chat region</button></div><p id="regionNotice"></p></details><details><summary>Detailed capture health</summary><pre id="health"></pre></details><details><summary>OCR preview</summary><pre id="preview"></pre></details></div>
<div class="panel"><h2>Add or correct a catch</h2><div class="grid"><label>Event ID (blank = add)<input id="eventId" readonly></label><label>Username<input id="username"></label><label>Fish<input id="fish"></label><label>Rarity<input id="rarity"></label><label>Weight kg<input id="weight"></label><label>Status<select id="eventStatus"><option>confirmed</option><option>review</option><option>disqualified</option></select></label><label>Reason<input id="reason"></label></div><p><button id="save">Save catch</button> <button id="clear">Clear form</button></p><pre id="evidence"></pre></div>
<div class="panel"><h2>Catch feed / history</h2><select id="history"></select><button id="previous">Previous</button><button id="next">Next</button><span id="pagination"></span><table><thead><tr><th>ID / time</th><th>Player / fish</th><th>kg</th><th>Confidence</th><th>Status / OCR</th><th>Actions</th></tr></thead><tbody id="events"></tbody></table></div>
<div class="panel"><h2>Overlay settings</h2><div class="grid"><label>Players<input id="players" type="number" min="1" max="10"></label><label>Scale<input id="scale" type="number" step="0.1" min="0.3" max="3"></label><label>Panel opacity<input id="opacity" type="number" step="0.05" min="0" max="1"></label><label>Vertical<input id="vertical" type="checkbox"></label><label>Recent catches<input id="recent" type="checkbox"></label><button id="overlaySave">Apply overlay</button></div></div>
<div class="panel"><h2>Parser calibration</h2><p class="muted">The shipped preset matches supplied live-game samples: [System] Player has perfectly caught a Rarity 1.2kg Fish! Modifiers remain in the fish description. Required named groups: username, fish, rarity, weight. Parser changes take effect on future events.</p><h3>Patterns</h3><p class="muted">Give each regex a friendly name. Enable the patterns to use for future catches (1-12 active). Changes apply when you save the parser.</p><div id="patterns"></div><button id="addPattern" type="button">Add pattern</button><span id="patternStatus" role="status"></span><label>Message start regex (wrapped-row grouping)<input id="messageStart"></label><div class="grid"><label>Decimal separator<select id="decimal"><option>.</option><option>,</option></select></label><label>Minimum kg<input id="minKg"></label><label>Maximum kg<input id="maxKg"></label><label>Minimum OCR confidence<input id="confidence" type="number" step="0.01" min="0" max="1"></label><label>Best-effort live scoring<input id="bestEffort" type="checkbox"></label><label>Verified with real samples<input id="verified" type="checkbox"></label></div><button id="parserSave">Save parser</button><label>Sample announcement<textarea id="sample"></textarea></label><button id="testParser">Test without recording</button><pre id="parsed"></pre></div></div>
<script>
const $=id=>document.getElementById(id);let offset=0,items=[],activeId=null,editing=false,leaderSnapshot='';
function err(e){$('error').textContent=e.message||String(e)}
async function api(path,method='GET',body){const r=await fetch(path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});if(!r.ok){let detail=await r.text();throw Error(r.status+' '+detail)}return r.headers.get('content-type').includes('application/json')?r.json():r.text()}
function cells(row,values){for(const v of values){const td=document.createElement('td');td.textContent=v;row.append(td)}}
let timerState=null,timerReceived=0;
function updateTimer(){if(!timerState)return;const t=timerState.tournament,now=timerState.server_time+(performance.now()-timerReceived)/1000;const fmtTime=x=>{x=Math.max(0,Math.floor(x));return (x>=3600?Math.floor(x/3600)+':':'')+String(Math.floor(x/60)%60).padStart(2,'0')+':'+String(x%60).padStart(2,'0')};let text='Ready to start';if(t.status==='ready'&&t.scheduled_start!=null)text='STARTS IN '+fmtTime(Math.ceil(t.scheduled_start-now));else if(t.status!=='ready'){const elapsed=(t.elapsed_seconds||0)+(t.status==='running'&&t.started_at!=null?Math.max(0,now-t.started_at):0);text=(t.status==='paused'?'PAUSED ':t.status==='finished'?'FINAL TIME ':'ELAPSED ')+fmtTime(elapsed)}$('timer').textContent=text}
setInterval(updateTimer,100);
function render(s){timerState=s;timerReceived=performance.now();updateTimer();activeId=s.tournament.id;$('title').textContent=s.tournament.name+' • '+s.tournament.status;$('stats').textContent='Confirmed: '+(s.counts.confirmed||0)+' / Review: '+(s.counts.review||0);const nextLeaders=JSON.stringify([activeId,s.leaderboard]);if(nextLeaders!==leaderSnapshot){leaderSnapshot=nextLeaders;$('leaders').replaceChildren();$('playerNames').replaceChildren();for(const p of s.leaderboard){const option=document.createElement('option');option.value=p.username;$('playerNames').append(option)}for(const p of s.leaderboard){let tr=document.createElement('tr');cells(tr,[p.rank,p.username,p.total_kg.toFixed(3),p.catches,p.largest_kg,p.average_kg.toFixed(3)]);const td=document.createElement('td'),correct=document.createElement('button'),kick=document.createElement('button');correct.textContent='Correct name';correct.onclick=()=>{$('correctionSource').value=p.username;$('correctionTarget').value='';$('correctionTarget').scrollIntoView({behavior:'smooth',block:'center'});$('correctionTarget').focus()};td.append(correct);kick.textContent='Kick';kick.onclick=async()=>{try{await api('/api/players/kick','POST',{username:p.username});render(await api('/api/state'));await feed()}catch(e){err(e)}};td.append(kick);tr.append(td);$('leaders').append(tr)}}$('kickedPlayers').textContent='Kicked this tournament: '+((s.kicked_players||[]).join(', ')||'none');$('captureStatus').textContent=s.health.capture+' | OCR: '+s.health.ocr;$('health').textContent=JSON.stringify({...s.health,preview:undefined},null,2);if(s.health.preview!==undefined)$('preview').textContent=s.health.preview;}
async function feed(){try{$('preview').textContent=(await api('/api/preview')).text;const selected=$('history').value||activeId;const r=await api('/api/events?tournament_id='+selected+'&limit=25&offset='+offset);items=r.items;$('events').replaceChildren();$('pagination').textContent=(offset+1)+'–'+(offset+items.length)+' of '+r.total;
for(const p of items){let tr=document.createElement('tr');tr.className=p.status;cells(tr,[p.id+' / '+p.timestamp,p.username+' / '+p.fish,p.weight_kg,Math.round(p.confidence*100)+'% OCR / '+Math.round(p.visual_confidence*100)+'% visual',p.status+' • '+p.reason+'\n'+p.raw_text]);let td=document.createElement('td');let edit=document.createElement('button');edit.textContent='Review / edit';edit.onclick=()=>{for(const [id,key] of [['eventId','id'],['username','username'],['fish','fish'],['rarity','rarity'],['weight','weight_kg'],['eventStatus','status'],['reason','reason']])$(id).value=p[key];$('evidence').textContent='Evidence: '+(p.evidence||'not saved');api('/api/audit/'+p.id).then(a=>$('evidence').textContent+='\nAudit: '+JSON.stringify(a,null,2)).catch(err);window.scrollTo({top:$('eventId').getBoundingClientRect().top+window.scrollY-100,behavior:'smooth'})};let del=document.createElement('button');del.textContent='Delete';del.onclick=async()=>{if(confirm('Remove event '+p.id+' from scoring? History is retained.'))try{await api('/api/events/'+p.id,'DELETE',{reason:'Operator deletion'});await feed()}catch(e){err(e)}};td.append(edit,del);tr.append(td);$('events').append(tr)}}catch(e){err(e)}}
async function load(){try{render(await api('/api/state'));const config=await api('/api/config');for(const id of ['players','scale','opacity'])$(id).value=config.overlay[id];for(const id of ['vertical','recent'])$(id).checked=config.overlay[id];loadPatterns(config);$('messageStart').value=config.message_start;$('decimal').value=config.decimal_separator;$('minKg').value=config.min_kg;$('maxKg').value=config.max_kg;$('confidence').value=config.confidence;$('verified').checked=config.parser_verified;$('bestEffort').checked=config.best_effort;$('rulesText').value=config.rules||'';const mappings={...(config.name_corrections||{})};for(const p of config.registered_players||[])for(const alias of p.aliases||[])if(!(alias in mappings))mappings[alias]=p.username;$('roster').value=Object.entries(mappings).map(([from,to])=>from+'->'+to).join('\n');await loadCapture(config);await history();await feed()}catch(e){err(e)}}
async function history(){const previous=$('history').value;const sessions=await api('/api/history');$('history').replaceChildren();for(const t of sessions){let o=document.createElement('option');o.value=t.id;o.textContent=t.name+' • '+t.status+' • #'+t.id;$('history').append(o)}$('history').value=previous||activeId}
for(const b of document.querySelectorAll('[data-action]'))b.onclick=async()=>{try{if(['reset','create'].includes(b.dataset.action)&&!confirm('Archive current session and create a new one?'))return;await api('/api/tournament','POST',{action:b.dataset.action,name:$('sessionName').value||'Fisch Weight Championship'});render(await api('/api/state'));$('history').value='';offset=0;await history();await feed()}catch(e){err(e)}};
const regionFields=['regionLeft','regionTop','regionWidth','regionHeight'];let monitorList=[],screenImage=null,screenMonitor=null,dragStart=null;
async function loadCapture(config){if(config.region)regionFields.forEach((id,i)=>$(id).value=config.region[i]);monitorList=await api('/api/monitors');$('captureMonitor').replaceChildren();for(const m of monitorList){const option=document.createElement('option');option.value=m.id;option.textContent='Monitor '+m.id+' ('+m.width+' x '+m.height+')';$('captureMonitor').append(option)}}
for(const button of document.querySelectorAll('[data-monitor]'))button.onclick=async()=>{button.disabled=true;try{await api('/api/monitoring','POST',{action:button.dataset.monitor});render(await api('/api/state'));$('error').textContent=''}catch(e){err(e)}finally{button.disabled=false}};
$('saveRegion').onclick=async()=>{try{const region=regionFields.map(id=>Number($(id).value));await api('/api/config','PATCH',{region});$('regionNotice').textContent='Chat region saved. Start the tournament or restart capture.';$('error').textContent=''}catch(e){err(e)}};
$('loadScreen').onclick=async()=>{try{screenMonitor=monitorList.find(m=>m.id===Number($('captureMonitor').value));if(!screenMonitor)throw Error('Select a monitor');const response=await fetch('/api/monitor-image/'+screenMonitor.id,{cache:'no-store'});if(!response.ok)throw Error('Screenshot failed: '+response.status);const url=URL.createObjectURL(await response.blob());const picture=new Image();try{picture.src=url;await picture.decode();screenImage=picture;const canvas=$('regionCanvas');canvas.width=picture.naturalWidth;canvas.height=picture.naturalHeight;canvas.style.display='block';canvas.getContext('2d').drawImage(picture,0,0)}finally{URL.revokeObjectURL(url)}}catch(e){err(e)}};
const canvas=$('regionCanvas');const point=e=>{const rect=canvas.getBoundingClientRect();return {x:Math.max(0,Math.min(canvas.width,(e.clientX-rect.left)*canvas.width/rect.width)),y:Math.max(0,Math.min(canvas.height,(e.clientY-rect.top)*canvas.height/rect.height))}};
canvas.onpointerdown=e=>{if(!screenImage)return;dragStart=point(e);canvas.setPointerCapture(e.pointerId)};
canvas.onpointermove=e=>{if(!dragStart)return;const end=point(e),ctx=canvas.getContext('2d');ctx.drawImage(screenImage,0,0);ctx.strokeStyle='#7eeac3';ctx.lineWidth=4;ctx.strokeRect(dragStart.x,dragStart.y,end.x-dragStart.x,end.y-dragStart.y)};
canvas.onpointerup=e=>{if(!dragStart)return;const end=point(e);const region=[screenMonitor.left+Math.floor(Math.min(dragStart.x,end.x)),screenMonitor.top+Math.floor(Math.min(dragStart.y,end.y)),Math.round(Math.abs(end.x-dragStart.x)),Math.round(Math.abs(end.y-dragStart.y))];regionFields.forEach((id,i)=>$(id).value=region[i]);dragStart=null;canvas.releasePointerCapture(e.pointerId);$('regionNotice').textContent='Selection ready. Click Save chat region.'};canvas.onpointercancel=()=>{dragStart=null};
$('rulesSave').onclick=async()=>{try{await api('/api/config','PATCH',{rules:$('rulesText').value});render(await api('/api/state'))}catch(e){err(e)}};
function parseCorrections(text){const mappings=Object.create(null);for(const [i,line] of text.split('\n').entries()){if(!line.trim())continue;const parts=line.split('->').map(p=>p.trim());if(parts.length!==2||!parts[0]||!parts[1])throw Error('Line '+(i+1)+': use wrongName->correctUsername');if(Object.prototype.hasOwnProperty.call(mappings,parts[0]))throw Error('Repeated incorrect name on line '+(i+1));mappings[parts[0]]=parts[1]}return mappings}
async function saveCorrections(mappings){await api('/api/config','PATCH',{name_corrections:mappings,registered_players:[]});$('roster').value=Object.entries(mappings).map(([from,to])=>from+'->'+to).join('\n');$('correctionNotice').textContent='Corrections saved. Current scores merged; new players still join automatically.';render(await api('/api/state'));await feed()}
$('rosterSave').onclick=async()=>{try{await saveCorrections(parseCorrections($('roster').value))}catch(e){err(e)}};
$('addCorrection').onclick=async()=>{try{const from=$('correctionSource').value.trim(),to=$('correctionTarget').value.trim();if(!from||!to)throw Error('Enter the incorrect name and correct username');const mappings=parseCorrections($('roster').value);mappings[from]=to;await saveCorrections(mappings);$('correctionSource').value='';$('correctionTarget').value=''}catch(e){err(e)}};
$('scheduleStart').onclick=async()=>{try{await api('/api/tournament','POST',{action:'schedule',countdown_seconds:Number($('countdownSeconds').value)});render(await api('/api/state'))}catch(e){err(e)}};
$('save').onclick=async()=>{try{const body={username:$('username').value,fish:$('fish').value,rarity:$('rarity').value,weight_kg:$('weight').value,status:$('eventStatus').value,reason:$('reason').value};const id=$('eventId').value;await api('/api/events'+(id?'/'+id:''),id?'PATCH':'POST',body);$('clear').click();await feed()}catch(e){err(e)}};
$('clear').onclick=()=>{for(const id of ['eventId','username','fish','rarity','weight','reason'])$(id).value='';$('evidence').textContent='';$('eventStatus').value='confirmed'};
$('history').onchange=()=>{offset=0;feed()};$('previous').onclick=()=>{offset=Math.max(0,offset-25);feed()};$('next').onclick=()=>{offset+=25;feed()};
$('export').onclick=async()=>{try{const text=await api('/api/export?tournament_id='+$('history').value);let a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'text/csv'}));a.download='spacegoat-catches.csv';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}catch(e){err(e)}};
$('overlaySave').onclick=async()=>{try{await api('/api/config','PATCH',{overlay:{players:Number($('players').value),scale:Number($('scale').value),opacity:Number($('opacity').value),vertical:$('vertical').checked,recent:$('recent').checked}})}catch(e){err(e)}};
function addPatternRow(name='',pattern='',enabled=false){
 const row=document.createElement('fieldset');row.className='patternEntry';
 const activeLabel=document.createElement('label'),active=document.createElement('input');active.type='checkbox';active.checked=enabled;active.className='patternEnabled';activeLabel.append(active,document.createTextNode(' Enabled'));
 const nameLabel=document.createElement('label'),nameInput=document.createElement('input');nameInput.className='patternName';nameInput.value=name;nameInput.maxLength=80;nameLabel.append(document.createTextNode('Friendly name'),nameInput);
 const regexLabel=document.createElement('label'),regex=document.createElement('textarea');regex.className='patternRegex';regex.value=pattern;regex.maxLength=2000;regex.spellcheck=false;regexLabel.append(document.createTextNode('Regex expression'),regex);
 const remove=document.createElement('button');remove.type='button';remove.textContent='Remove';remove.onclick=()=>{row.remove();$('patternStatus').textContent='Unsaved changes; click Save parser to apply.'};
 row.append(activeLabel,nameLabel,regexLabel,remove);row.oninput=()=>{$('patternStatus').textContent='Unsaved changes; click Save parser to apply.'};$('patterns').append(row);return nameInput;
}
function loadPatterns(config){$('patterns').replaceChildren();const entries=[...(config.pattern_presets||[])];for(const pattern of config.patterns)if(!entries.some(p=>p.pattern===pattern))entries.push({name:'Existing pattern '+(entries.length+1),pattern});for(const entry of entries)addPatternRow(entry.name,entry.pattern,config.patterns.includes(entry.pattern));$('patternStatus').textContent=''}
function patternSettings(){const pattern_presets=Array.from($('patterns').children,row=>({name:row.querySelector('.patternName').value.trim(),pattern:row.querySelector('.patternRegex').value}));const patterns=Array.from($('patterns').children).filter(row=>row.querySelector('.patternEnabled').checked).map(row=>row.querySelector('.patternRegex').value);return {patterns,pattern_presets}}
$('addPattern').onclick=()=>{if($('patterns').children.length>=50){err(Error('Provide at most 50 named patterns'));return}addPatternRow().focus();$('patternStatus').textContent='Unsaved changes; click Save parser to apply.'};
$('parserSave').onclick=async()=>{try{await api('/api/config','PATCH',{...patternSettings(),message_start:$('messageStart').value,decimal_separator:$('decimal').value,min_kg:$('minKg').value,max_kg:$('maxKg').value,confidence:Number($('confidence').value),parser_verified:$('verified').checked,best_effort:$('bestEffort').checked});$('patternStatus').textContent='Parser saved.'}catch(e){err(e)}};
$('testParser').onclick=async()=>{try{$('parsed').textContent=JSON.stringify(await api('/api/parse','POST',{text:$('sample').value}),null,2)}catch(e){err(e)}};
const stream=new EventSource('/stream');stream.addEventListener('state',e=>{render(JSON.parse(e.data))});stream.onerror=()=>{$('auth').textContent='Connection interrupted • reconnecting'};setInterval(feed,3000);load();
</script></body></html>""".replace("__STYLE__", WEB_STYLE)


def validate_config(update, current):
    allowed = {"overlay", "patterns", "pattern_presets", "message_start", "decimal_separator", "min_kg", "max_kg", "confidence", "parser_verified", "best_effort", "region", "registered_players", "name_corrections", "rules"}
    if set(update) - allowed:
        raise ValueError("Unsupported setting")
    result = current | update
    if not isinstance(result.get("rules",""),str) or len(result.get("rules",""))>2000:
        raise ValueError("Rules must be text of at most 2000 characters")
    corrections = result.get("name_corrections",{})
    if not isinstance(corrections,dict) or len(corrections)>500:
        raise ValueError("Provide at most 500 name corrections")
    normalized_sources = {}
    for source,target in corrections.items():
        if not isinstance(source,str) or not re.fullmatch(r"[A-Za-z0-9_ ]{3,64}",source) or not isinstance(target,str) or not re.fullmatch(r"[A-Za-z0-9_]{3,20}",target):
            raise ValueError("Corrections need an OCR name and a valid Roblox target username")
        key = source.replace(" ","").lower()
        if key in normalized_sources:
            raise ValueError("Duplicate correction source")
        normalized_sources[key] = target
    for source,target in corrections.items():
        if target.replace(" ","").lower() in normalized_sources and target.lower()!=source.replace(" ","").lower():
            raise ValueError("Use direct corrections to the final username, not chains or cycles")
    players = result.get("registered_players",[])
    if not isinstance(players,list) or len(players)>200:
        raise ValueError("Register at most 200 players")
    seen = set()
    for player in players:
        if not isinstance(player,dict) or set(player)-{"username","aliases"} or not re.fullmatch(r"[A-Za-z0-9_]{3,20}",str(player.get("username",""))):
            raise ValueError("Each player needs a valid Roblox username")
        key = player["username"].lower()
        if key in seen:
            raise ValueError("Duplicate registered username")
        seen.add(key)
        aliases = player.get("aliases",[])
        if not isinstance(aliases,list) or len(aliases)>10 or any(not isinstance(a,str) or not a.strip() or len(a)>50 for a in aliases):
            raise ValueError("Player aliases must be nonempty text, at most 50 characters each")
    if "region" in update:
        region = update["region"]
        if not isinstance(region,list) or len(region)!=4 or any(type(x) is not int for x in region) or region[2]<60 or region[3]<30:
            raise ValueError("Chat region needs integer left, top, width (at least 60), and height (at least 30)")
    if result["decimal_separator"] not in (".", ","):
        raise ValueError("Decimal separator must be dot or comma")
    if not isinstance(result["patterns"], list) or not 1 <= len(result["patterns"]) <= 12:
        raise ValueError("Provide 1–12 regex patterns")
    for pattern in result["patterns"]:
        if not isinstance(pattern, str) or len(pattern) > 2000 or not {"username", "fish", "rarity", "weight"} <= set(re.compile(pattern).groupindex):
            raise ValueError("Pattern needs named username, fish, rarity, weight groups")
    presets = result.get("pattern_presets", [])
    if not isinstance(presets, list) or len(presets) > 50:
        raise ValueError("Provide at most 50 named patterns")
    names = set()
    for entry in presets:
        if not isinstance(entry, dict) or set(entry) != {"name", "pattern"}:
            raise ValueError("Each named pattern needs a name and regex expression")
        name, pattern = entry["name"], entry["pattern"]
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("Pattern names must contain 1-80 characters")
        if name.strip().casefold() in names:
            raise ValueError("Pattern names must be unique")
        names.add(name.strip().casefold())
        if not isinstance(pattern, str) or len(pattern) > 2000 or not {"username", "fish", "rarity", "weight"} <= set(re.compile(pattern).groupindex):
            raise ValueError("Named patterns need valid regexes with username, fish, rarity, weight groups")
    re.compile(result["message_start"])
    if not 0 <= float(result["confidence"]) <= 1:
        raise ValueError("Confidence must be 0–1")
    if not 0 < Decimal(result["min_kg"]) <= Decimal(result["max_kg"]) <= Decimal("1000000000"):
        raise ValueError("Invalid plausibility bounds")
    o = result["overlay"]
    if set(o) != set(DEFAULTS["overlay"]) or not isinstance(o["players"], int) or not 1 <= o["players"] <= 10 or not .3 <= o["scale"] <= 3 or not 0 <= o["opacity"] <= 1:
        raise ValueError("Invalid overlay settings")
    for key in ("parser_verified", "best_effort"):
        if not isinstance(result[key], bool):
            raise ValueError("Expected boolean")
    return update


def create_app(runtime):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
    # FastAPI resolves annotations against module globals, including in frozen builds.
    globals()["Request"] = Request
    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)

    def check_origin(request):
        if request.method != "GET":
            origin = request.headers.get("origin")
            if origin and origin != f"http://{request.headers.get('host')}":
                raise HTTPException(403, "Cross-origin mutation refused")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        from starlette.responses import JSONResponse
        try:
            address = ipaddress.ip_address(request.client.host)
            allowed = address.is_loopback or address in ipaddress.ip_network("10.0.0.0/8") or address in ipaddress.ip_network("172.16.0.0/12") or address in ipaddress.ip_network("192.168.0.0/16")
        except (ValueError, AttributeError):
            allowed = False
        if not allowed:
            return JSONResponse({"detail": "Trusted private LAN clients only"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 32768:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/overlay")
    def overlay():
        return HTMLResponse(OVERLAY_HTML)

    @app.get("/admin")
    def admin():
        return HTMLResponse(ADMIN_HTML)

    @app.get("/health")
    def health():
        with runtime.lock:
            return json.loads(json.dumps(runtime.health))

    @app.get("/api/state")
    def state():
        # Read-only OBS clients receive scoring and health, but no raw OCR.
        result = runtime.state()
        result["health"].pop("preview", None)
        return result

    @app.get("/api/events")
    def events(request: Request, tournament_id: int | None = None, limit: int = 50, offset: int = 0):
        check_origin(request)
        return runtime.store.events(tournament_id, max(1, min(limit, 200)), max(0, offset))

    @app.get("/api/history")
    def history(request: Request):
        check_origin(request)
        return runtime.store.history()

    @app.get("/api/audit/{event_id}")
    def audit(event_id: int, request: Request):
        check_origin(request)
        return runtime.store.audits(event_id)

    @app.get("/api/evidence/{filename}")
    def evidence(filename: str, request: Request):
        check_origin(request)
        if not re.fullmatch(r"[a-zA-Z0-9_-]+\.png", filename):
            raise HTTPException(400, "Invalid evidence name")
        path = runtime.config.directory / "evidence" / filename
        if not path.is_file():
            raise HTTPException(404, "Evidence not found")
        return FileResponse(path, media_type="image/png")

    @app.get("/api/export")
    def export(request: Request, tournament_id: int | None = None):
        check_origin(request)
        stream = io.StringIO(newline="")
        fields = ["id", "username", "fish", "rarity", "weight_kg", "timestamp", "status", "confidence", "reason", "raw_text", "evidence"]
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        offset = 0
        while True:
            rows = runtime.store.events(tournament_id, 500, offset)["items"]
            for row in rows:
                # Protect spreadsheet users from formula injection in OCR strings.
                writer.writerow({k: "'"+v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for k, v in row.items()})
            offset += len(rows)
            if len(rows) < 500:
                break
        return StreamingResponse(iter([stream.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=spacegoat-catches.csv"})

    @app.get("/api/config")
    def config(request: Request):
        check_origin(request)
        settings = runtime.config.snapshot()
        settings.pop("admin_token", None)
        return settings

    @app.get("/api/preview")
    def preview(request: Request):
        check_origin(request)
        return {"text": runtime.state()["health"]["preview"]}

    async def body(request):
        check_origin(request)
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(400, "JSON object required")
        return payload

    @app.post("/api/tournament")
    async def tournament(request: Request):
        payload = await body(request)
        try:
            await asyncio.to_thread(runtime.tournament_action, payload["action"], payload.get("name", "Fisch Weight Championship"), payload.get("countdown_seconds"))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/events")
    async def add(request: Request):
        payload = await body(request)
        settings = runtime.config.snapshot()
        try:
            fields = {"username": str(payload["username"]).strip(), "fish": str(payload["fish"]).strip(),
                      "rarity": str(payload["rarity"]).strip(), "weight_units": parse_weight(str(payload["weight_kg"]), settings),
                      "status": payload.get("status", "confirmed"), "reason": str(payload.get("reason", "Manual addition")),
                      "raw_text": "[Manual addition]", "confidence": 1.0, "visual_confidence": 1.0}
            validate_manual(fields, settings)
            if fields["status"] not in ("confirmed", "review", "disqualified"):
                raise ValueError("Invalid status")
            with runtime.control_lock:
                event_id = runtime.store.insert(fields, uuid.uuid4().hex, runtime.store.active()["id"], now(), actor="admin")
            runtime.changed()
        except (ValueError, KeyError, InvalidOperation) as exc:
            raise HTTPException(400, str(exc))
        return {"id": event_id}

    @app.patch("/api/events/{event_id}")
    async def correct(event_id: int, request: Request):
        payload = await body(request)
        try:
            await asyncio.to_thread(runtime.store.correct, event_id, payload, runtime.config.snapshot())
            runtime.changed()
        except (ValueError, InvalidOperation) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.delete("/api/events/{event_id}")
    async def delete(event_id: int, request: Request):
        payload = await body(request)
        try:
            await asyncio.to_thread(runtime.store.correct, event_id, payload, runtime.config.snapshot(), True)
            runtime.changed()
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.patch("/api/config")
    async def configure(request: Request):
        payload = await body(request)
        try:
            validate_config(payload, runtime.config.snapshot())
            if "region" in payload and (any(t.is_alive() for t in runtime.threads) or runtime.restart_pending):
                raise ValueError("Stop capture and wait for the current OCR call before changing the chat region")
            with runtime.control_lock:
                runtime.config.update(payload)
                if "registered_players" in payload or "name_corrections" in payload:
                    settings = runtime.config.snapshot()
                    runtime.store.reconcile_players(settings.get("registered_players",[]),settings.get("name_corrections",{}))
                runtime.changed()
        except (ValueError, TypeError, KeyError, InvalidOperation, re.error) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.post("/api/players/kick")
    async def kick(request: Request):
        payload = await body(request)
        try:
            with runtime.control_lock:
                runtime.store.kick_player(str(payload["username"]))
                runtime.changed()
        except (ValueError,KeyError) as exc:
            raise HTTPException(400,str(exc))
        return {"ok":True}

    @app.post("/api/monitoring")
    async def monitoring(request: Request):
        payload = await body(request)
        try:
            await asyncio.to_thread(runtime.monitoring_action,payload["action"])
        except (ValueError,KeyError) as exc:
            raise HTTPException(400,str(exc))
        return {"ok":True}

    @app.get("/api/monitors")
    def monitors(request: Request):
        check_origin(request)
        import mss
        with mss.mss() as capture:
            return [{"id":i,**m} for i,m in enumerate(capture.monitors) if i]

    @app.get("/api/monitor-image/{monitor_id}")
    def monitor_image(monitor_id: int,request: Request):
        check_origin(request)
        import mss
        from fastapi.responses import Response
        with mss.mss() as capture:
            if not 1 <= monitor_id < len(capture.monitors):
                raise HTTPException(400,"Monitor not connected")
            image=np.array(capture.grab(capture.monitors[monitor_id]))[:,:,:3]
        ok,encoded=cv2.imencode(".png",image)
        if not ok:
            raise HTTPException(500,"Screenshot failed")
        return Response(encoded.tobytes(),media_type="image/png",headers={"Cache-Control":"no-store"})

    @app.post("/api/parse")
    async def parse(request: Request):
        payload = await body(request)
        return parse_catch(str(payload.get("text", ""))[:4000], 1, 1, runtime.config.snapshot())

    @app.get("/stream")
    async def stream(request: Request):
        async def generate():
            # Full snapshot first on EVERY connection. Slow consumers never own a
            # producer queue and cannot backpressure capture, OCR, or persistence.
            last, heartbeat = None, 0
            while not runtime.stop_event.is_set() and not await request.is_disconnected():
                state = await asyncio.to_thread(runtime.state)
                state["health"].pop("preview", None)
                encoded = json.dumps(state)
                if encoded != last:
                    yield f"event: state\ndata: {encoded}\n\n"
                    last = encoded
                elif time.monotonic()-heartbeat > 10:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(.1)
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache, no-transform"})

    return app


def lan_address():
    addresses = []
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private and not ip.is_loopback and not ip.is_link_local:
            return address
    return "127.0.0.1"


def launch_server(runtime, host, port):
    import uvicorn
    runtime.server = uvicorn.Server(uvicorn.Config(create_app(runtime), host=host, port=port,
                                                  log_config=None, access_log=False, timeout_graceful_shutdown=3,
                                                  proxy_headers=False))
    def run():
        try:
            runtime.server.run()
        except BaseException as exc:
            runtime.update_health(server=f"failed: {exc}")
            runtime.warn(f"HTTP server failed on {host}:{port}: {exc}")
    threading.Thread(target=runtime.timer_loop, name="tournament-timer", daemon=True).start()
    thread = threading.Thread(target=run, name="http", daemon=True)
    thread.start()
    return thread


def launch_gui(runtime, host, port, smoke=False):
    from PySide6.QtCore import Qt, QTimer, QRect, QPoint
    from PySide6.QtGui import QPainter, QColor, QPen
    from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                                  QLabel, QPushButton, QTextEdit, QLineEdit, QCheckBox, QSpinBox,
                                  QComboBox, QFormLayout, QMessageBox, QDialog)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setStyleSheet("QWidget{background:#101e29;color:#e4f5f6;font-size:13px} QPushButton{background:#176375;border:0;border-radius:6px;padding:9px} QPushButton:disabled{background:#33414b} QLineEdit,QTextEdit,QComboBox,QSpinBox{background:#192e3e;border:1px solid #365064;border-radius:4px;padding:5px}")

    class Selector(QDialog):
        def __init__(self, image):
            super().__init__()
            self.image = image
            self.origin = self.end = QPoint()
            self.selected = None
            self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
            self.resize(image.size())
            self.setCursor(Qt.CursorShape.CrossCursor)

        def paintEvent(self, event):
            painter = QPainter(self)
            painter.drawPixmap(self.rect(), self.image)
            painter.fillRect(self.rect(), QColor(0, 0, 0, 60))
            painter.setPen(QPen(QColor("#7effbe"), 2))
            painter.drawRect(QRect(self.origin, self.end).normalized())
            painter.drawText(20, 30, "Drag around the bottom 3–5 chat lines. Escape cancels.")

        def mousePressEvent(self, event):
            self.origin = self.end = event.position().toPoint()

        def mouseMoveEvent(self, event):
            self.end = event.position().toPoint()
            self.update()

        def mouseReleaseEvent(self, event):
            self.end = event.position().toPoint()
            self.selected = QRect(self.origin, self.end).normalized().intersected(self.rect())
            self.accept()

        def keyPressEvent(self, event):
            if event.key() == Qt.Key.Key_Escape:
                self.reject()

    class Window(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle(APP_NAME)
            self.resize(1100, 790)
            widget = QWidget()
            self.setCentralWidget(widget)
            layout = QVBoxLayout(widget)
            title = QLabel("SPACEGOAT  /  FISCH TOURNAMENT SERVER")
            title.setStyleSheet("font-size:23px;font-weight:700;color:#7ce6c6;padding:10px")
            layout.addWidget(title)
            controls = QHBoxLayout()
            layout.addLayout(controls)
            self.monitor = QComboBox()
            for i, screen in enumerate(app.screens()):
                self.monitor.addItem(f"Monitor {i+1}: {screen.name()} ({screen.size().width()}×{screen.size().height()})")
            controls.addWidget(self.monitor)
            self.start_button = self.stop_button = None
            for text, fn in (("Select Chat Region", self.pick), ("Test OCR / Preview", self.test_preview), ("Start Monitoring", self.start), ("Stop Monitoring", self.stop)):
                button = QPushButton(text)
                button.clicked.connect(fn)
                controls.addWidget(button)
                if text == "Start Monitoring":
                    self.start_button = button
                elif text == "Stop Monitoring":
                    self.stop_button = button
                    button.setEnabled(False)
            self.region = QLabel()
            layout.addWidget(self.region)
            self.status = QLabel()
            self.status.setWordWrap(True)
            layout.addWidget(self.status)
            form = QFormLayout()
            layout.addLayout(form)
            for caption, value in (("LAN IP Address", host), ("OBS Overlay URL", f"http://{host}:{port}/overlay"),
                                   ("Admin Dashboard URL", f"http://{host}:{port}/admin")):
                row = QHBoxLayout()
                field = QLineEdit(value)
                field.setReadOnly(True)
                row.addWidget(field)
                button = QPushButton("Copy")
                button.clicked.connect(lambda checked=False, v=value: app.clipboard().setText(v))
                row.addWidget(button)
                form.addRow(caption, row)
            settings_row = QHBoxLayout()
            layout.addLayout(settings_row)
            self.diagnostic = QCheckBox("Diagnostic: save screenshot evidence")
            self.diagnostic.setChecked(runtime.config.snapshot()["diagnostic"])
            self.diagnostic.toggled.connect(lambda enabled: runtime.config.update({"diagnostic": enabled}))
            settings_row.addWidget(self.diagnostic)
            self.fps = QSpinBox()
            self.fps.setRange(3, 30)
            self.fps.setValue(runtime.config.snapshot()["fps"])
            self.fps.valueChanged.connect(lambda value: runtime.config.update({"fps": value}))
            settings_row.addWidget(QLabel("Capture FPS (next start)"))
            settings_row.addWidget(self.fps)
            layout.addWidget(QLabel("OCR Preview"))
            self.preview = QTextEdit()
            self.preview.setReadOnly(True)
            self.preview.setMaximumHeight(110)
            layout.addWidget(self.preview)
            layout.addWidget(QLabel("Live Catch Log / Warnings — review and correct in the admin dashboard"))
            self.log = QTextEdit()
            self.log.setReadOnly(True)
            layout.addWidget(self.log, 1)
            self.snapshot_queue = queue.Queue(maxsize=1)
            self.refresh_stop = threading.Event()
            self.refresh_thread = threading.Thread(target=self.fetch_state, daemon=True)
            self.refresh_thread.start()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.refresh)
            self.timer.start(250)

        def fetch_state(self):
            while not self.refresh_stop.is_set():
                try:
                    state = runtime.state()
                    events = runtime.store.events(limit=25)
                    self.snapshot_queue.put_nowait((state, events))
                except queue.Full:
                    pass
                except Exception:
                    logging.exception("GUI refresh failed")
                self.refresh_stop.wait(.75)

        def refresh(self):
            if runtime.server.started:
                runtime.update_health(server=f"listening on {host}:{port}")
            try:
                state, events = self.snapshot_queue.get_nowait()
            except queue.Empty:
                return
            self.region.setText("Chat region: " + str(runtime.config.snapshot()["region"]))
            self.status.setText(f"Server: {state['health']['server']} | Capture: {state['health']['capture']} | OCR: {state['health']['ocr']}\nActive Tournament: {state['tournament']['name']} ({state['tournament']['status']}) | Total Catches: {state['counts'].get('confirmed',0)} | Review: {state['counts'].get('review',0)}")
            self.preview.setPlainText(state["health"]["preview"])
            busy = any(t.is_alive() for t in runtime.threads)
            self.start_button.setEnabled(not busy or runtime.monitor_stop.is_set())
            self.stop_button.setEnabled((busy and not runtime.monitor_stop.is_set()) or runtime.restart_pending)
            self.log.setPlainText("\n".join(f"#{e['id']} {e['status'].upper()} {e['username']} / {e['fish']} / {e['weight_kg']} kg — {e['reason']}\n  {e['raw_text']}" for e in events["items"]) + "\n\n" + "\n".join(w["message"] for w in state["health"]["warnings"][-8:]))

        def pick(self):
            if any(t.is_alive() for t in runtime.threads):
                QMessageBox.information(self, "Monitoring", "Stop monitoring before changing the chat region.")
                return
            screen = app.screens()[self.monitor.currentIndex()]
            import mss
            # Match Qt screen origin to MSS physical display. Screenshot pixels are
            # mapped proportionally, handling DPI and negative monitor coordinates.
            geometry = screen.geometry()
            with mss.mss() as capture:
                monitors = capture.monitors[1:]
            physical = windows_monitor_rect(screen.name())
            match = next((m for m in monitors if physical and
                          m["left"] == physical[0] and m["top"] == physical[1]), None)
            if match is None:
                # Qt on Windows scales sizes but keeps desktop screen origins.
                # QScreen.name can be an EDID model name, not a \\.\DISPLAY device.
                scale = screen.devicePixelRatio()
                coordinate_matches = [m for m in monitors if
                    m["left"] == geometry.x() and m["top"] == geometry.y() and
                    abs(m["width"] - round(geometry.width()*scale)) <= 2 and
                    abs(m["height"] - round(geometry.height()*scale)) <= 2]
                if len(coordinate_matches) == 1:
                    match = coordinate_matches[0]
            if match is None:
                if len(monitors) != 1:
                    QMessageBox.warning(self, "Monitor mapping", "Cannot safely map this monitor to physical pixels. Check display names and restart after display changes.")
                    return
                match = monitors[0]
            self.hide()
            app.processEvents()
            image = screen.grabWindow(0)
            selector = Selector(image)
            selector.resize(geometry.size())
            selector.move(geometry.topLeft())
            accepted = selector.exec() == QDialog.DialogCode.Accepted
            self.show()
            if accepted and selector.selected:
                r = selector.selected
                sx, sy = match["width"] / geometry.width(), match["height"] / geometry.height()
                region = [match["left"]+round(r.x()*sx), match["top"]+round(r.y()*sy), round(r.width()*sx), round(r.height()*sy)]
                if region[2] < 60 or region[3] < 30:
                    QMessageBox.warning(self, "Region too small", "Select at least 60×30 physical pixels.")
                else:
                    runtime.config.update({"region": region})

        def start(self):
            try:
                runtime.start_monitoring()
                if runtime.store.active()["status"] == "ready":
                    runtime.tournament_action("start")
            except Exception as exc:
                QMessageBox.warning(self, "Cannot start", str(exc))

        def stop(self):
            runtime.stop_monitoring()

        def test_preview(self):
            try:
                runtime.request_preview()
            except Exception as exc:
                QMessageBox.warning(self, "Cannot preview", str(exc))

        def closeEvent(self, event):
            self.refresh_stop.set()
            runtime.close()
            event.accept()

    window = Window()
    window.show()
    if smoke:
        QTimer.singleShot(1200, window.close)
        QTimer.singleShot(1500, app.quit)
    result = app.exec()
    runtime.close()
    return result


def windows_monitor_rect(device_name):
    """Get physical desktop coordinates by display device name, never DPI-scaled origins."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    class Info(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD), ("device", wintypes.WCHAR*32)]
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                      ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(Info)]
    @callback_type
    def visit(handle, dc, rect, data):
        info = Info()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)) and info.device.casefold() == device_name.casefold():
            r = info.monitor
            found.append((r.left, r.top, r.right-r.left, r.bottom-r.top))
        return True
    user32.EnumDisplayMonitors(None, None, visit, 0)
    return found[0] if found else None


def synthetic_frame(rows, height=126, width=600):
    frame = np.zeros((height, width, 3), np.uint8)
    for i, text in enumerate(rows):
        cv2.putText(frame, text, (8, 20+i*24), cv2.FONT_HERSHEY_SIMPLEX, .5, (255,255,255), 1, cv2.LINE_8)
    return frame


def replay(runtime, directory):
    """Replay sorted PNG screenshots of a fixed chat crop through production workers.

    Repeated input frames preserve timing/stability. This creates a NEW session.
    Real screenshots remain operator-supplied; no invented Fisch syntax is enabled.
    """
    paths = sorted(Path(directory).glob("*.png"))
    if not paths:
        raise ValueError("Replay folder has no PNG frames")
    runtime.store.transition("create", "Screenshot replay " + now())
    runtime.store.transition("start")
    session = runtime.store.active()["id"]
    runtime.frames = queue.Queue(runtime.config.snapshot()["frame_queue"])
    runtime.candidates = queue.Queue(runtime.config.snapshot()["ocr_queue"])
    runtime.run_id = uuid.uuid4().hex
    runtime.initialize_ocr()
    runtime.threads = [threading.Thread(target=lambda: runtime.guarded(runtime.track_loop,True)),
                       threading.Thread(target=lambda: runtime.guarded(runtime.ocr_loop))]
    for thread in runtime.threads:
        thread.start()
    shape = None
    try:
        for path in paths:
            image = cv2.imread(str(path))
            if image is None or (shape and image.shape != shape):
                raise ValueError("Replay images must be readable and have identical dimensions")
            shape = image.shape
            runtime.frames.put((image,session,"running",now(),0,0),timeout=15)
        runtime.monitor_stop.set()
        for thread in runtime.threads:
            thread.join(timeout=120)
        if any(t.is_alive() for t in runtime.threads):
            raise RuntimeError("Replay processing did not finish")
        runtime.store.transition("finish")
        report = runtime.state()
        (runtime.config.directory/"replay-results.json").write_text(json.dumps(report,indent=2),encoding="utf-8")
        if sys.stdout:
            print(json.dumps(report,indent=2))
    finally:
        runtime.monitor_stop.set()


def self_test(directory=None):
    import tempfile
    import unittest

    class Tests(unittest.TestCase):
        def test_named_pattern_library(self):
            import tempfile
            with tempfile.TemporaryDirectory(dir=directory) as folder:
                config = Config(folder)
                settings = config.snapshot()
                self.assertEqual(settings["pattern_presets"][:3], NAMED_PATTERNS)
                update = {"patterns": [ALL_RARITIES_PATTERN], "pattern_presets": NAMED_PATTERNS[:2]}
                validate_config(update, settings)
                config.update(update)
                self.assertEqual(Config(folder).snapshot()["pattern_presets"], NAMED_PATTERNS[:2])
                edited = [{"name": "My common catches", "pattern": COMMON_FISH_PATTERN}]
                update = {"patterns": [COMMON_FISH_PATTERN], "pattern_presets": edited}
                validate_config(update, settings)
                config.update(update)
                self.assertEqual(Config(folder).snapshot()["pattern_presets"], edited)
                for entries in ([{"name": "", "pattern": COMMON_FISH_PATTERN}],
                                [NAMED_PATTERNS[0], NAMED_PATTERNS[0]],
                                [{"name": "Broken", "pattern": "("}],
                                [{"name": "Missing groups", "pattern": "fish"}]):
                    with self.assertRaises((ValueError, re.error)):
                        validate_config({"pattern_presets": entries}, settings)

        def test_named_pattern_rarities_and_overlap(self):
            settings = self.settings | {"message_start": FISCH_START, "best_effort": True}
            for rarity in ("Common", "Limited", "Rare"):
                text = f"[System] Player123 has perfectly caught a {rarity} 1.2kg Trout!"
                for entry in NAMED_PATTERNS:
                    match = re.fullmatch(entry["pattern"], text)
                    self.assertEqual(bool(match), entry["name"] == "All Rarities" or entry["name"] == rarity + " Fish")
                parsed = parse_catch(text, 1, 1, settings | {"patterns": [p["pattern"] for p in NAMED_PATTERNS]})
                self.assertEqual(parsed["status"], "confirmed", parsed)
                self.assertEqual(parsed["rarity"], rarity)
            conflicting = ALL_RARITIES_PATTERN.replace("(?P<fish>.+?)", "(?P<fish>.+?)[t]")
            parsed = parse_catch("[System] Player123 has caught a Common 1.2kg Trout!", 1, 1,
                                 settings | {"patterns": [ALL_RARITIES_PATTERN, conflicting]})
            self.assertEqual(parsed["reason"], "Ambiguous parser matches")

        def setUp(self):
            self.tmp = tempfile.TemporaryDirectory(dir=directory)
            self.config = Config(self.tmp.name)
            self.config.update({"patterns": [LEGACY_PATTERN], "message_start": LEGACY_START, "parser_verified": True, "best_effort": False})
            self.settings = self.config.snapshot()
            self.store = Store(Path(self.tmp.name)/"test.sqlite3")
            self.store.transition("start")
            self.runtime = Runtime(self.config, self.store)
            self.runtime.auto_monitor = False

        def tearDown(self):
            self.tmp.cleanup()

        def feed(self, tracker, rows):
            image = synthetic_frame(rows)
            tracker.feed(image)
            return tracker.feed(image)[0]

        def catch(self, identity, text="PlayerA caught Rare Carp (10.5 kg)", confidence=1):
            return self.store.insert(parse_catch(text,confidence,1,self.settings),identity,self.store.active()["id"],now())

        def test_explicit_corrections_keep_automatic_join_and_avoid_fuzzy_merge(self):
            settings=self.settings | {"patterns":[FISCH_PATTERN],"best_effort":True,"name_corrections":{"FifthSparkGMZZ":"FifthSparkGMZ"}}
            for label,expected in (("FifthSparkGMZZ","FifthSparkGMZ"),("F FifthSparkG MZ Z","FifthSparkGMZ"),("FifthSparkGMZ","FifthSparkGMZ"),("NewPlayer","NewPlayer"),("FifthSparkGMX","FifthSparkGMX")):
                parsed=parse_catch(f"[System] {label} has caught a Common 3.3kg Trout!",1,1,settings)
                self.assertEqual((parsed["status"],parsed["username"]),("confirmed",expected))

        def test_explicit_mapping_merges_history_and_preserves_kick(self):
            fields=parse_catch("PlayerA caught Rare Carp (10.5 kg)",1,1,self.settings)
            fields["username"]="FifthSparkGMZZ"
            self.store.insert(fields,"map:1",self.store.active()["id"],now())
            fields["username"]="FifthSparkGMZ"
            self.store.insert(fields,"map:2",self.store.active()["id"],now())
            self.store.reconcile_players([],{"FifthSparkGMZZ":"FifthSparkGMZ"})
            leaders=self.store.state()["leaderboard"]
            self.assertEqual([(p["username"],p["total_kg"]) for p in leaders],[("FifthSparkGMZ",21)])
            self.store.kick_player("FifthSparkGMZ")
            self.store.reconcile_players([],{"FifthSparkGMZZ":"FifthSparkGMZ"})
            self.assertEqual(self.store.state()["leaderboard"],[])

        def test_mapping_validation_and_persistence(self):
            update={"name_corrections":{"FifthSparkGMZZ":"FifthSparkGMZ"}}
            validate_config(update,self.settings);self.config.update(update)
            self.assertEqual(Config(self.tmp.name).snapshot()["name_corrections"],update["name_corrections"])
            for mapping in ({"Bad":"bad name"},{"FirstA":"SecondB","SecondB":"ThirdC"},{"FirstA":"SecondB","SecondB":"FirstA"},{"Wrong Name":"PlayerA","WrongName":"PlayerB"}):
                with self.assertRaises(ValueError):validate_config({"name_corrections":mapping},self.settings)

        def test_registered_player_resolves_split_name_and_custom_regex(self):
            roster=[{"username":"FifthSparkGMZ","aliases":["Fifth Spark"]},{"username":"ActualUser","aliases":["Meow Skulls"]}]
            settings=self.settings | {"patterns":[FISCH_PATTERN],"registered_players":roster,"best_effort":True}
            for label in ("F FifthSparkG MZ Z","FifthSparkGMZ","FifthSparkGM","Fifth Spark"):
                parsed=parse_catch(f"[System] {label} has perfectly caught a Common ( 0.6kg Sparkling Giant Plagued Anchovy!",.86,1,settings)
                self.assertEqual((parsed["status"],parsed["username"]),("confirmed","FifthSparkGMZ"))
            parsed=parse_catch("[System] N Meow Skulls I has caught a Common : 2.6kg Snapper!",1,1,settings)
            self.assertEqual((parsed["status"],parsed["username"]),("confirmed","ActualUser"))
            custom=FISCH_PATTERN.replace('(?P<username>[A-Za-z0-9_]{3,20}?)', '(?P<username>[A-Za-z0-9_]{3,20}?)(?:[A-Za-z]\s+)?')
            parsed=parse_catch("[System] FifthSparkGMZ has caught a Common 3.3kg Negative Trout!",1,1,settings|{"patterns":[custom]})
            self.assertEqual(parsed["username"],"FifthSparkGMZ")

        def test_roster_ambiguity_unknown_and_exact_priority(self):
            roster=[{"username":"FifthSparkGMZ"},{"username":"FifthSparkGMX"}]
            self.assertIsNone(resolve_player("FifthSparkGM",roster))
            self.assertEqual(resolve_player("FifthSparkGMZ",roster),"FifthSparkGMZ")
            self.assertIsNone(resolve_player("DifferentPlayer",roster))
            self.assertEqual(resolve_player("PlayerAB",[{"username":"PlayerA"},{"username":"PlayerAB"}]),"PlayerAB")
            settings=self.settings | {"patterns":[FISCH_PATTERN],"registered_players":roster,"best_effort":True}
            self.assertEqual(parse_catch("[System] DifferentPlayer has caught a Common 1kg Carp!",1,1,settings)["status"],"confirmed")

        def test_kick_removes_score_and_blocks_future_events_only_this_session(self):
            event=self.catch("kick:1")
            self.store.kick_player("PlayerA")
            self.assertEqual(self.store.state()["leaderboard"],[])
            self.assertIsNone(self.store.state()["recent"])
            self.assertEqual(self.store.state()["kicked_players"],["PlayerA"])
            self.catch("kick:2")
            self.assertEqual(self.store.state()["leaderboard"],[])
            self.assertEqual(self.store.state()["counts"]["disqualified"],2)
            with self.assertRaises(ValueError):
                self.store.correct(event,{"status":"confirmed"},self.settings)
            restarted=Store(self.store.path)
            self.assertEqual(restarted.state()["kicked_players"],["PlayerA"])
            self.store.transition("create","Next")
            self.store.transition("start")
            self.catch("kick:3")
            self.assertEqual(self.store.state()["leaderboard"][0]["username"],"PlayerA")

        def test_roster_save_merges_split_score_without_changing_weight(self):
            fields=parse_catch("PlayerA caught Rare Carp (10.5 kg)",1,1,self.settings)
            fields.update(username="FifthSparkGM",raw_text="[System] FifthSparkGMZ has caught a Common 10.5kg Carp!")
            self.store.insert(fields,"split:1",self.store.active()["id"],now())
            fields.update(username="FifthSparkGMZ")
            self.store.insert(fields,"split:2",self.store.active()["id"],now())
            self.store.reconcile_players([{"username":"FifthSparkGMZ"}])
            leaders=self.store.state()["leaderboard"]
            self.assertEqual(len(leaders),1)
            self.assertEqual((leaders[0]["username"],leaders[0]["total_kg"]),("FifthSparkGMZ",21))
            self.store.kick_player("FifthSparkGMZ")
            self.assertEqual(self.store.state()["leaderboard"],[])

        def test_overlay_dom_has_every_referenced_element_once(self):
            from html.parser import HTMLParser
            class IDs(HTMLParser):
                def __init__(self): super().__init__(); self.ids=[]
                def handle_starttag(self,tag,attrs):
                    self.ids.extend(value for key,value in attrs if key=="id")
            page=IDs();page.feed(OVERLAY_HTML)
            self.assertEqual(len(page.ids),len(set(page.ids)))
            script=OVERLAY_HTML.split("<script>",1)[1].split("</script>",1)[0]
            referenced=set(re.findall(r"\$\('([^']+)'\)",script))
            self.assertTrue(referenced <= set(page.ids),referenced-set(page.ids))
            self.assertIn("rules",page.ids)
            self.assertEqual(page.ids.count("connection"),1)

        def test_rules_and_roster_configuration_and_permanent_theme(self):
            update={"rules":"Common only\nHighest weight wins <script>","registered_players":[{"username":"PlayerA","aliases":["Meow Skulls"]}]}
            validate_config(update,self.settings)
            self.config.update(update)
            self.assertEqual(self.runtime.state()["rules"],update["rules"])
            self.assertEqual(Config(self.tmp.name).snapshot()["registered_players"],update["registered_players"])
            for bad in ({"rules":"x"*2001},{"registered_players":[{"username":"bad name"}]},{"registered_players":[{"username":"PlayerA"},{"username":"playera"}]}):
                with self.assertRaises(ValueError): validate_config(bad,self.settings)
            self.assertIn("--fisch-panel",OVERLAY_HTML)
            self.assertIn("$('rules').textContent",OVERLAY_HTML)
            self.assertNotIn("__FISCH_STYLE__",OVERLAY_HTML)
            self.assertNotIn("textContent='LIVE'",OVERLAY_HTML)

        def test_laptop_ocr_nameplates_and_detached_weight_tokens(self):
            settings=self.settings | {"patterns":[FISCH_PATTERN],"best_effort":True}
            samples=[
                ("[System] N Meow Skulls I has perfectly caught an Unusual 4.1kg Harmonized Eel!","MeowSkulls",4.1),
                ("[System] F Fischisthebest_user has perfectly caught a Legendary 435.0kg Albino Sawfish!","Fischisthebest_user",435),
                ("[System] F Fish_Melol I has perfectly caught a Legendary 1469.0kg Typhoon Tailfin!","Fish_Melol",1469),
                ("[System] Meow Skulls has caught an Unusual 1.3kg Fossilized Eel!","MeowSkulls",1.3),
                ("[System] Fischisthebest_user has perfectly caught a Common 1 2.1kg Hexed Sea Bass!","Fischisthebest_user",2.1),
                ("[System] jefy has perfectly caught t a Mythical 24.2kg Levitas s Chlorophage!","jefy",24.2),
                ("[System] Fischisthebest_user has perfectly caught a Legendary 1 177.9kg Hexed Dolphin!","Fischisthebest_user",1177.9),
                ("[System] Fish_Melol has perfectly caught a Mythical 71.5kg Harmonized Twilight Tentaclefish!","Fish_Melol",71.5),
            ]
            for text,user,kg in samples:
                with self.subTest(text=text):
                    result=parse_catch(text,.93,1,settings)
                    self.assertEqual((result["status"],result["username"],result["weight_units"]),("confirmed",user,round(kg*1000000)))
            for text in ("Levitas Bone!","Levitas Widow Bloom!","Puritas sWidow Bloom!"):
                self.assertEqual(parse_catch(text,1,1,settings)["status"],"review")
            strict=settings | {"best_effort":False}
            self.assertEqual(parse_catch(samples[4][0],1,1,strict)["status"],"review")

        def test_stop_discards_backlog_and_inflight_ocr(self):
            r=self.runtime
            entered,release=threading.Event(),threading.Event()
            def slow(image):
                entered.set()
                release.wait(3)
                return [([[0,0],[500,0],[500,20],[0,20]],"PlayerA caught Rare Carp (10.5 kg)",.99)],None
            r.engine=slow;r.frames=queue.Queue(24);r.candidates=queue.Queue(64)
            for i in range(20):
                r.candidates.put((synthetic_frame(['Catch']),1,str(i),self.store.active()['id'],now()))
            r.track_done.set()
            worker=threading.Thread(target=r.ocr_loop);r.threads=[worker];worker.start()
            try:
                self.assertTrue(entered.wait(2))
                began=time.monotonic();r.stop_monitoring()
                self.assertLess(time.monotonic()-began,.5)
                self.assertTrue(r.candidates.empty())
                release.set();worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(r.candidates.unfinished_tasks,0)
                self.assertEqual(self.store.events()['total'],0)
            finally:
                release.set();worker.join(3)

        def test_restart_waits_for_old_worker_without_blocking(self):
            from unittest.mock import patch
            r=self.runtime;release=threading.Event()
            worker=threading.Thread(target=lambda:release.wait(3));r.threads=[worker];worker.start()
            r.config.update({"region":[0,0,200,100]})
            class Capture:
                monitors=[{}, {"left":0,"top":0,"width":1920,"height":1080}]
                def __enter__(self): return self
                def __exit__(self,*args): pass
            try:
                with patch("mss.mss",return_value=Capture()),patch.object(r,"guarded"):
                    began=time.monotonic();r.monitoring_action("restart")
                    self.assertLess(time.monotonic()-began,.5)
                    self.assertTrue(r.restart_pending)
                    self.assertIs(r.threads[0],worker)
                    release.set();r.restart_thread.join(2)
                    self.assertFalse(r.restart_pending)
                    self.assertFalse(r.monitor_stop.is_set())
                    self.assertTrue(r.health['monitoring'])
            finally:
                release.set();worker.join(3);r.stop_monitoring(wait=True)

        def test_web_tournament_start_automatically_starts_capture(self):
            from unittest.mock import patch
            self.runtime.auto_monitor=True
            self.store.transition("create","Web tournament")
            with patch.object(self.runtime,"start_monitoring") as start:
                self.runtime.tournament_action("schedule",countdown_seconds=60)
                self.assertEqual(start.call_count,1)
                self.runtime.tournament_action("start")
                self.assertEqual(start.call_count,2)
                self.runtime.tournament_action("pause")
                self.assertEqual(start.call_count,2)
                self.runtime.tournament_action("resume")
                self.assertEqual(start.call_count,3)
            with self.assertRaises(ValueError):
                validate_config({"region":[0,0,1,1]},self.config.snapshot())
            self.assertEqual(validate_config({"region":[-100,0,200,100]},self.config.snapshot())["region"],[-100,0,200,100])

        def test_countdown_start_pause_resume_finish_and_restart(self):
            from unittest.mock import patch
            self.store.transition("create","Timer round")
            with patch("time.time",return_value=1000):
                self.runtime.tournament_action("schedule",countdown_seconds=10)
            self.assertEqual(self.store.active()["scheduled_start"],1010)
            restored=Store(self.store.path)
            self.assertEqual(restored.active()["scheduled_start"],1010)
            with patch("time.time",return_value=1009):
                self.assertFalse(self.runtime.timer_tick())
                self.assertEqual(self.store.active()["status"],"ready")
            with patch("time.time",return_value=1010):
                self.assertTrue(self.runtime.timer_tick())
                self.assertFalse(self.runtime.timer_tick())
            self.assertEqual(self.store.active()["status"],"running")
            self.assertIsNone(self.store.active()["scheduled_start"])
            with patch("time.time",return_value=1020):
                self.runtime.tournament_action("pause")
            self.assertEqual(self.store.active()["elapsed_seconds"],10)
            with patch("time.time",return_value=1100):
                self.runtime.tournament_action("resume")
            with patch("time.time",return_value=1105):
                self.runtime.tournament_action("finish")
            self.assertEqual(self.store.active()["elapsed_seconds"],15)
            self.assertIsNone(self.store.active()["started_at"])

        def test_countdown_cancel_manual_start_and_validation(self):
            self.store.transition("create","Timer round")
            for invalid in (0,-1,86401,True,"60",float("nan")):
                with self.assertRaises(ValueError):
                    self.runtime.tournament_action("schedule",countdown_seconds=invalid)
            self.runtime.tournament_action("schedule",countdown_seconds=60)
            self.runtime.tournament_action("cancel_countdown")
            self.assertIsNone(self.store.active()["scheduled_start"])
            self.runtime.tournament_action("schedule",countdown_seconds=60)
            self.runtime.tournament_action("start")
            self.assertEqual(self.store.active()["status"],"running")
            self.assertIsNone(self.store.active()["scheduled_start"])
            with self.assertRaises(ValueError):
                self.runtime.tournament_action("schedule",countdown_seconds=60)
            self.runtime.tournament_action("create","Next round")
            self.assertEqual(self.store.active()["elapsed_seconds"],0)
            self.assertIsNone(self.store.active()["scheduled_start"])

        def test_best_effort_scores_uncertain_readable_catches(self):
            settings=self.settings | {"best_effort":True,"parser_verified":False}
            fields=parse_catch("PlayerA caught Rare Carp (10.5 kg)",.4,.35,settings)
            self.assertEqual(fields["status"],"confirmed")
            self.assertEqual(parse_catch("unreadable",.4,.35,settings)["status"],"review")
            self.assertEqual(parse_catch("PlayerA caught Rare Carp (bad kg)",.4,.35,settings)["status"],"review")
            tid=self.store.active()["id"]
            first=self.store.insert(fields,"fast:1",tid,"2026-10-13T13:00:00+00:00")
            self.assertIsNotNone(first)
            variant=fields | {"fish":"Carp!"}
            self.assertIsNone(self.store.insert(variant,"fast:2",tid,"2026-10-13T13:00:01+00:00"))
            self.assertIsNotNone(self.store.insert(fields,"fast:3",tid,"2026-10-13T13:00:04+00:00"))
            self.assertEqual(self.store.state()["leaderboard"][0]["total_kg"],21)

        def test_single_new_event_exactly_once(self):
            tracker=VisualTracker()
            self.feed(tracker,["OldA", "OldB"])
            candidates=self.feed(tracker,["OldA", "OldB", "PlayerA caught Rare Carp (10.5 kg)"])
            self.assertEqual(len(candidates),1)
            for image,confidence,identity in candidates:
                self.catch(str(identity))
            self.assertEqual(self.store.state()["leaderboard"][0]["catches"],1)
            self.assertIsNone(self.catch("1"))

        def test_unchanged_no_duplicates(self):
            tracker=VisualTracker()
            self.feed(tracker,["PlayerA caught Rare Carp (10.5 kg)"])
            for _ in range(30):
                self.assertEqual(self.feed(tracker,["PlayerA caught Rare Carp (10.5 kg)"]),[])

        def test_identical_consecutive_visual_events(self):
            tracker=VisualTracker()
            self.feed(tracker,["OldA", "OldB"])
            text="PlayerA caught Rare Carp (10.5 kg)"
            first=self.feed(tracker,["OldA","OldB",text])
            second=self.feed(tracker,["OldA","OldB",text,text])
            self.assertEqual(len(first),1)
            self.assertEqual(len(second),1)
            self.assertNotEqual(first[0][2],second[0][2])
            self.catch(str(first[0][2]));self.catch(str(second[0][2]))
            self.assertEqual(self.store.state()["leaderboard"][0]["total_kg"],21)

        def test_multiple_visible_arrivals_and_scroll(self):
            tracker=VisualTracker()
            self.feed(tracker,["OldA","OldB","OldC","OldD","OldE"])
            candidates=self.feed(tracker,["OldC","OldD","OldE","PlayerA catch","PlayerB catch"])
            self.assertEqual(len(candidates),1)
            self.assertEqual(len(row_bands(glyph_mask(candidates[0][0]))),2)
            self.assertGreaterEqual(candidates[0][1],.9)

        def test_wrapped_insertion_preserved(self):
            tracker=VisualTracker()
            self.feed(tracker,["OldA","OldB"])
            candidates=self.feed(tracker,["OldA","OldB","PlayerA caught Legendary", "Megalodon (1000.5 kg)"])
            self.assertEqual(len(row_bands(glyph_mask(candidates[0][0]))),2)
            self.assertEqual(parse_catch("PlayerA caught Legendary\nMegalodon (1000.5 kg)",1,1,self.settings)["status"],"confirmed")

        def test_ocr_failure_low_confidence_and_unverified(self):
            self.catch("failed","garbled kg")
            self.catch("uncertain",confidence=.4)
            settings=self.settings | {"parser_verified":False}
            self.assertEqual(parse_catch("PlayerA caught Rare Carp (10 kg)",1,1,settings)["status"],"review")
            self.assertEqual(self.store.state()["leaderboard"],[])

        def test_live_fisch_screenshot_samples(self):
            settings = self.settings | {"patterns": [FISCH_PATTERN], "message_start": FISCH_START}
            samples = [
                ("[System] ImissTheOldKanye has\nperfectly caught a Mythical 1.2kg\nBig Harmonized Harmonic Dove!", "ImissTheOldKanye", "Mythical", 1.2, "Big Harmonized Harmonic Dove"),
                ("[System] Bug78 has perfectly\ncaught an Unusual 3.4kg\nDarkened Pike!", "Bug78", "Unusual", 3.4, "Darkened Pike"),
                ("[System] Leo has perfectly caught\na Secret 4.0kg Shiny Mustard!.", "Leo", "Secret", 4.0, "Shiny Mustard"),
                ("[System] Leo has perfectly caught\na Mythical 160.4kg Glossy Oarfish!", "Leo", "Mythical", 160.4, "Glossy Oarfish"),
                ("[System] ImissTheOldKanye has\nperfectly caught a Rare 1.4kg\nShiny Harmonized Flying\nPufferfish!", "ImissTheOldKanye", "Rare", 1.4, "Shiny Harmonized Flying Pufferfish"),
            ]
            for raw, username, rarity, kg, fish in samples:
                with self.subTest(raw=raw):
                    parsed = parse_catch(raw, .99, 1, settings)
                    self.assertEqual(parsed["status"], "confirmed")
                    self.assertEqual((parsed["username"],parsed["rarity"],parsed["weight_units"]/1000000,parsed["fish"]), (username,rarity,kg,fish))

        def test_live_fisch_partial_and_malformed_are_review(self):
            settings = self.settings | {"patterns": [FISCH_PATTERN]}
            for raw in (
                "perfectly caught a Legendary 2.3kg Shiny Harmonized Flying Fish!",
                "[System] Leo has perfectly caught a Mythical 160.4kg Glossy",
                "[System] Leo has perfectly caught a Mythical 16O.4kg Glossy Oarfish!",
                "[System] Leo has perfectly caught a Mythical kg Glossy Oarfish!",
            ):
                self.assertEqual(parse_catch(raw,1,1,settings)["status"],"review")
            self.assertEqual(parse_catch("[System] Leo has perfectly caught a Mythical 160.4kg Glossy Oarfish!",.4,1,settings)["status"],"review")

        def test_live_fisch_optional_perfectly_and_ocr_spacing(self):
            settings=self.settings | {"patterns":[FISCH_PATTERN]}
            samples=[
                ("[System] Bruh has caught a Mythical 1185.3kg Electric Colossal Squid!", "Bruh",1185.3),
                ("[System]Thing_6hasperfectlycaughtaMythical 66.7kgWhiskerBill!", "Thing_6",66.7),
                ("[System]BruhhascaughtaMythical836.8kg DarkenedColossalSquid\uff01", "Bruh",836.8),
                ("[System] Bruh123 has perfectly caught an Unusual 14.1kg Jolly Acanthodii!!", "Bruh123",14.1),
                ("[System]darklordscarymanhasperfectlycaughtanUncommon0.4kgTropicalPaleTang!", "darklordscaryman",.4),
            ]
            for raw,username,kg in samples:
                with self.subTest(raw=raw):
                    result=parse_catch(raw,.99,1,settings)
                    self.assertEqual(result["status"],"confirmed")
                    self.assertEqual((result["username"],result["weight_units"]/1000000),(username,kg))
            bad="[System] holidaybag has caughta Legendary 9.okg ObsidianSalmon!"
            self.assertEqual(parse_catch(bad,.99,1,settings)["status"],"review")

        def test_diagnostic_preview_never_scores_visible_history(self):
            text="[System] Leo has caught a Mythical 160.4kg Glossy Oarfish!"
            self.runtime.engine=lambda image: ([([[0,0],[500,0],[500,20],[0,20]],text,.99)],None)
            self.runtime.preview_image(synthetic_frame([text]))
            self.assertEqual(self.runtime.health["preview"],text)
            self.assertIsNotNone(self.runtime.health["last_ocr"])
            self.assertEqual(self.store.events()["total"],0)
            self.assertEqual(self.store.state()["leaderboard"],[])

        def test_colored_ocr_fragments_keep_physical_reading_order(self):
            # Real bounding boxes observed on the user's captured System message.
            fragments=[
                ([[26,4],[162,4],[162,33],[26,33]],"[System]",.989),
                ([[165,0],[736,1],[736,35],[165,33]],"Bruh has caught a Mythical 836.8kg",.988),
                ([[19,41],[186,41],[186,64],[19,64]],"Darkened",.995),
                ([[179,36],[426,36],[426,66],[179,66]],"ColossalSquid!",.962),
            ]
            rows=ocr_text_rows(fragments)
            self.assertEqual(rows[0][0],"[System] Bruh has caught a Mythical 836.8kg")
            self.assertEqual(rows[1][0],"Darkened ColossalSquid!")
            settings=self.settings | {"patterns":[FISCH_PATTERN]}
            parsed=parse_catch(" ".join(text for text,score in rows),min(score for text,score in rows),1,settings)
            self.assertEqual((parsed["status"],parsed["username"],parsed["weight_units"]),("confirmed","Bruh",836800000))
            duplicate_prefix=parse_catch("[System] ] Bruh has caught a Mythical 836.8kg Darkened Colossal Squid!",.96,1,settings)
            self.assertEqual((duplicate_prefix["status"],duplicate_prefix["username"]),("confirmed","Bruh"))

        def test_shipped_parser_migration_preserves_customization(self):
            self.config.update({"patterns":[LEGACY_PATTERN],"message_start":LEGACY_START,"parser_verified":False})
            self.config.values["admin_token"] = "legacy-test-token"
            self.config.save()
            migrated = Config(self.tmp.name).snapshot()
            self.assertEqual(migrated["patterns"],[FISCH_PATTERN])
            self.assertTrue(migrated["parser_verified"])
            self.assertNotIn("admin_token", migrated)
            self.assertNotIn("admin_token", json.loads(self.config.path.read_text(encoding="utf-8")))
            self.assertTrue(Path(self.tmp.name,"config.pre-fisch-samples.json").is_file())
            self.config.update({"patterns":[r"^custom (?P<username>.+) (?P<fish>.+) (?P<rarity>.+) (?P<weight>.+)$"]})
            self.assertEqual(Config(self.tmp.name).snapshot()["patterns"],self.config.snapshot()["patterns"])

        def test_correction_delete_audit_and_score(self):
            a=self.catch("a");b=self.catch("b")
            self.store.correct(a,{"weight_kg":"25"},self.settings)
            self.assertEqual(self.store.state()["leaderboard"][0]["total_kg"],35.5)
            self.store.correct(b,{},self.settings,True)
            self.assertEqual(self.store.state()["leaderboard"][0]["total_kg"],25)
            self.assertEqual(len(self.store.audits(b)),2)
            self.assertEqual(self.store.events()["total"],2)

        def test_restart_and_reset_preserve_history(self):
            self.catch("a")
            restarted=Store(self.store.path)
            self.assertEqual(restarted.active()["status"],"running")
            self.assertEqual(restarted.state()["leaderboard"][0]["total_kg"],10.5)
            old=self.store.active()["id"]
            restarted.transition("reset")
            self.assertEqual(restarted.state()["leaderboard"],[])
            self.assertEqual(restarted.events(old)["total"],1)

        def test_pause_resume_and_session_tag(self):
            old=self.store.active()["id"]
            self.store.transition("pause");self.store.transition("resume")
            self.store.transition("reset")
            self.store.insert(parse_catch("PlayerA caught Rare Carp (10 kg)",1,1,self.settings),"delayed",old,now())
            self.assertEqual(self.store.state()["leaderboard"],[])
            self.assertEqual(self.store.events(old)["total"],1)

        def test_weight_locale_validation(self):
            for value in ("1,234.56", "1 234.56"):
                self.assertEqual(parse_weight(value,self.settings),1234560000)
            self.assertEqual(parse_weight("1.234,56",self.settings|{"decimal_separator":","}),1234560000)
            for value in ("1,23.4","NaN","-10","1.2.3","999999999999"):
                with self.assertRaises((ValueError,InvalidOperation)):
                    parse_weight(value,self.settings)

        def test_production_tracking_ocr_persistence_pipeline(self):
            text="PlayerA caught Rare Carp (10.5 kg)"
            def fake_engine(image):
                bands=row_bands(glyph_mask(image))
                return [([[0,a],[500,a],[500,b],[0,b]],text,.99) for a,b in bands],None
            r=self.runtime
            r.engine=fake_engine
            r.frames=queue.Queue(24);r.candidates=queue.Queue(24)
            r.run_id="test"
            session=self.store.active()["id"]
            frames=[['OldA','OldB'],['OldA','OldB',text],['OldA','OldB',text,text]]
            for rows in frames:
                for _ in range(2):
                    r.frames.put((synthetic_frame(rows),session,'running',now(),0,0))
            for _ in range(5):
                r.frames.put((synthetic_frame(frames[-1]),session,'running',now(),0,0))
            r.monitor_stop.set()
            r.track_loop()
            r.ocr_loop()
            self.assertEqual(self.store.state()['leaderboard'][0]['total_kg'],21)
            self.assertEqual(self.store.state()['leaderboard'][0]['catches'],2)

        def test_ocr_exception_is_saved_for_review(self):
            r=self.runtime
            def failure(image):
                raise RuntimeError('simulated OCR error')
            r.engine=failure;r.candidates=queue.Queue(4)
            r.candidates.put((synthetic_frame(['Catch']),1,'failure',self.store.active()['id'],now()))
            r.track_done.set()
            r.ocr_loop()
            rows=self.store.events()['items']
            self.assertEqual(rows[0]['status'],'review')
            self.assertTrue((self.config.directory/'evidence'/rows[0]['evidence']).is_file())
            self.assertEqual(self.store.state()['leaderboard'],[])

        def test_unknown_redraw_never_auto_confirms(self):
            tracker=VisualTracker()
            self.feed(tracker,["OldA","OldB"])
            candidates=self.feed(tracker,["Totally different screen", "PlayerA caught Rare Carp (10 kg)"])
            self.assertLess(candidates[0][1],.9)

        def test_reconnect_full_snapshot_and_disconnect_isolation(self):
            # Exercise the actual SSE iterator with a fake request rather than
            # TestClient's buffering of an infinite response.
            app=create_app(self.runtime)
            endpoint=next(r.endpoint for r in app.routes if getattr(r,"path",None)=="/stream")
            class Request:
                async def is_disconnected(self):
                    return False
            async def check():
                for expected in (10.5,21.0):
                    self.catch(str(expected))
                    response=await endpoint(Request())
                    generator=response.body_iterator
                    initial=await generator.__anext__()
                    state=json.loads(initial.split("data: ")[1])
                    self.assertEqual(state["leaderboard"][0]["total_kg"],expected)
                    await generator.aclose()  # OBS disconnects, producer still writes
            asyncio.run(check())
            self.assertEqual(self.store.state()["leaderboard"][0]["catches"],2)

    suite=unittest.defaultTestLoader.loadTestsFromTestCase(Tests)
    output=io.StringIO()
    result=unittest.TextTestRunner(stream=output,verbosity=2).run(suite)
    report=output.getvalue()
    if sys.stdout:
        print(report)
    if directory:
        Path(directory,"test-results.txt").write_text(report,encoding="utf-8")
    return result.wasSuccessful()


def smoke_test(directory):
    import urllib.request
    import tempfile
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        config=Config(tmp)
        runtime=Runtime(config,Store(Path(tmp)/"smoke.sqlite3"))
        runtime.initialize_ocr()
        results,_=runtime.engine(cv2.resize(synthetic_frame(["PlayerA caught Rare Carp (10.5 kg)"]),None,fx=2,fy=2))
        recognized=" ".join(r[1] for r in results or [])
        if "PlayerA" not in recognized or "Carp" not in recognized:
            raise RuntimeError(f"Bundled OCR smoke recognition failed: {recognized}")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0));port=sock.getsockname()[1]
        thread=launch_server(runtime,"127.0.0.1",port)
        try:
            for _ in range(100):
                if runtime.server.started:
                    break
                time.sleep(.05)
            base=f"http://127.0.0.1:{port}"
            state=json.load(urllib.request.urlopen(base+"/api/state",timeout=5))
            assert state["tournament"]["status"]=="ready"
            assert urllib.request.urlopen(base+"/overlay",timeout=5).status==200
            assert urllib.request.urlopen(base+"/admin",timeout=5).status==200
            import urllib.error
            req=urllib.request.Request(base+"/api/tournament", data=b'{"action":"create"}', headers={"Content-Type":"application/json"}, method="POST")
            assert json.load(urllib.request.urlopen(req,timeout=5))["ok"]
            req=urllib.request.Request(base+"/api/config")
            assert "admin_token" not in json.load(urllib.request.urlopen(req,timeout=5))
            req=urllib.request.Request(base+"/api/events",data=json.dumps({"username":"PlayerA","fish":"Carp","rarity":"Rare","weight_kg":"10.5"}).encode(),headers={"Content-Type":"application/json"},method="POST")
            event_id=json.load(urllib.request.urlopen(req,timeout=5))["id"]
            assert event_id==1
            try:
                urllib.request.urlopen(urllib.request.Request(base+"/api/tournament",data=b'{"action":"start"}',
                    headers={"Content-Type":"application/json","Origin":"http://evil.example"},method="POST"),timeout=5)
                raise AssertionError("Cross-origin mutation accepted")
            except urllib.error.HTTPError as exc:
                assert exc.code == 403
            with urllib.request.urlopen(base+"/stream",timeout=5) as stream:
                assert stream.readline().decode().strip()=="event: state"
                data=json.loads(stream.readline().decode().removeprefix("data: "))
                assert data["leaderboard"][0]["total_kg"]==10.5
            req=urllib.request.Request(base+"/api/config",data=json.dumps({"registered_players":[{"username":"PlayerA"},{"username":"FifthSparkGMZ"}],"name_corrections":{"FifthSparkGMZZ":"FifthSparkGMZ"},"rules":"Common fish only\nHighest weight wins"}).encode(),headers={"Content-Type":"application/json"},method="PATCH")
            assert json.load(urllib.request.urlopen(req,timeout=5))["ok"]
            req=urllib.request.Request(base+"/api/parse",data=json.dumps({"text":"[System] F FifthSparkG MZ Z has perfectly caught a Common ( 0.6kg Plagued Anchovy!"}).encode(),headers={"Content-Type":"application/json"},method="POST")
            parsed=json.load(urllib.request.urlopen(req,timeout=5))
            assert parsed["username"]=="FifthSparkGMZ" and parsed["status"]=="confirmed"
            req=urllib.request.Request(base+"/api/parse",data=json.dumps({"text":"[System] NewPlayer has caught a Common 3.3kg Trout!"}).encode(),headers={"Content-Type":"application/json"},method="POST")
            parsed=json.load(urllib.request.urlopen(req,timeout=5))
            assert parsed["username"]=="NewPlayer" and parsed["status"]=="confirmed"
            req=urllib.request.Request(base+"/api/players/kick",data=b'{"username":"PlayerA"}',headers={"Content-Type":"application/json"},method="POST")
            assert json.load(urllib.request.urlopen(req,timeout=5))["ok"]
            state=json.load(urllib.request.urlopen(base+"/api/state",timeout=5))
            assert state["leaderboard"]==[] and state["kicked_players"]==["PlayerA"]
            assert state["rules"]=="Common fish only\nHighest weight wins"
            launch_gui(runtime,"127.0.0.1",port,smoke=True)
        finally:
            runtime.close();thread.join(timeout=10)
        report="PASS: packaged models load offline; synthetic OCR recognizes player/fish; HTTP routes; password-free mutation and cross-origin protection; full SSE snapshot; name corrections and automatic joining, rules and password-free kick; Qt window launches and closes.\nOCR: "+recognized+"\nClean Windows without Python and real two-player Fisch: NOT VERIFIED.\n"
        Path(directory,"smoke-results.txt").write_text(report,encoding="utf-8")
        if sys.stdout:
            print(report)


def build():
    import subprocess
    command=[sys.executable,"-m","PyInstaller","--noconfirm","--clean","--onedir","--windowed",
             "--name","SpaceGoatFisch","--collect-all","rapidocr_onnxruntime","--collect-all","onnxruntime",
             "--hidden-import","uvicorn.logging","--hidden-import","uvicorn.loops.auto",
             "--hidden-import","uvicorn.protocols.http.h11_impl","--hidden-import","uvicorn.lifespan.on",
             str(Path(__file__).resolve())]
    subprocess.run(command,check=True)


def main():
    parser=argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--data-dir",type=Path,default=data_directory())
    parser.add_argument("--host",help="Specific private LAN IPv4 address; default: detected LAN interface")
    parser.add_argument("--port",type=int)
    parser.add_argument("--self-test",action="store_true")
    parser.add_argument("--smoke-test",action="store_true")
    parser.add_argument("--build",action="store_true")
    parser.add_argument("--replay",type=Path,help="Replay sorted fixed-region PNG frames into a new session")
    args=parser.parse_args()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    if args.self_test:
        return 0 if self_test(args.data_dir) else 1
    if args.smoke_test:
        smoke_test(args.data_dir)
        return 0
    if args.build:
        build();return 0
    handler=RotatingFileHandler(args.data_dir/"server.log",maxBytes=2000000,backupCount=3,encoding="utf-8")
    logging.basicConfig(handlers=[handler],level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s")
    config=Config(args.data_dir)
    if args.replay:
        replay(Runtime(config,Store(args.data_dir/'tournaments.sqlite3')),args.replay)
        return 0
    settings=config.snapshot()
    host=args.host or settings["host"] or lan_address()
    ip=ipaddress.ip_address(host)
    if not ip.is_private or ip.is_unspecified or ip.version != 4:
        raise ValueError("Choose a specific private LAN IPv4 address or loopback")
    port=args.port or settings["port"]
    if not 1024 <= port <= 65535:
        raise ValueError("Port must be 1024–65535")
    runtime=Runtime(config,Store(args.data_dir/"tournaments.sqlite3"))
    thread=launch_server(runtime,host,port)
    try:
        return launch_gui(runtime,host,port)
    finally:
        runtime.close();thread.join(timeout=10)


if __name__=="__main__":
    try:
        result=main()
    except Exception:
        import traceback
        report=traceback.format_exc()
        directory=data_directory()
        directory.mkdir(parents=True,exist_ok=True)
        (directory/"startup-error.txt").write_text(report,encoding="utf-8")
        if sys.stderr:
            sys.stderr.write(report)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,report,APP_NAME+" startup error",0x10)
        result=1
    sys.exit(result)
