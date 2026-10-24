# A2A Invoice Action Agent (GA5)

Public HTTPS agent that:

1. Publishes an A2A 1.0 Agent Card
2. Reads an invoice claim batch
3. Proposes one typed action per package with evidence
4. Executes only `ACCEPTED` grader results
5. Stores Tasks so retries cannot repeat payments

## Run locally

```bash
cd a2a-invoice-agent
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

- Agent Card: `http://localhost:8080/.well-known/agent-card.json`
- A2A base URL: `http://localhost:8080/a2a/`

Any `Authorization: Bearer <token>` value is a separate user. There is no shared password. Missing auth is 401.

Optional LLM (not graded; used to read long packages):

```
LLM_API_KEY=...
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.1-8b-instant
```

Without a key, the agent still decides from the live paragraph heuristics and caches by canonical package content.

## Tests

```bash
pytest -q
```

## Deploy (public HTTPS)

Set `PUBLIC_BASE_URL` to the exact URL you will paste into the grader, including the trailing slash, for example:

`https://YOUR-SERVICE.onrender.com/a2a/`

Submit that same value as the A2A interface base URL. Do not put credentials, query strings, or fragments in it. Do not put the Agent Card path in it.

Keep one Check in reserve for Save.
