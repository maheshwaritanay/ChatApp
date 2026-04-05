# ChatApp

## Setup

```bash

git clone https://github.com/maheshwaritanay/ChatApp.git
cd ChatApp

uv sync
```

### Running the server

```bash
uv run uvicorn app.main:app --reload
```

The server starts at `http://localhost:8000`.

API docs are available at `http://localhost:8000/docs`.


## Development

### Reset the database

If you change models, delete the database and restart:

```bash
rm app/chat.db
uv run uvicorn app.main:app --reload
```