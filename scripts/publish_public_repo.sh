#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env.github if present (gitignored)
# shellcheck source=./_load_env.sh
source "$SCRIPT_DIR/_load_env.sh"
load_env_file "$ROOT_DIR/.env.github"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "ERROR: missing GITHUB_TOKEN." >&2
  echo "Create $ROOT_DIR/.env.github (gitignored) based on .env.github.example." >&2
  exit 2
fi

repo_name="${GITHUB_REPO:-$(basename "$ROOT_DIR")}"
repo_desc="${GITHUB_DESCRIPTION:-CLI tool to manage CLIProxyAPI /v0/management/auth-files}"
repo_public="${GITHUB_PUBLIC:-true}"

owner="${GITHUB_OWNER:-}"
if [[ -z "$owner" ]]; then
  owner="$(
    python3 - <<'PY'
import json
import os
import urllib.request

req = urllib.request.Request(
    "https://api.github.com/user",
    headers={
        "Authorization": f"Bearer {os.environ.get('GITHUB_TOKEN', '')}",
        "Accept": "application/vnd.github+json",
    },
)
with urllib.request.urlopen(req, timeout=20) as resp:
    data = json.loads(resp.read().decode("utf-8", errors="replace"))
print(data.get("login", ""))
PY
  )"
fi

if [[ -z "$owner" ]]; then
  echo "ERROR: cannot resolve GitHub username (owner)." >&2
  echo "Set GITHUB_OWNER in .env.github, or check token permissions." >&2
  exit 2
fi

private_flag="false"
if [[ "${repo_public,,}" != "true" ]]; then
  private_flag="true"
fi

echo "Publishing repo: $owner/$repo_name (private=$private_flag)"

create_payload="$(
  python3 - <<PY
import json

print(
    json.dumps(
        {
            "name": "$repo_name",
            "description": "$repo_desc",
            "private": $private_flag,
            "has_issues": False,
            "has_projects": False,
            "has_wiki": False,
            "auto_init": False,
        }
    )
)
PY
)"

# Create repo (ignore if already exists)
http_code="$(
  curl -sS -o /tmp/cpa_authfiles_create_repo.json -w "%{http_code}" \
    -X POST "https://api.github.com/user/repos" \
    -H "Accept: application/vnd.github+json" \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -d "$create_payload" || true
)"

if [[ "$http_code" == "201" ]]; then
  echo "Repo created."
elif [[ "$http_code" == "422" ]]; then
  echo "Repo already exists (422). Continue..."
else
  echo "ERROR: GitHub API create repo failed (http $http_code)." >&2
  sed -n '1,200p' /tmp/cpa_authfiles_create_repo.json >&2 || true
  exit 1
fi

cd "$ROOT_DIR"

if [[ ! -d ".git" ]]; then
  git init -b main
  git add .
  git commit -m "Initial commit"
fi

git remote remove origin >/dev/null 2>&1 || true
git remote add origin "https://github.com/$owner/$repo_name.git"

# Push without putting token into git config/remote url.
askpass="$(mktemp)"
cat >"$askpass" <<'SH'
#!/usr/bin/env sh
case "$1" in
  *Username*) echo "x-access-token" ;;
  *Password*) echo "${GITHUB_TOKEN}" ;;
  *) echo "" ;;
esac
SH
chmod +x "$askpass"
trap 'rm -f "$askpass"' EXIT

GIT_ASKPASS="$askpass" GIT_TERMINAL_PROMPT=0 git push -u origin main

echo "Done: https://github.com/$owner/$repo_name"
