#!/usr/bin/env bash
# CRUD demo for the capstone: create, read, update, delete over the API, with the rows
# shown in Postgres in between. Needs the stack up (docker compose up) on localhost:8080.
B=${B:-localhost:8080}
J='Content-Type: application/json'
# How to reach psql. Default: the db container from docker-compose.yml.
PSQL=${PSQL:-docker compose exec -T db psql -U app -d app -tA -c}
jid() { python3 -c "import sys,json;print(json.load(sys.stdin)['id'])"; }

echo "== CREATE a poll"
R=$(curl -s -X POST $B/polls -H "$J" -d '{"title":"Demo night","opened_by":"sripad"}'); echo "$R"
PID=$(echo "$R" | jid)
echo "== CREATE two films and one ballot"
F1=$(curl -s -X POST $B/polls/$PID/films -H "$J" -d '{"title":"Alien"}'   | jid)
F2=$(curl -s -X POST $B/polls/$PID/films -H "$J" -d '{"title":"Arrival"}' | jid)
curl -s -w "  [HTTP %{http_code}]\n" -X POST $B/polls/$PID/ballots -H "$J" \
     -d "{\"voter\":\"asha\",\"ranking\":[$F2,$F1]}"
echo "== READ it back"
curl -s $B/polls/$PID; echo
echo "== UPDATE (rename with PATCH)"
curl -s -w "  [HTTP %{http_code}]\n" -X PATCH $B/polls/$PID -H "$J" -d '{"title":"Renamed by PATCH"}'

echo; echo "== PROOF IT IS IN POSTGRES (poll, films, ballots)"
$PSQL "select id, title, status from polls where id=$PID;"
$PSQL "select count(*) as films   from films   where poll_id=$PID;"
$PSQL "select count(*) as ballots from ballots where poll_id=$PID;"
echo; read -r -p "Press Enter to DELETE it... " _

echo "== DELETE"
curl -s -o /dev/null -w "  [HTTP %{http_code}]\n" -X DELETE $B/polls/$PID
echo "== READ after delete (must be 404)"
curl -s -w "  [HTTP %{http_code}]\n" $B/polls/$PID
echo "== POSTGRES AFTER DELETE (all three must be 0)"
$PSQL "select (select count(*) from polls where id=$PID) as polls, (select count(*) from films where poll_id=$PID) as films, (select count(*) from ballots where poll_id=$PID) as ballots;"
