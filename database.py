"""Banco local (SQLite) com o histórico de temas da Venezuela.

Garante três regras do projeto:
1. Nenhum tema se repete: título, assunto principal (entity) e títulos
   muito parecidos são bloqueados.
2. No máximo um vídeo por dia: cada data só pode ter um tema ativo.
3. Fato só vai para o render com fonte: um tema da trilha "fact" só passa
   para "verified" com pelo menos 2 fontes de domínios diferentes.

Uso pela linha de comando:
    python database.py init
    python database.py list
    python database.py check "Francisco de Miranda no Arco do Triunfo"
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

DEFAULT_DB_PATH = Path(__file__).parent / "data" / "stories.db"

TRACKS = ("fact", "fiction")
CATEGORIES = (
    "personagem",
    "lugar",
    "fenomeno_natural",
    "evento_historico",
    "tragedia",
    "cultura",
    "lenda",
    "curiosidade",
)
STATUSES = ("reserved", "scripted", "verified", "rendered", "published", "failed", "rejected")

# Transições permitidas. "failed" = erro técnico (o tema pode ser tentado de novo);
# "rejected" = o fato não se confirmou (o tema fica bloqueado para sempre).
TRANSITIONS = {
    "reserved": {"scripted", "failed"},
    "scripted": {"verified", "failed", "rejected"},
    "verified": {"rendered", "failed"},
    "rendered": {"published", "failed"},
    "published": set(),
    "failed": set(),
    "rejected": set(),
}

MIN_FACT_SOURCES = 2
SIMILARITY_THRESHOLD = 0.85

# Segundo nível comum em domínios de país (ex.: .com.ve, .gob.ve, .com.br).
_SECOND_LEVEL = {"com", "gob", "gov", "org", "net", "edu", "co", "ac", "mil"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS topics (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    entity          TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN ({", ".join(f"'{c}'" for c in CATEGORIES)})),
    track           TEXT NOT NULL CHECK (track IN ({", ".join(f"'{t}'" for t in TRACKS)})),
    status          TEXT NOT NULL DEFAULT 'reserved'
                    CHECK (status IN ({", ".join(f"'{s}'" for s in STATUSES)})),
    scheduled_date  TEXT UNIQUE,
    video_path      TEXT,
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    published_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_topics_entity ON topics (entity);
CREATE INDEX IF NOT EXISTS idx_topics_status ON topics (status);

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    topic_id    INTEGER NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    domain      TEXT NOT NULL,
    title       TEXT,
    added_at    TEXT NOT NULL,
    UNIQUE (topic_id, url)
);
"""


class DuplicateTopicError(ValueError):
    """O tema (ou um muito parecido) já foi usado."""


class DaySlotTakenError(ValueError):
    """Já existe um tema ativo para essa data."""


class InvalidTransitionError(ValueError):
    """Mudança de status não permitida."""


class NotEnoughSourcesError(ValueError):
    """Tema factual sem fontes independentes suficientes."""


@dataclass(frozen=True)
class Topic:
    id: int
    slug: str
    title: str
    entity: str
    category: str
    track: str
    status: str
    scheduled_date: str | None
    video_path: str | None
    error: str | None
    created_at: str
    updated_at: str
    published_at: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(text: str) -> str:
    """Minúsculas, sem acento, só letras/números separados por hífen."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = "".join(ch if ch.isalnum() else " " for ch in ascii_text.lower()).split()
    return "-".join(words)


def base_domain(url: str) -> str:
    """Domínio da fonte sem subdomínio (pt.wikipedia.org e es.wikipedia.org são a mesma fonte)."""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise ValueError(f"URL inválida: {url!r}")
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _row_to_topic(row: sqlite3.Row) -> Topic:
    return Topic(**dict(row))


class StoryDatabase:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path or os.getenv("DATABASE_PATH") or DEFAULT_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- consulta -----------------------------------------------------

    def get(self, topic_id: int) -> Topic:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        if row is None:
            raise KeyError(f"Tema {topic_id} não existe")
        return _row_to_topic(row)

    def list_topics(self, status: str | None = None) -> list[Topic]:
        query = "SELECT * FROM topics"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY COALESCE(scheduled_date, created_at)"
        with self._connect() as conn:
            return [_row_to_topic(r) for r in conn.execute(query, params)]

    def find_duplicate(self, title: str, entity: str) -> Topic | None:
        """Devolve o tema que bloqueia este, ou None se o tema é inédito.

        Temas com status "failed" não bloqueiam: nunca foram publicados.
        """
        slug, entity_key = normalize(title), normalize(entity)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM topics WHERE status != 'failed'").fetchall()
        for row in rows:
            if row["slug"] == slug or row["entity"] == entity_key:
                return _row_to_topic(row)
        for row in rows:
            ratio = SequenceMatcher(None, row["slug"], slug).ratio()
            if ratio >= SIMILARITY_THRESHOLD:
                return _row_to_topic(row)
        return None

    def used_topics_summary(self) -> list[str]:
        """Lista "assunto: título" de tudo que já foi usado, para o roteirista evitar."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT entity, title FROM topics WHERE status != 'failed' ORDER BY created_at"
            ).fetchall()
        return [f"{r['entity']}: {r['title']}" for r in rows]

    def topic_for_date(self, day: date) -> Topic | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM topics WHERE scheduled_date = ?", (day.isoformat(),)
            ).fetchone()
        return _row_to_topic(row) if row else None

    def sources(self, topic_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT url, domain, title, added_at FROM sources WHERE topic_id = ? ORDER BY id",
                (topic_id,),
            ).fetchall()

    # ---- escrita ------------------------------------------------------

    def reserve_topic(
        self,
        title: str,
        entity: str,
        category: str,
        track: str,
        day: date | None = None,
    ) -> Topic:
        """Reserva um tema inédito para o dia (padrão: hoje).

        Um tema que falhou por erro técnico pode ser reservado de novo.
        """
        if category not in CATEGORIES:
            raise ValueError(f"Categoria inválida: {category!r}. Use uma de {CATEGORIES}")
        if track not in TRACKS:
            raise ValueError(f"Trilha inválida: {track!r}. Use 'fact' ou 'fiction'")
        day = day or date.today()
        slug, entity_key = normalize(title), normalize(entity)
        if not slug or not entity_key:
            raise ValueError("Título e entity não podem ficar vazios")

        duplicate = self.find_duplicate(title, entity)
        if duplicate:
            raise DuplicateTopicError(
                f"Tema repetido: parecido com #{duplicate.id} '{duplicate.title}' "
                f"({duplicate.status})"
            )

        now = _now()
        with self._connect() as conn:
            taken = conn.execute(
                "SELECT id, title FROM topics WHERE scheduled_date = ?", (day.isoformat(),)
            ).fetchone()
            if taken:
                raise DaySlotTakenError(
                    f"{day.isoformat()} já tem o tema #{taken['id']} '{taken['title']}'"
                )
            retry = conn.execute(
                "SELECT id FROM topics WHERE slug = ? AND status = 'failed'", (slug,)
            ).fetchone()
            if retry:
                conn.execute(
                    """UPDATE topics SET title = ?, entity = ?, category = ?, track = ?,
                       status = 'reserved', scheduled_date = ?, error = NULL, updated_at = ?
                       WHERE id = ?""",
                    (title, entity_key, category, track, day.isoformat(), now, retry["id"]),
                )
                topic_id = retry["id"]
            else:
                cursor = conn.execute(
                    """INSERT INTO topics
                       (slug, title, entity, category, track, scheduled_date, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (slug, title, entity_key, category, track, day.isoformat(), now, now),
                )
                topic_id = cursor.lastrowid
        return self.get(topic_id)

    def add_source(self, topic_id: int, url: str, title: str | None = None) -> None:
        domain = base_domain(url)
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO sources (topic_id, url, domain, title, added_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (topic_id, url, domain, title, _now()),
            )

    def set_status(
        self,
        topic_id: int,
        status: str,
        *,
        video_path: str | None = None,
        error: str | None = None,
    ) -> Topic:
        topic = self.get(topic_id)
        if status not in TRANSITIONS[topic.status]:
            raise InvalidTransitionError(f"#{topic_id}: {topic.status} -> {status} não é permitido")

        if status == "verified" and topic.track == "fact":
            with self._connect() as conn:
                domains = conn.execute(
                    "SELECT COUNT(DISTINCT domain) FROM sources WHERE topic_id = ?", (topic_id,)
                ).fetchone()[0]
            if domains < MIN_FACT_SOURCES:
                raise NotEnoughSourcesError(
                    f"#{topic_id} tem {domains} fonte(s) independente(s); "
                    f"precisa de {MIN_FACT_SOURCES} para ir ao render"
                )

        now = _now()
        fields = {"status": status, "updated_at": now}
        if video_path is not None:
            fields["video_path"] = video_path
        if error is not None:
            fields["error"] = error
        if status == "published":
            fields["published_at"] = now
        if status == "failed":
            # Libera o dia para outro tema; o tema em si pode ser tentado depois.
            fields["scheduled_date"] = None

        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE topics SET {assignments} WHERE id = ?", (*fields.values(), topic_id)
            )
        return self.get(topic_id)


def _cli(argv: list[str]) -> int:
    db = StoryDatabase()
    command = argv[1] if len(argv) > 1 else "list"
    if command == "init":
        print(f"Banco pronto em {db.path}")
    elif command == "list":
        topics = db.list_topics()
        if not topics:
            print("Nenhum tema ainda.")
        for t in topics:
            print(f"#{t.id:<4} {t.scheduled_date or '-':<10}  {t.status:<9} {t.track:<7} {t.title}")
    elif command == "check" and len(argv) > 2:
        title = argv[2]
        entity = argv[3] if len(argv) > 3 else title
        duplicate = db.find_duplicate(title, entity)
        print(f"REPETIDO: #{duplicate.id} '{duplicate.title}'" if duplicate else "Inédito.")
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
