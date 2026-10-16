# Movie Night Decider

**Language:** Python (FastAPI) &nbsp;|&nbsp; **Needs:** Postgres + Redis

The application works, and it is now built, tested and shipped end to end: a container
image, a one-command Compose stack, unit and integration tests, a CircleCI pipeline that
publishes images to Docker Hub, and three Kubernetes environments that run those exact
images. This README keeps the original description of the app and adds how each piece
was done and why.

---

## You do not need Python installed

The app is built into a container, and the container brings its own Python 3.12.
To run it you only need Docker.

---

## 1. What this app needs

| | |
|---|---|
| **Runtime** | Python 3.12 |
| **Install dependencies** | `pip install -r requirements.txt` |
| **Start the app** | `uvicorn app.main:app --host 0.0.0.0 --port 8080` |
| **Listens on** | port 8080, bound to `0.0.0.0` |
| **Environment variables** | `DATABASE_URL`, `REDIS_URL` |
| **Needs running first** | Postgres, Redis, and the migrations applied |

### What it does

Someone opens a poll, everyone adds films and ranks them. When the poll closes the app runs a real instant-runoff count instead of just picking whatever had the most first-place votes, and shows you every elimination round - who went out, where their votes went, and what the majority bar was at the time.

### Endpoints

```
GET    /health
GET    /polls
POST   /polls                   {"title":"Friday night","opened_by":"asha"}
GET    /polls/{id}              films, live first-preference counts, ballots cast
PATCH  /polls/{id}              {"title":"Saturday night"}   rename an open poll
DELETE /polls/{id}              remove a poll and everything under it (204)
POST   /polls/{id}/films        {"title":"Dune: Part Two","added_by":"rahul"}
POST   /polls/{id}/ballots      {"voter":"asha","ranking":[3,1,2]}
POST   /polls/{id}/close        runs the count, exactly once
GET    /polls/{id}/results      the stored round-by-round history
```

`/health` reports Postgres and Redis **separately**. If it says
`postgres: false` the app started fine and your compose wiring is wrong -
do not go looking in the application code.

**The two added endpoints** complete the create / read / update / delete cycle:

- `PATCH` rejects a blank title (400), an unknown poll (404) and a closed poll (409). The
  update only matches rows whose status is still `open`, so a rename that races a close loses
  cleanly instead of editing a finished poll.
- `DELETE` removes the poll, and Postgres cascades the delete to its films, ballots and
  choices. Redis has no cascade, so the handler also drops the poll's live-count key. It takes
  the same lock that closing uses, so a poll cannot be deleted in the middle of being counted
  (409), and a close that starts during a delete backs off.

### Migrations

`migrations/` holds `.sql` files applied **in filename order** before the app
starts. They create the tables and insert sample data. A container running
`psql` over them in order is enough; you do not need a migration tool.

The files are written to be safe to run repeatedly (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`),
which both environments rely on:

- **Compose:** a one-shot `migrate` service runs them and exits; the app waits for it.
- **Kubernetes:** an init container on the app Pod runs them from a ConfigMap built out of
  `migrations/`. See the Kubernetes section.

---

## 2. What was built

| File | How it meets the requirement |
|---|---|
| `Dockerfile` | Pinned base image (`python:3.12.4-slim`); `requirements.txt` is copied and installed **before** the source so the slow layer stays cached; runs as a non-root user (uid 999); only `app/` is copied; start command in list form on `0.0.0.0:8080`. `.dockerignore` keeps `.git`, `.env`, tests and migrations out of the image. |
| `docker-compose.yml` | App + Postgres 16 + Redis 7 + a one-shot `migrate` service. Healthchecks on both databases, and the app waits with `service_healthy` / `service_completed_successfully`. One `docker compose up`. |
| `.circleci/config.yml` | secret scan -> lint -> unit tests -> integration tests -> image build and push, each stage gated on the one before. See Pipeline below. |
| `tests/unit/` | 48 tests for `app/voting.py`. No database, no network. |
| `tests/integration/` | 37 tests against a real Postgres and Redis, covering every endpoint. |
| `k8s/qa`, `k8s/stage`, `k8s/prod` | The same manifests per environment, differing only in namespace and image tag. |
| `scripts/crud-demo.sh` | Create, read, update and delete over the API, with the rows shown in Postgres in between. |

Images are published to Docker Hub as `dapirs2006/movie-night-python` by the pipeline, never by
hand: `:qa`, `:stage` and `:prod` (the moving tags Kubernetes follows) plus an immutable
`<branch>-<short-sha>` tag for every build.

### When it works

```bash
docker compose up --build
curl localhost:8080/health
```

```json
{"status":"ok","postgres":true,"redis":true}
```

Then the full create / read / update / delete walk-through, ending with the rows gone from
Postgres:

```bash
bash scripts/crud-demo.sh
```

`docker compose down -v` stops everything and deletes the data volume.

---

## Tests

| | Command | What it covers |
|---|---|---|
| Unit | `python -m pytest tests/unit -v --cov=app.voting --cov-report=term-missing --cov-fail-under=70` | `app/voting.py` only: pure functions, no database, no network |
| Integration | `python -m pytest tests/integration -v` | every endpoint, through FastAPI's in-process `TestClient`, against a real Postgres and a real Redis (no mocks) |

Install the test tools with `python -m pip install -r requirements-dev.txt`. They are kept out of
`requirements.txt` so the production image does not ship them.

Integration tests need `DATABASE_URL` and `REDIS_URL` (they fail loudly if either is missing,
because a skipped test looks green while testing nothing). Locally:

```bash
docker run -d --rm --name pg-test -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app -e POSTGRES_DB=app -p 5432:5432 postgres:16-alpine
docker run -d --rm --name redis-test -p 6379:6379 redis:7-alpine
export DATABASE_URL=postgresql://app:app@localhost:5432/app REDIS_URL=redis://localhost:6379
python -m pytest tests/integration -v
```

- **Unit tests** pin the seeded poll 1 (the instant-runoff winner, Spirited Away, differs from
  the plurality winner, Dune; round one has a two-way tie at the bottom), the majority maths, the
  tie-break, and the ugly cases: everything tied, every ballot exhausted, one film, no films, a
  ballot that repeats a film, a ballot that names a film not in the poll, and that the same
  ballots always give byte-identical rounds.
- **Coverage** of `app/voting.py` is 96% and the pipeline fails below 70%. It is measured on
  `app.voting` rather than all of `app/`, because `main.py`, `db.py` and `cache.py` need a
  database and are covered by the integration tests instead. The four uncovered lines are a
  branch that cannot be reached: a single surviving film always already holds a majority.
- **Integration tests** create their own poll per test and delete it afterwards, so they never
  disturb the seed data. They check the database and Redis directly (for example that a delete
  removes the rows and the cache key) and include two cold-cache regressions.

---

## Why Redis is here

Two different jobs. A hash of live first-preference counts, so the room can watch the poll fill up without re-aggregating the ballots table on every refresh - it is warmed from Postgres if the cache is cold. And a `SET NX EX` lock around closing, so two people hitting Close at the same moment cannot both run the count. Note that the lock alone is not enough: it expires. The conditional `UPDATE ... WHERE status='open'` is what actually makes it exactly once.

The same lock now also guards `DELETE`, and deleting a poll drops its live-count hash, because
Postgres cascades but Redis does not.

---

## The hard part

**Ties at the bottom. Two films on two votes each - which goes out?**

### The rule

1. **Countback.** Look at the previous round, then the one before it, and so on. Among the tied
   films, the one that had fewer votes in the most recent earlier round where they differed goes
   out.
2. **Stable order.** If they were level in every earlier round (which includes a tie in round
   one, where there is no history yet), a fixed order decides.
3. **Never random.** The same ballots always give the same answer.
4. **If every surviving film is tied, nobody is eliminated.** The result is status `tie` with no
   winner, rather than a coin toss that quietly picks one.

### Why this rule

- A tie *in this round* says nothing about which film is less wanted, but earlier rounds do. A
  film that was behind earlier has less support, so countback uses information that is really in
  the ballots instead of an arbitrary choice.
- It is deterministic, so it is auditable. Every round is stored, anyone re-running the count
  gets the same answer, and "Paddington went out in round one" can be checked. A random pick
  would make the winner unrepeatable and impossible to defend.
- The fixed order is only the last resort and is openly arbitrary; it exists so that the count
  can always finish.
- Declaring a tie is more honest than eliminating one of several perfectly level films, which
  would be choosing the winner by coin toss.

### Worked examples

**Seeded poll 1 (11 ballots).** Round one: Dune 4, Spirited Away 3, Paddington 2, The Matrix 2.
Nobody reaches the bar of 6. Paddington and The Matrix are tied at the bottom with no earlier
round to look at, so the fixed order sends Paddington out; both of its ballots rank Spirited
Away next, giving Spirited Away 5. The Matrix goes next, one of its ballots moves to Spirited
Away and the other is exhausted, and Spirited Away wins with 6 of the 10 live ballots. Plain
first-place counting would have chosen Dune.

**A tie that countback resolves.** Eleven ballots: five rank film 1 only, three rank film 2 only,
two rank film 3 only, and one ranks film 4 then film 3. Round one is 5 / 3 / 2 / 1, so film 4
goes out and its ballot moves to film 3, making round two 5 / 3 / 3. Films 2 and 3 are now tied,
but in round one film 3 had 2 votes against film 2's 3, so **film 3** goes out. The fixed order
alone would have removed film 2. Film 1 then wins with 5 of the 8 live ballots.

### The majority bar

"More than half" is recomputed every round over the ballots still live. A ballot whose every
choice has been eliminated is exhausted and counts for nobody; if the bar stayed pinned to the
number of ballots cast, a round could end with nobody able to reach it.

### A known quirk, kept on purpose

The fixed order compares film ids **as text**, so id `10` sorts before id `9`. It is still
deterministic, and it only applies after countback has failed. I chose to keep it and pin it
with three tests rather than quietly change application behaviour; switching to numeric order
would be a deliberate change that updates those tests.

### An alternative that is available but not the default

`count(..., batch=True)` removes several films in one round when the tied-lowest films, even
combined, still have fewer votes than the next film up, because none of them could ever overtake
it. It is a legitimate answer to the tie question and gives the same winner on both examples above,
but one-at-a-time is easier to explain and audit, so it is not the default.

---

## Pipeline (CircleCI)

| Branch | What runs |
|---|---|
| `main` | secret scan -> lint -> unit tests -> integration tests -> build and push `:qa` |
| `staging` | build and push `:stage` (no tests: the same code already passed on `main`) |
| `prod` | build and push `:prod` |
| any other branch | the four checks only, nothing is pushed |

- Each stage `requires` the previous one, so a failure stops everything after it and a broken
  commit never becomes an image.
- The **secret scan** (gitleaks, version pinned and checksum-verified) reads the whole git
  history, not just the current files: a secret that was committed and later deleted is still
  readable from old commits.
- The integration job starts Postgres and Redis as **service containers**. They share the job's
  network, so the host is `localhost`, the opposite of Compose where it is the service name.
- Images are built for `linux/amd64` and `linux/arm64`, because CircleCI is amd64 and the
  machines the cluster runs on may be arm64. One tag then runs natively on both.
- Every build pushes a moving tag (`qa`, `stage`, `prod`) and an immutable
  `<branch>-<short-sha>` that ties a running image to a commit.
- `DOCKERHUB_USER` and `DOCKERHUB_TOKEN` are CircleCI project environment variables. Nothing
  secret is in the repository.

---

## Branches and promotion

`main` is QA, `staging` is pre-production and `prod` is production. A ruleset on all three
blocks direct pushes, force pushes and deletion, with an empty bypass list. Work reaches `main`
by pull request, and promotion is `main` -> `staging` -> `prod`, always by pull request and
always with **Create a merge commit**. Squashing would rewrite the commits into a new commit
with a different id and leave the next promotion reporting conflicts.

Right after a promotion, `git diff origin/main origin/staging` and
`git diff origin/staging origin/prod` both print nothing.

---

## Kubernetes

One namespace per environment (`qa`, `stage`, `prod`), with manifests in `k8s/<namespace>/`.
Each namespace has Postgres, Redis and the app (two replicas), each behind a Service, plus a
`dockerhub` pull secret that the app Pods reference through `imagePullSecrets`. The app pulls
`dapirs2006/movie-night-python:<qa|stage|prod>` with `imagePullPolicy: Always`.

```bash
minikube start
kubectl create namespace qa            # likewise stage and prod
kubectl create secret docker-registry dockerhub \
  --docker-username=<user> --docker-password=<access-token> -n qa
kubectl apply -f k8s/qa/
kubectl get pods,endpoints -n qa
kubectl port-forward -n qa svc/movie-night 8090:8080     # then: curl localhost:8090/health
```

The pull secret holds an access token, so it is created by hand and never committed. Secrets
belong to one namespace, so it is created once per namespace.

Choices worth knowing:

- **Migrations** run in an init container on the app Pod, from a ConfigMap built from
  `migrations/` (`kubectl create configmap migrations --from-file=migrations/ -n qa --dry-run=client -o yaml`).
  It waits for Postgres, then applies the files inside a single `psql` session holding an
  advisory lock. Without the lock, two Pods starting together on an empty database collided on
  `CREATE TABLE`; in a local test 6 of 12 starts failed without it and 0 of 20 with it.
- **Readiness probe only**, on `/health`. If Postgres is down, restarting the app would not help,
  but taking it out of the Service is exactly right, and a new Pod gets no traffic until it
  is ready.
- **No fixed `nodePort`**, so the three namespaces cannot collide.
- **Postgres uses `emptyDir`**: data survives a container restart but not Pod deletion. The
  migrations rebuild the schema and sample data on the next start, but polls created in the
  meantime are lost. A PersistentVolumeClaim is the production answer.
- The database credentials in the manifests are throwaway values for a cluster that exposes no
  database port. Real credentials would go in a Secret.

### Release loop, and proof of what is running

```bash
# merge a pull request into main -> CircleCI builds and pushes :qa -> then:
kubectl rollout restart deployment/movie-night -n qa
kubectl rollout status deployment/movie-night -n qa
kubectl get pods -n qa -o jsonpath='{.items[*].spec.containers[*].image}'
kubectl describe pod -n qa -l app=movie-night | grep -i "image id"
```

`rollout restart` rebuilds nothing: it replaces the Pods one at a time, and
`imagePullPolicy: Always` makes the new Pods pull whatever the tag points at now. The
`Image ID` digest must equal the `Digest:` printed at the end of that pipeline's `build-push`
log.

---

## Getting unstuck

| Symptom | Almost always |
|---|---|
| `/health` says `postgres: false` | Wrong hostname. In compose the host is the **service name**, not `localhost`. |
| Page will not load, logs fine | No `ports:` mapping, or bound to `127.0.0.1` not `0.0.0.0`. |
| `relation "..." does not exist` | Migrations did not run, or the app started before they finished. |
| Build takes minutes each time | `COPY . .` is above your dependency install. |
| CI cannot reach the database | In CircleCI service containers the host **is** `localhost` - opposite of compose. |
| `port is already allocated` | A previous stack still holds 8080: `docker compose down`. |
| `Cannot connect to the Docker daemon` | Docker Desktop is not running. |
| Kubernetes Service exists, page does not load | `kubectl get endpoints <svc> -n <ns>`. Empty means the Service selector does not match the Pod labels. |
| Pod is `ImagePullBackOff` | Wrong image or tag, the tag was never pushed, or the `dockerhub` secret is missing in **that namespace**. `kubectl describe pod` shows which. |
| Pod stuck at `Init:0/1` | The migration init container is waiting for Postgres. First start also pulls images, so allow a few minutes. `kubectl logs <pod> -c migrate -n <ns>`. |
| Pod is `Running` but `0/1` | The readiness probe is failing: `/health` is not 200, so check Postgres and Redis. |
| `kubectl port-forward` says `lost connection to pod` | The Pod it was attached to was replaced. Start the port-forward again. |
| Rolled out, but the old version is still live | The pipeline had not finished pushing the tag yet. Compare the Pod's `Image ID` with the pipeline's `Digest:`. |
| CircleCI fails the same way after you fixed the config | You re-ran an old pipeline, which reuses the old commit. Push a new commit. |
| `docker login` complains about a missing username | The CircleCI variables must be named exactly `DOCKERHUB_USER` and `DOCKERHUB_TOKEN`. |
| `git push` to `main` is rejected | The ruleset is working: open a pull request instead. |

---

## Known limitations

- The integration tests call the app in-process rather than over real HTTP to a container; the
  Compose walkthrough and `scripts/crud-demo.sh` cover that path.
- GitHub requires a pull request but does not require the checks to pass before merging; that
  would be a "require status checks" rule on the ruleset.
- Promotion rebuilds the image on each branch instead of promoting the exact image that was
  tested.
- Postgres in Kubernetes is not persistent (see above).