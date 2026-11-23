# Manual paper deployment

In GitHub Actions select **Deploy paper → Run workflow**, use branch `main`,
and supply a full 40-character commit SHA. The commit must be on main's history
and its latest push runs of both CI and Security must have succeeded.
Deployments are serialized. No deployment runs automatically on push.

The `paper-production` environment contains VDS_DEPLOY_KEY, VDS_KNOWN_HOSTS,
and the VDS_HOST variable. The dedicated cryptodeploy SSH key has a forced
command and cannot open a shell or forward ports. It invokes the root-owned
`/usr/local/sbin/cryptolab-deploy-commit`, installed from deploy-commit.sh.
Changes to this server script require separate administrator installation;
a new repository commit does not silently replace the server deployment policy.
Authorized main-branch source executes in a Docker build: protect repository
write permissions and deployment credentials accordingly.

The server builds exact committed source, labels the image with its SHA,
locks against backups, stops the sole writer, and backs up both SQLite files.
It preserves the existing server Compose configuration and environment.
Schema-changing releases are refused. It pins the new image by image ID,
checks progress/health, and restores the previous image on a failed rollout.
Database files are never automatically overwritten during rollback.
A schema-compatible code change can still have semantic incompatibilities:
review changes before deploying, especially persistence and financial rules.

Current SHA: /srv/cryptolab/CURRENT_COMMIT.
Deployment records: /srv/cryptolab/deployments/<UTC timestamp>-<SHA>/.
Backups: /srv/cryptolab/backups/ (daily retention also applies).
For manual rollback, stop paper-bot, restore previous.env from the selected
record to /srv/cryptolab/.env, then run docker compose up -d --no-build and
verify paper health. Do not restore an old database without explicitly
reviewing simulated activity since its backup.
