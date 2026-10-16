import json

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from . import cache, db
from .voting import BallotError, clean_ballot, count, describe

app = FastAPI(title="movie-night")

LOCK_TTL = 30


@app.get("/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as e:
        out["pg_error"] = str(e)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)
    return out if out["postgres"] and out["redis"] else JSONResponse(out, status_code=503)


def _poll(pid):
    poll = db.one("SELECT id, title, opened_by, status, created_at, closed_at, result"
                  " FROM polls WHERE id=%s", (pid,))
    if not poll:
        raise HTTPException(404, "no such poll")
    return poll


def _films(pid):
    return db.query("SELECT id, title, added_by FROM films WHERE poll_id=%s ORDER BY id", (pid,))


def _ballots(pid):
    rows = db.query("SELECT b.id, array_agg(bc.film_id ORDER BY bc.rank) AS ranking"
                    " FROM ballots b JOIN ballot_choices bc ON bc.ballot_id=b.id"
                    " WHERE b.poll_id=%s GROUP BY b.id ORDER BY b.id", (pid,))
    return [[int(f) for f in r["ranking"]] for r in rows]


def _live_firsts(pid, films):
    """First-preference counts, kept in a Redis hash, warmed from Postgres."""
    c = cache.client()
    key = f"poll:{pid}:first"
    raw = c.hgetall(key)
    if not raw:
        rows = db.query(
            "SELECT bc.film_id AS fid, count(*) AS n FROM ballots b"
            " JOIN ballot_choices bc ON bc.ballot_id=b.id AND bc.rank=1"
            " WHERE b.poll_id=%s GROUP BY bc.film_id", (pid,))
        warm = {str(f["id"]): 0 for f in films}
        warm.update({str(r["fid"]): int(r["n"]) for r in rows})
        if warm:
            c.hset(key, mapping=warm)
            c.expire(key, 86400)
        return {int(k): int(v) for k, v in warm.items()}
    return {int(k): int(v) for k, v in raw.items()}


@app.get("/polls")
def list_polls():
    rows = db.query("SELECT p.id, p.title, p.opened_by, p.status, p.created_at,"
                    " (SELECT count(*) FROM films f WHERE f.poll_id=p.id) AS films,"
                    " (SELECT count(*) FROM ballots b WHERE b.poll_id=p.id) AS ballots"
                    " FROM polls p ORDER BY p.id")
    return {"polls": rows}


@app.post("/polls", status_code=201)
def open_poll(payload: dict = Body(...)):
    title = str(payload.get("title", "")).strip()
    who = str(payload.get("opened_by", "")).strip()
    if not title:
        raise HTTPException(400, "a poll needs a title")
    if not who:
        raise HTTPException(400, "opened_by is required")
    return db.one("INSERT INTO polls (title, opened_by) VALUES (%s,%s)"
                  " RETURNING id, title, opened_by, status, created_at", (title, who))


@app.get("/polls/{pid}")
def show_poll(pid: int):
    poll = _poll(pid)
    films = _films(pid)
    firsts = _live_firsts(pid, films)
    return {**poll,
            "films": [{**f, "first_preferences": firsts.get(f["id"], 0)} for f in films],
            "ballots_cast": db.one("SELECT count(*) AS n FROM ballots WHERE poll_id=%s",
                                   (pid,))["n"]}

@app.patch("/polls/{pid}")
def rename_poll(pid: int, payload: dict = Body(...)):
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "a poll needs a title")
    if _poll(pid)["status"] != "open":
        raise HTTPException(409, "that poll is closed - it can't be renamed")
    # status='open' in the WHERE closes the gap between the check above and a
    # close() that lands in between; zero rows back means we lost that race.
    row = db.one("UPDATE polls SET title=%s WHERE id=%s AND status='open'"
                 " RETURNING id, title, opened_by, status, created_at", (title, pid))
    if not row:
        raise HTTPException(409, "that poll was closed while you were renaming it")
    return row

@app.delete("/polls/{pid}", status_code=204)
def delete_poll(pid: int):
    c = cache.client()
    # Same lock close() uses: a count in progress makes us back off, and a
    # close() that starts while we delete backs off from us.
    lock = f"lock:close:{pid}"
    if not c.set(lock, "deleting", nx=True, ex=LOCK_TTL):
        raise HTTPException(409, "a count is running for that poll - try again shortly")
    try:
        # Films, ballots and ballot_choices go with it (ON DELETE CASCADE).
        if not db.one("DELETE FROM polls WHERE id=%s RETURNING id", (pid,)):
            raise HTTPException(404, "no such poll")
        cache.drop(f"poll:{pid}:first")  # Redis has no cascade
    finally:
        c.delete(lock)

@app.post("/polls/{pid}/films", status_code=201)
def add_film(pid: int, payload: dict = Body(...)):
    poll = _poll(pid)
    if poll["status"] != "open":
        raise HTTPException(409, "that poll is closed - no more films")
    title = str(payload.get("title", "")).strip()
    if not title:
        raise HTTPException(400, "a film needs a title")
    who = str(payload.get("added_by", "")).strip() or "anonymous"
    if db.one("SELECT id FROM films WHERE poll_id=%s AND lower(title)=lower(%s)", (pid, title)):
        raise HTTPException(409, "that film is already on the list")
    row = db.one("INSERT INTO films (poll_id, title, added_by) VALUES (%s,%s,%s)"
                 " RETURNING id, poll_id, title, added_by", (pid, title, who))
    # Warm first. hsetnx on a cold cache would create a hash holding this one
    # film, and _live_firsts only warms when the hash is EMPTY - so every
    # other film in the poll would read as zero from then on.
    _live_firsts(pid, _films(pid))
    cache.client().hsetnx(f"poll:{pid}:first", str(row["id"]), 0)
    return row


@app.post("/polls/{pid}/ballots", status_code=201)
def cast(pid: int, payload: dict = Body(...)):
    poll = _poll(pid)
    if poll["status"] != "open":
        raise HTTPException(409, "that poll is closed - voting is over")
    voter = str(payload.get("voter", "")).strip()
    if not voter:
        raise HTTPException(400, "voter is required")
    films = {f["id"] for f in _films(pid)}
    if not films:
        raise HTTPException(409, "nobody has added a film yet")
    try:
        ranking = clean_ballot([int(x) for x in (payload.get("ranking") or [])], films)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e) if isinstance(e, BallotError)
                            else "ranking must be a list of film ids")
    if not ranking:
        raise HTTPException(400, "rank at least one film")
    if db.one("SELECT id FROM ballots WHERE poll_id=%s AND voter=%s", (pid, voter)):
        raise HTTPException(409, "that person already voted")

    # Warm the cache BEFORE the insert. Warming afterwards reads a Postgres
    # count that already includes this ballot, and the hincrby below then
    # counts it a second time - a bug you will never see while the cache is
    # warm, which is every run except the first one after a restart.
    _live_firsts(pid, _films(pid))
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO ballots (poll_id, voter) VALUES (%s,%s) RETURNING id",
                    (pid, voter))
        bid = cur.fetchone()["id"]
        for rank, fid in enumerate(ranking, start=1):
            cur.execute("INSERT INTO ballot_choices (ballot_id, rank, film_id)"
                        " VALUES (%s,%s,%s)", (bid, rank, fid))
    cache.client().hincrby(f"poll:{pid}:first", str(ranking[0]), 1)
    return {"ballot_id": bid, "poll_id": pid, "voter": voter, "ranking": list(ranking)}


@app.post("/polls/{pid}/close")
def close(pid: int):
    _poll(pid)
    c = cache.client()
    lock = f"lock:close:{pid}"
    # Two guards, and both are needed. The lock stops two simultaneous
    # requests both starting a count. The conditional UPDATE stops the second
    # request counting again after the first one finished and let the lock go.
    if not c.set(lock, "counting", nx=True, ex=LOCK_TTL):
        raise HTTPException(409, "a count is already running for that poll")
    try:
        claimed = db.one("UPDATE polls SET status='closed', closed_at=now()"
                         " WHERE id=%s AND status='open' RETURNING id", (pid,))
        if not claimed:
            raise HTTPException(409, "that poll was already closed")
        films = _films(pid)
        if not films:
            db.query("UPDATE polls SET status='open', closed_at=NULL WHERE id=%s",
                     (pid,), fetch=False)
            raise HTTPException(409, "nothing to count - no films in that poll")
        names = {f["id"]: f["title"] for f in films}
        result = count(_ballots(pid), [f["id"] for f in films])
        result["names"] = {str(k): v for k, v in names.items()}
        result["summary"] = describe(result, names)
        db.query("UPDATE polls SET result=%s::jsonb WHERE id=%s",
                 (json.dumps(result), pid), fetch=False)
    finally:
        c.delete(lock)
    return _decorate(result, names)


@app.get("/polls/{pid}/results")
def results(pid: int):
    poll = _poll(pid)
    if poll["status"] != "closed":
        raise HTTPException(409, "that poll is still open - close it to see the count")
    result = poll["result"]
    if not result:
        raise HTTPException(404, "that poll closed without a stored count")
    names = {int(k): v for k, v in (result.get("names") or {}).items()}
    return _decorate(result, names)


def _decorate(result, names):
    out = {"status": result["status"],
           "winner": names.get(result["winner"], result["winner"]),
           "winner_id": result["winner"],
           "plurality_winner": names.get(result["plurality_winner"],
                                         result["plurality_winner"]),
           "irv_changed_the_answer": result["winner"] != result["plurality_winner"],
           "summary": result.get("summary") or describe(result, names),
           "rounds": []}
    for r in result["rounds"]:
        out["rounds"].append({
            "round": r["round"],
            "active_ballots": r["active"],
            "exhausted_ballots": r["exhausted"],
            "majority_needed": r["threshold"],
            "tallies": {names.get(int(k), k): v for k, v in r["tallies"].items()},
            "eliminated": [names.get(c, c) for c in r["eliminated"]],
            "reason": r["reason"]})
    return out
