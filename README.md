<img src="https://wcm.io/images/favicon-16@2x.png"/> sonarcloud-keepalive
======

Keeps the SonarQube Cloud projects of all wcm.io GitHub organizations alive by triggering regular builds for repositories that have not been analyzed recently.

SonarQube Cloud [deletes projects on the free plan that have not been analyzed for one year](https://docs.sonarsource.com/sonarqube-cloud/managing-your-projects/project-data-lifecycle). Because wcm.io is very modular, some libraries get no commits for more than a year and would lose their SonarQube Cloud project, even though they are still in use.

The [SonarQube Cloud Keepalive](.github/workflows/sonarcloud-keepalive.yml) workflow runs monthly. For each configured GitHub organization, it:

1. Lists all repositories and ignores archived or disabled ones.
2. Keeps only repositories with a SonarQube Cloud project. The GitHub organization must match the SonarQube Cloud organization, and the project key must be `<org>_<repo>`.
3. Checks the date of the last analysis of the project's main branch.
4. If that analysis is older than 30 days and there was no run of the `maven-build.yml` workflow on the default branch in the last 30 days, triggers the regular `maven-build.yml` workflow on the repository's default branch through `workflow_dispatch`. The build then runs the SonarQube Cloud analysis as usual. Other branches are never built.

If an analysis is outdated but there was a recent build on the default branch, no new build is triggered and a warning is reported instead, because the build is most likely broken. The result for each organization is shown in the job summary of the workflow run.


Configuration
-------------

Edit [sonarcloud-keepalive.yml](.github/workflows/sonarcloud-keepalive.yml) to change:

* `matrix.org`: the GitHub organizations to check
* `env.MAX_AGE_DAYS`: the maximum age of the last analysis or build, in days (default: 30)
* `env.WORKFLOW_FILE`: the build workflow to trigger in each repository (default: `maven-build.yml`)
* `on.schedule`: when the check runs (default: 1st day of every month)

To check the current state without triggering builds, start the workflow manually with "Dry run" enabled (the default for manual runs).

To trigger the build of a single repository regardless of its last analysis and recent builds (e.g. to test the setup), start the workflow manually with "Repository" set to `<org>/<repo>` and "Dry run" disabled. The organization has to be one of the configured organizations; all other organizations are skipped.


GitHub App setup
----------------

Triggering workflows in other repositories across several organizations needs a GitHub App:

1. Create a GitHub App, for example "wcm.io SonarCloud Keepalive" in the `wcm-io-devops` organization (Organization settings → Developer settings → GitHub Apps → New GitHub App, or directly via https://github.com/organizations/wcm-io-devops/settings/apps/new):
   * Description (shown on the app page and when installing it):
     ```
     Keeps SonarQube Cloud projects of rarely changed wcm.io repositories alive by triggering their regular build workflow (`maven-build.yml`) when there was no analysis in the last 30 days.

     Used only by the scheduled workflow in https://github.com/wcm-io-devops/sonarcloud-keepalive. Permissions: Actions (read/write) to start the build workflow, Metadata (read-only).
     ```
   * Homepage URL: `https://github.com/wcm-io-devops/sonarcloud-keepalive`
   * Webhook: disabled (uncheck "Active")
   * Repository permissions: **Actions: Read and write**, **Metadata: Read-only**, all others: no access
   * Where can this GitHub App be installed: **Any account**. This is required to install the app in several organizations. Only holders of the private key can create tokens.
2. In the app settings, note the **Client ID** and generate a private key (downloads a `.pem` file).
3. Install the app in all configured organizations, with access to all repositories (app settings → Install App; owners of other organizations can use [https://github.com/apps/wcm-io-sonarcloud-keepalive](https://github.com/apps/wcm-io-sonarcloud-keepalive)).
4. In this repository (Settings → Secrets and variables → Actions), configure:
   * Variable `SONARCLOUD_KEEPALIVE_APP_CLIENT_ID`: the client ID of the app
   * Secret `SONARCLOUD_KEEPALIVE_APP_PRIVATE_KEY`: the full content of the `.pem` file

   Delete the local `.pem` file afterwards.


Running locally
---------------

```
GITHUB_TOKEN=<token> python3 scripts/sonarcloud_keepalive.py --org wcm-io --dry-run
GITHUB_TOKEN=<token> python3 scripts/sonarcloud_keepalive.py --org wcm-io --repo wcm-io/<repo> --dry-run
```


Note on scheduled workflows
---------------------------

GitHub disables scheduled workflows in public repositories with no repository activity for 60 days, and sends a notification beforehand. Renovate keeps this repository's action versions up to date and automerges non-major updates (see [renovate.json](.github/renovate.json)), which usually keeps the repository active without manual work. If the workflow gets disabled anyway, re-enable it in the "Actions" tab - the next run catches up on all stale projects, as SonarQube Cloud deletes projects only after one year without analysis.
