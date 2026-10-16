import pytest

from app.voting import (
    BallotError, break_tie, can_batch_eliminate, clean_ballot, count, describe,
    lowest_candidates, majority_threshold, plurality_winner, tally,
)

# Poll 1 from migrations/002_seed.sql: films 1=Dune, 2=Spirited Away, 3=Paddington, 4=Matrix
POLL1_FILMS = [1, 2, 3, 4]
POLL1_BALLOTS = [[1, 4, 2], [1, 3], [1], [1, 2], [2, 3], [2, 1], [2, 4],
                 [3, 2, 1], [3, 2], [4, 2, 1], [4]]


# ---- majority_threshold ----
@pytest.mark.parametrize("active,expected", [(0, 0), (1, 1), (2, 2), (10, 6), (11, 6)])
def test_majority_threshold(active, expected):
    assert majority_threshold(active) == expected


# ---- clean_ballot ----
def test_clean_ballot_keeps_first_position_of_duplicates():
    assert clean_ballot([1, 2, 1], [1, 2, 3]) == (1, 2)


def test_clean_ballot_strict_rejects_unknown_film():
    with pytest.raises(BallotError):
        clean_ballot([1, 99], [1, 2])


def test_clean_ballot_lenient_drops_unknown_film():
    assert clean_ballot([99, 1, 98, 2], [1, 2], strict=False) == (1, 2)


@pytest.mark.parametrize("ranking", [None, []])
def test_clean_ballot_empty(ranking):
    assert clean_ballot(ranking, [1, 2]) == ()


# ---- tally ----
def test_tally_counts_first_surviving_choice():
    counts, exhausted = tally([(1, 2), (2, 1), (1,)], [1, 2])
    assert counts == {1: 2, 2: 1} and exhausted == 0


def test_tally_transfers_after_elimination_and_counts_exhausted():
    counts, exhausted = tally([(1, 2), (1,), (2,)], [1, 2], eliminated=[1])
    assert counts == {2: 2} and exhausted == 1


# ---- lowest_candidates ----
def test_lowest_candidates_returns_all_tied_sorted():
    assert lowest_candidates({1: 5, 3: 2, 2: 2}) == [2, 3]


def test_lowest_candidates_empty():
    assert lowest_candidates({}) == []


# ---- break_tie ----
def test_break_tie_single_film():
    assert break_tie([7], [])[0] == 7


def test_break_tie_no_films_raises():
    with pytest.raises(BallotError):
        break_tie([], [])


def test_break_tie_countback_eliminates_earlier_weaker():
    history = [{1: 5, 2: 1, 3: 2}]  # film 2 was weaker than 3 last round
    film, why = break_tie([2, 3], history)
    assert film == 2 and "countback" in why


def test_break_tie_countback_goes_back_several_rounds():
    history = [{2: 1, 3: 4}, {2: 3, 3: 3}]  # level last round, 2 weaker before that
    film, why = break_tie([2, 3], history)
    assert film == 2 and "2 round" in why


def test_break_tie_falls_back_to_stable_order_when_always_level():
    film, why = break_tie([4, 3], [{3: 2, 4: 2}])
    assert film == 3 and "stable order" in why


def test_break_tie_is_independent_of_input_order():
    assert break_tie([3, 4], []) == break_tie([4, 3], [])


# ---- can_batch_eliminate ----
def test_can_batch_when_combined_cannot_reach_next():
    assert can_batch_eliminate({1: 5, 2: 1, 3: 1}, [2, 3]) is True


def test_cannot_batch_when_combined_reaches_next():
    assert can_batch_eliminate({1: 3, 2: 2, 3: 2}, [2, 3]) is False


def test_cannot_batch_when_everyone_is_tied():
    assert can_batch_eliminate({1: 2, 2: 2}, [1, 2]) is False


# ---- plurality_winner ----
def test_plurality_winner_clear():
    assert plurality_winner([(1,), (1,), (2,)], [1, 2]) == 1


def test_plurality_winner_none_with_no_candidates():
    assert plurality_winner([], []) is None


def test_plurality_winner_none_on_tie():
    assert plurality_winner([(1,), (2,)], [1, 2]) is None


# ---- count: the seeded poll ----
def test_poll1_plurality_and_irv_disagree():
    result = count(POLL1_BALLOTS, POLL1_FILMS)
    assert result["plurality_winner"] == 1      # Dune leads first preferences 4-3-2-2
    assert result["winner"] == 2                # Spirited Away wins the runoff
    assert result["status"] == "majority"


def test_poll1_round_one_has_two_way_tie_at_bottom():
    r1 = count(POLL1_BALLOTS, POLL1_FILMS)["rounds"][0]
    assert r1["tallies"] == {1: 4, 2: 3, 3: 2, 4: 2}
    assert lowest_candidates(r1["tallies"]) == [3, 4]
    assert r1["eliminated"] == [3]              # stable order: no history to count back on


def test_poll1_threshold_falls_as_ballots_exhaust():
    rounds = count(POLL1_BALLOTS, POLL1_FILMS)["rounds"]
    assert [r["threshold"] for r in rounds] == [6, 6, 6]
    assert rounds[-1]["active"] == 10 and rounds[-1]["exhausted"] == 1
    assert rounds[-1]["tallies"] == {1: 4, 2: 6}


# ---- count: edge cases ----
def test_first_round_majority():
    result = count([[1], [1], [2]], [1, 2])
    assert result["winner"] == 1 and len(result["rounds"]) == 1


def test_single_film_wins():
    result = count([[1]], [1])
    assert result["winner"] == 1 and result["status"] == "majority"


def test_no_films_raises():
    with pytest.raises(BallotError):
        count([], [])


def test_zero_ballots_is_exhausted_with_no_winner():
    result = count([], [1, 2])
    assert result["status"] == "exhausted" and result["winner"] is None


def test_all_tied_is_a_tie_not_a_coin_toss():
    result = count([[1], [2], [3]], [1, 2, 3])
    assert result["status"] == "tie" and result["winner"] is None
    assert result["rounds"][0]["eliminated"] == []


def test_poll2_seeded_deadlock_is_a_tie():
    result = count([[5], [6], [7]], [5, 6, 7])
    assert result["status"] == "tie"


def test_all_ballots_empty_is_exhausted():
    result = count([[], []], [1, 2])
    assert result["status"] == "exhausted" and result["winner"] is None
    assert result["rounds"][0]["exhausted"] == 2


def test_ballot_with_repeated_film_counts_once():
    result = count([[1, 1, 1], [2], [2]], [1, 2])
    assert result["winner"] == 2
    assert result["rounds"][0]["tallies"] == {1: 1, 2: 2}


def test_ballot_naming_unknown_film_is_ignored():
    result = count([[99, 1], [1], [2]], [1, 2])
    assert result["winner"] == 1


def test_countback_resolves_a_later_tie():
    # round 1: 1:5, 2:3, 3:2, 4:1 -> 4 goes, its ballot moves to 3 -> round 2: 2 and 3 tied on 3.
    # Round 1 had 3 weaker than 2, so countback removes 3 (stable order alone would remove 2).
    ballots = [[1]] * 5 + [[2]] * 3 + [[3]] * 2 + [[4, 3]]
    rounds = count(ballots, [1, 2, 3, 4])["rounds"]
    assert rounds[1]["tallies"] == {1: 5, 2: 3, 3: 3}
    assert rounds[1]["eliminated"] == [3]
    assert "countback" in rounds[1]["reason"]


BATCH_BALLOTS = [[1]] * 4 + [[2]] * 3 + [[3, 2]] + [[4, 2]]


def test_batch_elimination_removes_several_films_in_one_round():
    result = count(BATCH_BALLOTS, [1, 2, 3, 4], batch=True)
    assert result["rounds"][0]["eliminated"] == [3, 4]


def test_batch_and_single_elimination_agree_on_the_winner():
    batched = count(BATCH_BALLOTS, [1, 2, 3, 4], batch=True)
    single = count(BATCH_BALLOTS, [1, 2, 3, 4], batch=False)
    assert batched["winner"] == single["winner"] == 2


def test_count_is_deterministic():
    a = count(POLL1_BALLOTS, POLL1_FILMS)
    b = count(POLL1_BALLOTS, POLL1_FILMS)
    assert repr(a) == repr(b)


def test_count_does_not_mutate_its_input():
    ballots = [list(b) for b in POLL1_BALLOTS]
    count(ballots, POLL1_FILMS)
    assert ballots == [list(b) for b in POLL1_BALLOTS]


# ---- describe ----
def test_describe_names_winner_and_every_round():
    result = count(POLL1_BALLOTS, POLL1_FILMS)
    names = {1: "Dune", 2: "Spirited Away", 3: "Paddington", 4: "Matrix"}
    lines = describe(result, names)
    assert len(lines) == len(result["rounds"]) + 1
    assert lines[-1] == "Winner: Spirited Away (majority)"


def test_describe_reports_no_winner():
    assert describe(count([[1], [2]], [1, 2]))[-1] == "No winner: tie"


# ---- known quirk, kept on purpose (defended in the README) ----
# The stable-order fallback sorts film IDs as TEXT, so "10" comes before "9". It is still
# deterministic (same ballots, same answer), but it is not numeric order. These tests pin that
# behaviour so a change to it is a conscious decision, not an accident.
def test_stable_order_compares_ids_as_text_not_numbers():
    film, why = break_tie([9, 10], [])
    assert film == 10 and "stable order" in why


def test_lowest_candidates_orders_ids_as_text():
    assert lowest_candidates({9: 1, 10: 1, 2: 5}) == [10, 9]


def test_level_tie_between_ids_9_and_10_eliminates_10_first():
    ballots = [[2]] * 2 + [[3]] * 2 + [[9]] + [[10]]
    round_one = count(ballots, [2, 3, 9, 10])["rounds"][0]
    assert round_one["eliminated"] == [10]
    assert "stable order" in round_one["reason"]
