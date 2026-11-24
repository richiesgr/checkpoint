---
inclusion: always
---
# Git workflow

- Remote: `gitea@192.168.50.214:richiesgr/checkpoint-assignement.git` (SSH). Name the remote `origin`.
- The remote repository already exists. If the workspace is not yet a git repository, run `git init`, `git remote add origin <url>`, then `git fetch` and align with the remote's default branch before committing. Do not overwrite remote history.
- Work on a feature branch, for example `feature/<short-name>`, not directly on the default branch, unless the user says otherwise.
- Make small commits, one logical change each, using Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`). When a spec task is completed, commit it with the task id in the message, for example `feat: config model and validation (task 2.2)`.
- Run the test command before each commit. Do not commit failing tests, except a commit that is clearly labelled work in progress.
- Stage files by name. Never use `git add -A` or `git add .`.
- Never commit: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.hypothesis/`, Spark artifacts (`spark-warehouse/`, `metastore_db/`, `derby.log`), checkpoint directories, run output directories (`output/`, `audit/`, `rejected/`), Kafka data volumes, `.env` files, or any secret. Keep `.gitignore` current with these.
- Ask the user before `git push`, and before any force push, history rewrite, `reset --hard`, `clean`, branch deletion or merge into the default branch. Never skip hooks with `--no-verify`.
- Never modify git config.
- The AI interaction log under `docs/ai-log/` is a deliverable and is committed unedited, with secrets and personal data removed.