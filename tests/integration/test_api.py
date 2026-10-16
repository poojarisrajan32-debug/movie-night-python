"""Every endpoint, end to end, against real Postgres and Redis (no mocks).

Each test makes its own poll, so nothing here depends on - or disturbs - the seed data.
"""
UNKNOWN = 999_999_999


def add_film(client, pid, title, **extra):
    r = client.post(f"/polls/{pid}/films", json={"title": title, **extra})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def vote(client, pid, voter, ranking):
    r = client.post(f"/polls/{pid}/ballots", json={"voter": voter, "ranking": ranking})
    assert r.status_code == 201, r.text
    return r.json()


def firsts(client, pid):
    films = client.get(f"/polls/{pid}").json()["films"]
    return {f["title"]: f["first_preferences"] for f in films}


def closed_poll(client, make_poll):
    """A closed poll whose answer is known: C is eliminated, its voter's 2nd choice
    (B) is added, and B reaches 3 of 5. Plurality alone would have said A or B."""
    pid = make_poll()["id"]
    a, b, c = (add_film(client, pid, t) for t in ("A", "B", "C"))
    for voter, ranking in (("v1", [a]), ("v2", [a]), ("v3", [b]), ("v4", [b]), ("v5", [c, b])):
        vote(client, pid, voter, ranking)
    assert client.post(f"/polls/{pid}/close").status_code == 200
    return pid


# ---------------------------------------------------------------- health

def test_health_reports_both_services_up(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["postgres"] is True and r.json()["redis"] is True


# ---------------------------------------------------------------- create / read

def test_open_poll_returns_the_new_poll(poll):
    assert poll["title"] == "Friday film night"
    assert poll["opened_by"] == "tester"
    assert poll["status"] == "open"
    assert isinstance(poll["id"], int)


def test_open_poll_strips_whitespace(make_poll):
    assert make_poll(title="  padded  ", opened_by="  me ")["title"] == "padded"


def test_open_poll_requires_title_and_opener(client):
    assert client.post("/polls", json={"opened_by": "x"}).status_code == 400
    assert client.post("/polls", json={"title": "   ", "opened_by": "x"}).status_code == 400
    assert client.post("/polls", json={"title": "x"}).status_code == 400


def test_show_poll_reads_back_what_was_created(client, poll):
    r = client.get(f"/polls/{poll['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == poll["title"]
    assert body["films"] == [] and body["ballots_cast"] == 0
    assert body["status"] == "open" and body["result"] is None


def test_show_poll_unknown_is_404(client):
    assert client.get(f"/polls/{UNKNOWN}").status_code == 404


def test_show_poll_non_numeric_id_is_422(client):
    assert client.get("/polls/abc").status_code == 422


def test_list_polls_includes_counts(client, poll):
    pid = poll["id"]
    a = add_film(client, pid, "A")
    vote(client, pid, "v1", [a])
    row = next(p for p in client.get("/polls").json()["polls"] if p["id"] == pid)
    assert row["films"] == 1 and row["ballots"] == 1


# ---------------------------------------------------------------- update (PATCH)

def test_rename_persists_and_strips(client, poll):
    pid = poll["id"]
    r = client.patch(f"/polls/{pid}", json={"title": "  Saturday night  "})
    assert r.status_code == 200 and r.json()["title"] == "Saturday night"
    assert client.get(f"/polls/{pid}").json()["title"] == "Saturday night"


def test_rename_rejects_blank_or_missing_title(client, poll):
    pid = poll["id"]
    assert client.patch(f"/polls/{pid}", json={"title": "   "}).status_code == 400
    assert client.patch(f"/polls/{pid}", json={}).status_code == 400
    assert client.get(f"/polls/{pid}").json()["title"] == poll["title"]


def test_rename_unknown_poll_is_404(client):
    assert client.patch(f"/polls/{UNKNOWN}", json={"title": "x"}).status_code == 404


def test_rename_closed_poll_is_409_and_title_unchanged(client, make_poll):
    pid = closed_poll(client, make_poll)
    before = client.get(f"/polls/{pid}").json()["title"]
    assert client.patch(f"/polls/{pid}", json={"title": "nope"}).status_code == 409
    assert client.get(f"/polls/{pid}").json()["title"] == before


# ---------------------------------------------------------------- films

def test_add_film_defaults_added_by(client, poll):
    r = client.post(f"/polls/{poll['id']}/films", json={"title": "Dune"})
    assert r.status_code == 201
    assert r.json()["added_by"] == "anonymous" and r.json()["poll_id"] == poll["id"]


def test_add_film_keeps_given_added_by(client, poll):
    r = client.post(f"/polls/{poll['id']}/films", json={"title": "Dune", "added_by": "asha"})
    assert r.json()["added_by"] == "asha"


def test_add_film_duplicate_is_409_ignoring_case(client, poll):
    add_film(client, poll["id"], "Spirited Away")
    r = client.post(f"/polls/{poll['id']}/films", json={"title": "  spirited AWAY "})
    assert r.status_code == 409


def test_add_film_blank_title_is_400(client, poll):
    assert client.post(f"/polls/{poll['id']}/films", json={"title": " "}).status_code == 400


def test_add_film_unknown_poll_is_404(client):
    assert client.post(f"/polls/{UNKNOWN}/films", json={"title": "x"}).status_code == 404


def test_add_film_to_closed_poll_is_409(client, make_poll):
    pid = closed_poll(client, make_poll)
    assert client.post(f"/polls/{pid}/films", json={"title": "late"}).status_code == 409


# ---------------------------------------------------------------- ballots

def test_cast_ballot_counts_first_preference(client, poll):
    pid = poll["id"]
    a, b = add_film(client, pid, "A"), add_film(client, pid, "B")
    body = vote(client, pid, "v1", [b, a])
    assert body["ranking"] == [b, a] and body["voter"] == "v1"
    assert firsts(client, pid) == {"A": 0, "B": 1}
    assert client.get(f"/polls/{pid}").json()["ballots_cast"] == 1


def test_cast_ballot_duplicate_in_ranking_keeps_first_position(client, poll):
    pid = poll["id"]
    a, b = add_film(client, pid, "A"), add_film(client, pid, "B")
    assert vote(client, pid, "v1", [a, b, a])["ranking"] == [a, b]


def test_same_voter_cannot_vote_twice(client, poll):
    pid = poll["id"]
    a = add_film(client, pid, "A")
    vote(client, pid, "v1", [a])
    r = client.post(f"/polls/{pid}/ballots", json={"voter": "v1", "ranking": [a]})
    assert r.status_code == 409
    assert client.get(f"/polls/{pid}").json()["ballots_cast"] == 1


def test_cast_ballot_validation(client, poll):
    pid = poll["id"]
    a = add_film(client, pid, "A")
    url = f"/polls/{pid}/ballots"
    assert client.post(url, json={"ranking": [a]}).status_code == 400            # no voter
    assert client.post(url, json={"voter": "v", "ranking": []}).status_code == 400
    assert client.post(url, json={"voter": "v", "ranking": [UNKNOWN]}).status_code == 400
    assert client.post(url, json={"voter": "v", "ranking": ["abc"]}).status_code == 400
    assert client.get(f"/polls/{pid}").json()["ballots_cast"] == 0


def test_cast_ballot_needs_films_and_an_open_poll(client, poll, make_poll):
    r = client.post(f"/polls/{poll['id']}/ballots", json={"voter": "v", "ranking": [1]})
    assert r.status_code == 409                                                  # no films yet
    pid = closed_poll(client, make_poll)
    a = client.get(f"/polls/{pid}").json()["films"][0]["id"]
    r = client.post(f"/polls/{pid}/ballots", json={"voter": "late", "ranking": [a]})
    assert r.status_code == 409                                                  # closed
    assert client.post(f"/polls/{UNKNOWN}/ballots",
                       json={"voter": "v", "ranking": [1]}).status_code == 404


# ---------------------------------------------------------------- the Redis cache

def test_cold_cache_does_not_zero_the_other_films(client, poll, redis):
    """add_film warms the hash first; otherwise one hsetnx hides every other film."""
    pid = poll["id"]
    a = add_film(client, pid, "A")
    vote(client, pid, "v1", [a])
    redis.delete(f"poll:{pid}:first")             # simulate a cache restart / expiry
    add_film(client, pid, "B")
    assert firsts(client, pid) == {"A": 1, "B": 0}


def test_cold_cache_does_not_double_count_a_ballot(client, poll, redis):
    """cast() warms before inserting; warming after would count the new ballot twice."""
    pid = poll["id"]
    a = add_film(client, pid, "A")
    vote(client, pid, "v1", [a])
    redis.delete(f"poll:{pid}:first")
    vote(client, pid, "v2", [a])
    assert firsts(client, pid) == {"A": 2}


# ---------------------------------------------------------------- close / results

def test_close_runs_the_count_and_returns_the_winner(client, make_poll):
    pid = make_poll()["id"]
    a, b, c = (add_film(client, pid, t) for t in ("A", "B", "C"))
    for voter, ranking in (("v1", [a]), ("v2", [a]), ("v3", [b]), ("v4", [b]), ("v5", [c, b])):
        vote(client, pid, voter, ranking)
    r = client.post(f"/polls/{pid}/close")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "majority" and body["winner"] == "B"
    assert [x["eliminated"] for x in body["rounds"]] == [["C"], []]
    assert body["rounds"][-1]["tallies"] == {"A": 2, "B": 3}
    assert client.get(f"/polls/{pid}").json()["status"] == "closed"


def test_results_after_close_match_the_close_response(client, make_poll):
    pid = closed_poll(client, make_poll)
    r = client.get(f"/polls/{pid}/results")
    assert r.status_code == 200
    assert r.json()["winner"] == "B" and r.json()["winner_id"] is not None


def test_results_while_open_is_409_and_unknown_is_404(client, poll):
    assert client.get(f"/polls/{poll['id']}/results").status_code == 409
    assert client.get(f"/polls/{UNKNOWN}/results").status_code == 404


def test_closing_twice_is_409(client, make_poll):
    pid = closed_poll(client, make_poll)
    assert client.post(f"/polls/{pid}/close").status_code == 409


def test_close_with_no_films_is_409_and_leaves_the_poll_open(client, poll):
    pid = poll["id"]
    assert client.post(f"/polls/{pid}/close").status_code == 409
    assert client.get(f"/polls/{pid}").json()["status"] == "open"


def test_close_while_a_count_is_running_is_409(client, poll, redis):
    pid = poll["id"]
    add_film(client, pid, "A")
    redis.set(f"lock:close:{pid}", "counting", ex=30)   # someone else holds the lock
    assert client.post(f"/polls/{pid}/close").status_code == 409
    assert client.get(f"/polls/{pid}").json()["status"] == "open"


def test_close_unknown_poll_is_404(client):
    assert client.post(f"/polls/{UNKNOWN}/close").status_code == 404


def test_close_releases_its_lock(client, make_poll, redis):
    pid = closed_poll(client, make_poll)
    assert redis.exists(f"lock:close:{pid}") == 0


# ---------------------------------------------------------------- delete

def test_delete_removes_the_poll_everything_under_it_and_its_cache_key(
        client, poll, sql, redis):
    pid = poll["id"]
    a, b = add_film(client, pid, "A"), add_film(client, pid, "B")
    vote(client, pid, "v1", [a, b])
    vote(client, pid, "v2", [b])
    key = f"poll:{pid}:first"
    assert redis.exists(key) == 1
    assert sql("SELECT count(*) FROM films WHERE poll_id=%s", (pid,))[0][0] == 2

    r = client.delete(f"/polls/{pid}")
    assert r.status_code == 204 and r.content == b""

    assert client.get(f"/polls/{pid}").status_code == 404
    assert sql("SELECT count(*) FROM polls WHERE id=%s", (pid,))[0][0] == 0
    assert sql("SELECT count(*) FROM films WHERE poll_id=%s", (pid,))[0][0] == 0
    assert sql("SELECT count(*) FROM ballots WHERE poll_id=%s", (pid,))[0][0] == 0
    assert sql("SELECT count(*) FROM ballot_choices WHERE film_id IN (%s, %s)", (a, b))[0][0] == 0
    assert redis.exists(key) == 0
    assert redis.exists(f"lock:close:{pid}") == 0


def test_delete_twice_and_unknown_are_404(client, poll):
    assert client.delete(f"/polls/{poll['id']}").status_code == 204
    assert client.delete(f"/polls/{poll['id']}").status_code == 404
    assert client.delete(f"/polls/{UNKNOWN}").status_code == 404


def test_delete_while_a_count_is_running_is_409_and_nothing_is_lost(client, poll, redis, sql):
    pid = poll["id"]
    add_film(client, pid, "A")
    redis.set(f"lock:close:{pid}", "counting", ex=30)
    assert client.delete(f"/polls/{pid}").status_code == 409
    assert redis.exists(f"lock:close:{pid}") == 1         # we didn't steal or free their lock
    assert sql("SELECT count(*) FROM films WHERE poll_id=%s", (pid,))[0][0] == 1


def test_delete_works_on_a_closed_poll(client, make_poll, sql):
    pid = closed_poll(client, make_poll)
    assert client.delete(f"/polls/{pid}").status_code == 204
    assert sql("SELECT count(*) FROM ballots WHERE poll_id=%s", (pid,))[0][0] == 0
