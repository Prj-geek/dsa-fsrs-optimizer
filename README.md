# DSA FSRS Optimizer

Fits personalized FSRS-5 weights from your Notion "Review Log" history and
writes them back into the "FSRS Config" row, so every scheduling formula in
"DSA Problems" picks them up automatically.

## One-time setup

1. **Create a Notion internal integration**
   - Go to https://www.notion.so/my-integrations -> "New integration"
   - Name it anything (e.g. "DSA FSRS Optimizer"), workspace = yours
   - Copy the "Internal Integration Secret" (starts with `ntn_` or `secret_`)

2. **Share both databases with the integration**
   - Open "Review Log" in Notion -> `...` menu -> "Connections" -> add your integration
   - Open "FSRS Config" -> same steps

3. **Collect the three IDs the script needs**
   - `REVIEW_LOG_DATA_SOURCE_ID`: `4f035804-e42f-47ce-b154-6b4fa99f3226`
   - `FSRS_CONFIG_PAGE_ID`: `3d25adef-674c-8121-ac7f-fe72ac2c6167`
   - (these are already filled in below from the workspace this was built in --
     re-verify them if you ever recreate the databases)

4. **Push this folder to a new GitHub repo**
   ```bash
   git init
   git add .
   git commit -m "Initial FSRS optimizer"
   git branch -M main
   git remote add origin https://github.com/<you>/dsa-fsrs-optimizer.git
   git push -u origin main
   ```

5. **Add repo secrets** (Settings -> Secrets and variables -> Actions -> New repository secret)
   - `NOTION_TOKEN` = the integration secret from step 1
   - `REVIEW_LOG_DATA_SOURCE_ID` = `4f035804-e42f-47ce-b154-6b4fa99f3226`
   - `FSRS_CONFIG_PAGE_ID` = `3d25adef-674c-8121-ac7f-fe72ac2c6167`

6. **Trigger it manually a few times early on**
   - Actions tab -> "Optimize FSRS weights" -> "Run workflow"
   - Until you have ~100+ logged reviews, the script will print a message and
     skip the fit rather than fit on too little data (`MIN_REVIEWS`, default 50)

7. **Let the schedule take over**
   - The workflow already runs daily at 6:00 AM IST (`cron: "30 0 * * *"`) --
     no changes needed once you've got enough review history

## Running locally (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export NOTION_TOKEN=ntn_...
export REVIEW_LOG_DATA_SOURCE_ID=4f035804-e42f-47ce-b154-6b4fa99f3226
export FSRS_CONFIG_PAGE_ID=3d25adef-674c-8121-ac7f-fe72ac2c6167
python optimize_fsrs.py
```
