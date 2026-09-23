# OpenDomino backend

Requires Python 3.12+.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
cp .env.example .env      # Windows PowerShell: Copy-Item .env.example .env
# Fill LLM_API_KEY, DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET in .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Health endpoint: `http://localhost:8000/health`.
